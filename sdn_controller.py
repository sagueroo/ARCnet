#!/usr/bin/env python3
"""ARCnet SDN Controller - Intent-based BGP/MPLS VPN provisioning."""

import argparse
import copy
import json
import ipaddress
import os
import sys
import telnetlib
import time

GNS3_PROJECT_DIR = r"./GNS"
HOST = "127.0.0.1"
_LOOPBACK_BASE = int(ipaddress.IPv4Address("10.200.0.0"))
# Anciens noms VRF a supprimer (renommage Shared -> SopraSteria, etc.)
LEGACY_VRF_NAMES = ("Shared",)

# Startup-config minimale Dynamips apres reset (hostname = nom du noeud GNS3)
STARTUP_CONFIG_TEMPLATE = """\
!
!
!
service timestamps debug datetime msec
service timestamps log datetime msec
no service password-encryption
!
hostname {hostname}
!
ip cef
no ip domain-lookup
no ip icmp rate-limit unreachable
ip tcp synwait 5
no cdp log mismatch duplex
!
line con 0
 exec-timeout 0 0
 logging synchronous
 privilege level 15
 no login
line aux 0
 exec-timeout 0 0
 logging synchronous
 privilege level 15
 no login
!
!
end
"""


# ── Utilitaires IP ──────────────────────────────────────────


def cidr_to_netmask(cidr):
    s = str(cidr)
    if not s.startswith("/"):
        s = "/" + s
    return str(ipaddress.IPv4Network(f"0.0.0.0{s}", strict=False).netmask)


def iface_ipv4(intf_data):
    if "ip" not in intf_data:
        return None
    return str(ipaddress.ip_interface(intf_data["ip"]).ip)


def iface_mask(intf_data):
    if "ip" not in intf_data:
        return "/32"
    return f"/{ipaddress.ip_interface(intf_data['ip']).network.prefixlen}"


def rip_network(intf_data):
    if "ip" not in intf_data:
        return None
    return str(ipaddress.ip_interface(intf_data["ip"]).network.network_address)


def bgp_network_for(intf_data):
    """Un seul prefixe (compat tests) — preferer bgp_networks_for."""
    nets = bgp_networks_for(intf_data)
    return nets[0] if nets else (None, None)


def bgp_networks_for(intf_data):
    """
    Prefixes BGP a annoncer sur un CE.
    Si 'announce' (global NAT) et IP LAN sont tous deux presents, annonce les deux :
    le LAN pour CE2<->CE3, le global pour le hub SopraSteria.
    """
    nets = []
    seen = set()
    if "ip" in intf_data:
        local = ipaddress.ip_interface(intf_data["ip"]).network
        entry = (str(local.network_address), str(local.netmask))
        if entry not in seen:
            nets.append(entry)
            seen.add(entry)
    if "announce" in intf_data:
        ann = ipaddress.ip_network(intf_data["announce"], strict=False)
        entry = (str(ann.network_address), str(ann.netmask))
        if entry not in seen:
            nets.append(entry)
            seen.add(entry)
    return nets


def peer_ip(intf_data):
    if "ip" not in intf_data:
        return None
    iface = ipaddress.ip_interface(intf_data["ip"])
    net = iface.network
    if net.prefixlen >= 31:
        return next((str(a) for a in net if a != iface.ip), None)
    return next((str(a) for a in net.hosts() if a != iface.ip), None)


# ── Validation ──────────────────────────────────────────────


def _all_router_names(intent):
    names = set()
    for info in intent.get("AS", {}).values():
        names.update(info.get("routers", {}).keys())
    return names


def validate_intent(intent):
    """Verifie la coherence de l'intent. Leve ValueError avec details si invalide."""
    errors = []

    if "AS" not in intent or not isinstance(intent["AS"], dict):
        errors.append("Cle 'AS' manquante ou invalide a la racine")
        raise ValueError("Intent invalide:\n  - " + "\n  - ".join(errors))

    all_routers = _all_router_names(intent)
    all_as = set(intent["AS"].keys())

    addr = intent.get("addressing", {})
    if addr:
        for field in ["core_pool", "core_prefix", "customer_pool", "customer_prefix"]:
            if field not in addr:
                errors.append(f"addressing.{field} manquant")
        for pool_key in ["loopback_pool", "core_pool", "customer_pool"]:
            if pool_key in addr:
                try:
                    ipaddress.ip_network(addr[pool_key], strict=False)
                except ValueError as e:
                    errors.append(f"addressing.{pool_key} invalide: {e}")

    for as_num, as_info in intent["AS"].items():
        prefix = f"AS {as_num}"

        if not isinstance(as_info.get("routers"), dict) or not as_info["routers"]:
            errors.append(f"{prefix}: 'routers' manquant ou vide")
            continue

        igp = as_info.get("igp", "OSPF")
        if igp not in ("OSPF", "RIP"):
            errors.append(f"{prefix}: igp doit etre 'OSPF' ou 'RIP', recu '{igp}'")

        is_provider = as_info.get("mpls", False)
        is_isp = as_info.get("isp", False)

        if not is_provider and not is_isp:
            up = as_info.get("upstream_as")
            if up is None:
                errors.append(f"{prefix}: AS client sans 'upstream_as'")
            elif str(up) not in all_as:
                errors.append(f"{prefix}: upstream_as '{up}' n'existe pas")

        if is_provider and as_info.get("vrfs"):
            for vname, vdata in as_info["vrfs"].items():
                if "customer_as" not in vdata:
                    errors.append(f"{prefix} VRF {vname}: 'customer_as' manquant")

        for rname, rinfo in as_info["routers"].items():
            if not isinstance(rinfo.get("interfaces"), dict):
                errors.append(f"{prefix} routeur {rname}: 'interfaces' manquant")
                continue

            for ifname, ifdata in rinfo["interfaces"].items():
                p = ifdata.get("peer")
                if p and p not in all_routers:
                    errors.append(
                        f"{prefix} {rname}.{ifname}: peer '{p}' inconnu"
                    )
                if ifdata.get("ip"):
                    try:
                        ipaddress.ip_interface(ifdata["ip"])
                    except ValueError as e:
                        errors.append(f"{prefix} {rname}.{ifname}: ip invalide: {e}")

                if ifdata.get("vrf") and is_provider:
                    vrf = ifdata["vrf"]
                    if vrf not in as_info.get("vrfs", {}):
                        errors.append(
                            f"{prefix} {rname}.{ifname}: vrf '{vrf}' non declaree"
                        )

    inet = intent.get("internet")
    if inet:
        pe = inet.get("gateway_pe")
        if not pe or pe not in all_routers:
            errors.append("internet.gateway_pe invalide ou inconnu")
        if not inet.get("interface"):
            errors.append("internet.interface manquant")
        ir = inet.get("router")
        if not ir or ir not in all_routers:
            errors.append("internet.router invalide ou inconnu")
        prov = next((i for i in intent["AS"].values() if i.get("mpls")), {})
        for v in inet.get("vrfs", []):
            if v not in prov.get("vrfs", {}):
                errors.append(f"internet.vrfs: vrf '{v}' non declaree")

    if errors:
        raise ValueError("Intent invalide:\n  - " + "\n  - ".join(errors))


def get_internet_config(intent):
    return intent.get("internet")


def apply_internet_link(intent):
    """Alloue le /30 PE-Internet depuis internet.link si les IP ne sont pas fixes."""
    inet = get_internet_config(intent)
    if not inet:
        return
    pe = inet["gateway_pe"]
    ifname = inet["interface"]
    ir = inet["router"]
    link = inet.get("link")
    if not link:
        return

    pe_info = None
    ir_if = None
    for asn, info in intent["AS"].items():
        if pe in info.get("routers", {}):
            pe_info = info["routers"][pe]["interfaces"].get(ifname)
        if ir in info.get("routers", {}):
            for iname, idata in info["routers"][ir]["interfaces"].items():
                if idata.get("peer") == pe:
                    ir_if = idata

    if not pe_info or not ir_if:
        return
    if pe_info.get("ip") and ir_if.get("ip"):
        return

    net = ipaddress.ip_network(link, strict=False)
    hosts = list(net.hosts())
    if len(hosts) < 2:
        return
    plen = net.prefixlen
    pe_info["ip"] = f"{hosts[0]}/{plen}"
    ir_if["ip"] = f"{hosts[1]}/{plen}"


def pe_vrf_sessions_for_ce(intent, ce_name):
    """Liens VRF PE->CE : (pe_name, ce_ip_vu_par_pe, pe_ip_next_hop_cote_ce)."""
    sessions = []
    for _, as_info in intent["AS"].items():
        if not as_info.get("mpls"):
            continue
        for pe_name, rinfo in as_info["routers"].items():
            for ifdata in rinfo.get("interfaces", {}).values():
                if ifdata.get("peer") == ce_name and ifdata.get("vrf") and ifdata.get("ip"):
                    sessions.append((pe_name, peer_ip(ifdata), iface_ipv4(ifdata)))
    return sessions


def is_dual_homed_ce(intent, ce_name):
    return len({s[0] for s in pe_vrf_sessions_for_ce(intent, ce_name)}) >= 2


def gateway_pe_ce_next_hop(intent, ce_rinfo):
    """IP du PE passerelle Internet sur le lien primaire d'un CE dual-home."""
    inet = get_internet_config(intent)
    if not inet:
        return None
    gw = inet["gateway_pe"]
    for ifdata in ce_rinfo.get("interfaces", {}).values():
        if ifdata.get("peer") == gw and ifdata.get("ip"):
            return peer_ip(ifdata)
    return None


def internet_gateway_next_hop(intent):
    """IP du routeur Internet (next-hop global depuis le PE)."""
    inet = get_internet_config(intent)
    if not inet:
        return None
    pe = inet["gateway_pe"]
    ifname = inet["interface"]
    ir = inet["router"]
    pe_if = intent["AS"][_router_to_as_map(intent)[pe]]["routers"][pe]["interfaces"][ifname]
    ir_asn = _router_to_as_map(intent)[ir]
    for _, idata in intent["AS"][ir_asn]["routers"][ir]["interfaces"].items():
        if idata.get("peer") == pe and idata.get("ip"):
            return str(ipaddress.ip_interface(idata["ip"]).ip)
    return peer_ip(pe_if)


# ── Allocation IP dynamique ─────────────────────────────────


def _router_to_as_map(intent):
    m = {}
    for asn, info in intent["AS"].items():
        for rname in info.get("routers", {}):
            m[rname] = asn
    return m


def discover_links(intent):
    """Decouvre les liens P2P en appariant les champs 'peer' entre routeurs."""
    r2as = _router_to_as_map(intent)
    matched = set()
    links = []

    for asn, info in intent["AS"].items():
        for rname, rinfo in info["routers"].items():
            for ifname, ifdata in rinfo.get("interfaces", {}).items():
                p = ifdata.get("peer")
                if not p or "Loopback" in ifname or (rname, ifname) in matched:
                    continue
                if p not in r2as:
                    continue

                peer_asn = r2as[p]
                peer_rinfo = intent["AS"][peer_asn]["routers"][p]
                for pifname, pifdata in peer_rinfo.get("interfaces", {}).items():
                    if (
                        pifdata.get("peer") == rname
                        and "Loopback" not in pifname
                        and (p, pifname) not in matched
                    ):
                        matched.add((rname, ifname))
                        matched.add((p, pifname))
                        link_type = "core" if asn == peer_asn else "customer"
                        if (asn, rname) <= (peer_asn, p):
                            a = (rname, ifname, ifdata)
                            b = (p, pifname, pifdata)
                        else:
                            a = (p, pifname, pifdata)
                            b = (rname, ifname, ifdata)
                        links.append({"a": a, "b": b, "type": link_type})
                        break

    links.sort(key=lambda l: (l["type"], l["a"][0], l["b"][0]))
    return links


def allocate_ips(intent):
    """Remplit les champs 'ip' manquants a partir des pools declares dans 'addressing'."""
    addr = intent.get("addressing")
    if not addr:
        return

    core_net = ipaddress.ip_network(addr["core_pool"], strict=False)
    core_pre = int(addr["core_prefix"])
    core_subs = core_net.subnets(new_prefix=core_pre)

    cust_net = ipaddress.ip_network(addr["customer_pool"], strict=False)
    cust_pre = int(addr["customer_prefix"])
    cust_subs = cust_net.subnets(new_prefix=cust_pre)

    links = discover_links(intent)

    for link in links:
        _, _, a_data = link["a"]
        _, _, b_data = link["b"]
        a_has = bool(a_data.get("ip"))
        b_has = bool(b_data.get("ip"))

        if a_has and b_has:
            continue

        if a_has and not b_has:
            p = peer_ip(a_data)
            plen = ipaddress.ip_interface(a_data["ip"]).network.prefixlen
            b_data["ip"] = f"{p}/{plen}"
        elif b_has and not a_has:
            p = peer_ip(b_data)
            plen = ipaddress.ip_interface(b_data["ip"]).network.prefixlen
            a_data["ip"] = f"{p}/{plen}"
        else:
            subnet = next(core_subs if link["type"] == "core" else cust_subs)
            hosts = list(subnet.hosts())
            plen = subnet.prefixlen
            a_data["ip"] = f"{hosts[0]}/{plen}"
            b_data["ip"] = f"{hosts[1]}/{plen}"


def build_router_id_map(data):
    """Adresse loopback / router-id unique par routeur (depuis le pool ou 10.200.0.x)."""
    addr = data.get("addressing", {})
    if "loopback_pool" in addr:
        pool = ipaddress.ip_network(addr["loopback_pool"], strict=False)
        hosts = pool.hosts()
    else:
        hosts = None

    ids = {}
    idx = 0
    for _, as_info in data["AS"].items():
        for router_name in as_info["routers"]:
            idx += 1
            if hosts:
                ids[router_name] = str(next(hosts))
            else:
                ids[router_name] = str(ipaddress.IPv4Address(_LOOPBACK_BASE + idx))
    return ids


# ── Helpers reseau ──────────────────────────────────────────


def expand_vrf(provider_as, vrf_spec):
    provider_as = str(provider_as)
    ca = str(vrf_spec["customer_as"])
    rd = str(vrf_spec.get("rd") or f"{provider_as}:{ca}")
    export = f"{provider_as}:{ca}"
    imports = [export]
    for x in vrf_spec.get("import_customers", []):
        rt = str(x) if ":" in str(x) else f"{provider_as}:{x}"
        if rt not in imports:
            imports.append(rt)
    return {"rd": rd, "rt_export": [export], "rt_import": imports}


def router_as_for(intent, name):
    for asn, info in intent["AS"].items():
        if name in info.get("routers", {}):
            return asn
    return None


def faces_provider(intf_data, as_info, intent):
    up = str(as_info.get("upstream_as") or "")
    p = intf_data.get("peer")
    if not up or not p:
        return False
    return p in intent["AS"].get(up, {}).get("routers", {})


def is_core_intf(ifname, ifdata, is_provider, intent=None):
    if not is_provider or not ifdata.get("ip"):
        return False
    if "Loopback" in ifname or ifdata.get("vrf"):
        return False
    inet = get_internet_config(intent) if intent else None
    if inet and ifdata.get("peer") == inet.get("router"):
        return False
    return True


def build_isp_router(data, rname, rinfo, as_s):
    """Routeur Internet (AS ISP) : prefixe public simule + lien vers le PE."""
    cmds = ["ip cef", "no ipv6 cef"]
    inet = get_internet_config(data)
    provider_as = next(k for k, v in data["AS"].items() if v.get("mpls"))

    for ifname, ifdata in rinfo.get("interfaces", {}).items():
        cmds.append(f"interface {ifname}")
        if ifdata.get("ip"):
            cmds.append(
                f" ip address {iface_ipv4(ifdata)} {cidr_to_netmask(iface_mask(ifdata))}"
            )
            cmds.append(" negotiation auto")
        cmds.append(" no shutdown")

    if inet and inet.get("prefix"):
        pfx = ipaddress.ip_network(inet["prefix"], strict=False)
        cmds.append(
            f"ip route {pfx.network_address} {pfx.netmask} Null0"
        )

    pe_nh = None
    for ifdata in rinfo.get("interfaces", {}).values():
        if ifdata.get("peer") == inet.get("gateway_pe") and ifdata.get("ip"):
            pe_nh = peer_ip(ifdata)
            cmds.append(f"ip route 0.0.0.0 0.0.0.0 {pe_nh}")
            break
    if pe_nh:
        cmds.extend([
            f"router bgp {as_s}",
            " bgp log-neighbor-changes",
            f" neighbor {pe_nh} remote-as {provider_as}",
            f" neighbor {pe_nh} activate",
        ])
        if inet.get("prefix"):
            pfx = ipaddress.ip_network(inet["prefix"], strict=False)
            cmds.append(
                f" network {pfx.network_address} mask {pfx.netmask}"
            )

    return cmds


def nat_map_cmds(nat_map):
    loc = ipaddress.ip_network(nat_map["local"], strict=False)
    gl = ipaddress.ip_network(nat_map["global"], strict=False)
    mask = str(loc.netmask)
    return [
        f"ip nat inside source static network {loc.network_address} {gl.network_address} {mask}",
        f"ip route {gl.network_address} {mask} Null0",
    ]


# ── GNS3 ────────────────────────────────────────────────────


def get_gns3_nodes(project_dir):
    """Noeuds Dynamips : {nom: {console, node_id, dynamips_id}}."""
    mapping = {}
    gns3_files = [f for f in os.listdir(project_dir) if f.endswith(".gns3")]
    if not gns3_files:
        return mapping
    with open(os.path.join(project_dir, gns3_files[0]), "r", encoding="utf-8") as f:
        gns3_data = json.load(f)
    for node in gns3_data.get("topology", {}).get("nodes", []):
        if node.get("node_type") != "dynamips":
            continue
        name = node.get("name")
        if not name:
            continue
        props = node.get("properties") or {}
        mapping[name] = {
            "console": node.get("console"),
            "node_id": node.get("node_id"),
            "dynamips_id": props.get("dynamips_id"),
        }
    return mapping


def get_gns3_consoles(project_dir):
    return {
        name: info["console"]
        for name, info in get_gns3_nodes(project_dir).items()
        if info.get("console")
    }


def _dynamips_configs_dir(project_dir, node_id):
    return os.path.join(project_dir, "project-files", "dynamips", node_id, "configs")


def dynamips_startup_config_paths(project_dir, node_id):
    cfg_dir = _dynamips_configs_dir(project_dir, node_id)
    if not os.path.isdir(cfg_dir):
        return []
    return [
        os.path.join(cfg_dir, fname)
        for fname in os.listdir(cfg_dir)
        if fname.endswith("_startup-config.cfg")
    ]


def dynamips_private_config_paths(project_dir, node_id):
    cfg_dir = _dynamips_configs_dir(project_dir, node_id)
    if not os.path.isdir(cfg_dir):
        return []
    return [
        os.path.join(cfg_dir, fname)
        for fname in os.listdir(cfg_dir)
        if fname.endswith("_private-config.cfg")
    ]


def startup_config_from_template(hostname):
    return STARTUP_CONFIG_TEMPLATE.format(hostname=hostname)


def reset_dynamips_saved_configs(project_dir, node_id, hostname):
    """
    Reinitialise les .cfg Dynamips : template startup (bon hostname) + private vide.
    Pris en compte au prochain demarrage du noeud dans GNS3.
    """
    cleared = []
    body = startup_config_from_template(hostname)
    for path in dynamips_startup_config_paths(project_dir, node_id):
        with open(path, "w", encoding="ascii", newline="\n") as f:
            f.write(body)
        cleared.append(path)
    for path in dynamips_private_config_paths(project_dir, node_id):
        with open(path, "w", encoding="ascii") as f:
            f.write("")
        cleared.append(path)
    return cleared


# ── Generation des commandes IOS ────────────────────────────


def generate_teardown(state, intent):
    cmds = {}
    for old_as, old_info in state.get("AS", {}).items():
        for rname, old_r in old_info.get("routers", {}).items():
            c = []
            new_info = intent.get("AS", {}).get(old_as, {})
            new_r = new_info.get("routers", {}).get(rname, {})

            if not new_info:
                c.append(f"no router bgp {old_as}")

            for vname in old_info.get("vrfs", {}):
                if vname not in new_info.get("vrfs", {}):
                    c.append(f"no vrf definition {vname}")

            if new_info.get("mpls") and _is_pe_router(new_r or old_r):
                current = set(new_info.get("vrfs", {}))
                for legacy in LEGACY_VRF_NAMES:
                    if legacy not in current:
                        c.append(f"no vrf definition {legacy}")

            for ifname in old_r.get("interfaces", {}):
                if ifname not in new_r.get("interfaces", {}):
                    c.extend([f"default interface {ifname}", f"interface {ifname}", " shutdown", " exit"])

            if c:
                cmds[rname] = c
    return cmds


def negate_build_commands(cmds):
    """Inverse les commandes generees par generate_build (reset sans write erase)."""
    neg = []
    seen_bgp = set()
    seen_vrf = set()
    seen_acl = set()
    seen_rmap = set()

    interfaces = []
    for c in cmds:
        if c.startswith("interface "):
            interfaces.append(c.split()[1])

    for ifname in reversed(interfaces):
        neg.extend([
            f"default interface {ifname}",
            f"interface {ifname}",
            " shutdown",
            " exit",
        ])

    for c in reversed(cmds):
        s = c.strip()
        if (
            s.startswith("interface ")
            or s == "no shutdown"
            or s.startswith("negotiation ")
            or (
                s.startswith("ip ")
                and not s.startswith("ip route")
                and not s.startswith("ip nat inside source")
                and not s.startswith("ip access-list ")
            )
            or s == "mpls ip"
            or s.startswith("vrf forwarding")
            or s.startswith("address-family")
            or s.startswith("neighbor")
            or s.startswith("network ")
            or s.startswith("redistribute ")
            or s in ("exit-address-family", "exit")
            or s.startswith("route-target ")
            or s == "address-family ipv4"
            or s == "no ipv6 cef"
        ):
            continue

        if s.startswith("router bgp "):
            asn = s.split()[2]
            if asn not in seen_bgp:
                neg.append(f"no router bgp {asn}")
                seen_bgp.add(asn)
        elif s.startswith("router ospf "):
            neg.append("no router ospf 1")
        elif s.startswith("router rip"):
            neg.append("no router rip")
        elif s.startswith("vrf definition "):
            vname = s.split()[2]
            if vname not in seen_vrf:
                neg.append(f"no vrf definition {vname}")
                seen_vrf.add(vname)
        elif s.startswith("ip route "):
            neg.append(f"no {s}")
        elif s.startswith("ip nat "):
            neg.append(f"no {s}")
        elif s.startswith("route-map "):
            rmap = s.split()[1]
            if rmap not in seen_rmap:
                neg.append(f"no route-map {rmap}")
                seen_rmap.add(rmap)
        elif s.startswith("ip access-list standard "):
            acl = s.split()[3]
            if acl not in seen_acl:
                neg.append(f"no ip access-list standard {acl}")
                seen_acl.add(acl)
        elif s == "ip cef":
            neg.append("no ip cef")

    return neg


def _is_pe_router(rinfo):
    return any("vrf" in d for d in rinfo.get("interfaces", {}).values())


def legacy_vrf_cleanup_cmds(data, rname, aggressive=False, fresh_routing=False):
    """
    Supprime les VRF obsoletes (ex. Shared) et libere les RD avant recreate.
    aggressive=True : reset demo (default interface sur tous les ports).
    fresh_routing=True : apply sur PE (default ports VRF + no vrf + no router bgp).
    """
    cmds = []
    for asn, as_info in data.get("AS", {}).items():
        if not as_info.get("mpls"):
            continue
        rinfo = as_info.get("routers", {}).get(rname)
        if not rinfo or not _is_pe_router(rinfo):
            continue
        current = set(as_info.get("vrfs", {}).keys())
        if aggressive:
            for ifname in rinfo.get("interfaces", {}):
                cmds.extend([
                    f"default interface {ifname}",
                    f"interface {ifname}",
                    " shutdown",
                    " exit",
                ])
        elif fresh_routing:
            for ifname, ifdata in rinfo.get("interfaces", {}).items():
                if ifdata.get("vrf"):
                    cmds.extend([
                        f"default interface {ifname}",
                        f"interface {ifname}",
                        " exit",
                    ])
        for legacy in LEGACY_VRF_NAMES:
            if legacy not in current:
                cmds.append(f"no vrf definition {legacy}")
        if fresh_routing:
            for vname in current:
                cmds.append(f"no vrf definition {vname}")
            cmds.append(f"no router bgp {asn}")
            if as_info.get("igp") == "OSPF":
                cmds.append("no router ospf 1")
        break
    return cmds


def generate_reset_commands(data, aggressive_legacy=True):
    """Demontage complet pour reset (intent actuel + VRF legacy)."""
    if not data.get("AS"):
        return {}
    build = generate_build(data)
    reset = {}
    for rname, cmds in build.items():
        neg = negate_build_commands(cmds)
        legacy = legacy_vrf_cleanup_cmds(data, rname, aggressive=aggressive_legacy)
        reset[rname] = legacy + neg
    return reset


def scan_configs_for_legacy_vrf(project_dir, names):
    """Avertit si des startup-config Dynamips contiennent encore une VRF legacy."""
    hits = []
    for rname, info in names.items():
        node_id = info.get("node_id")
        if not node_id:
            continue
        for path in dynamips_startup_config_paths(project_dir, node_id):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            except OSError:
                continue
            for legacy in LEGACY_VRF_NAMES:
                if legacy in text:
                    hits.append((rname, os.path.basename(path), legacy))
    return hits


def generate_build(data):
    build = {}
    rids = build_router_id_map(data)

    for as_num, as_info in data["AS"].items():
        is_prov = as_info.get("mpls", False)
        igp = as_info.get("igp", "OSPF")
        as_s = str(as_num)

        for rname, rinfo in as_info["routers"].items():
            if as_info.get("isp"):
                build[rname] = build_isp_router(data, rname, rinfo, as_s)
                continue

            cmds = ["ip cef", "no ipv6 cef"]
            inet = get_internet_config(data)
            inet_vrfs = set(inet.get("vrfs", [])) if inet else set()
            interfaces = rinfo.get("interfaces", {})
            is_pe = _is_pe_router(rinfo)

            if is_pe:
                cmds = legacy_vrf_cleanup_cmds(
                    data, rname, aggressive=False, fresh_routing=True
                ) + cmds

            if is_prov and as_info.get("vrfs"):
                for vname, vspec in as_info["vrfs"].items():
                    v = expand_vrf(as_s, vspec)
                    cmds.extend([f"vrf definition {vname}", f" rd {v['rd']}", " address-family ipv4"])
                    for rt in v["rt_export"]:
                        cmds.append(f"  route-target export {rt}")
                    for rt in v["rt_import"]:
                        cmds.append(f"  route-target import {rt}")
                    cmds.append(" exit-address-family")
            is_inet_gw = bool(inet and rname == inet.get("gateway_pe"))
            inet_if = inet.get("interface") if is_inet_gw else None

            for ifname, ifdata in interfaces.items():
                cmds.append(f"interface {ifname}")
                if ifdata.get("vrf"):
                    cmds.append(f" vrf forwarding {ifdata['vrf']}")
                if ifdata.get("nat"):
                    cmds.append(f" ip nat {ifdata['nat']}")
                if is_inet_gw and ifname == inet_if:
                    cmds.append(" ip nat outside")
                if is_inet_gw and ifdata.get("vrf") in inet_vrfs:
                    cmds.append(" ip nat inside")

                if "Loopback" in ifname:
                    cmds.append(f" ip address {rids[rname]} {cidr_to_netmask(ifdata.get('mask', '/32'))}")
                    if is_prov and igp == "OSPF":
                        cmds.append(" ip ospf 1 area 0")
                elif ifdata.get("ip"):
                    cmds.append(f" ip address {iface_ipv4(ifdata)} {cidr_to_netmask(iface_mask(ifdata))}")
                    cmds.append(" negotiation auto")
                    if is_core_intf(ifname, ifdata, is_prov, data):
                        if igp == "OSPF":
                            cmds.append(" ip ospf 1 area 0")
                        cmds.append(" mpls ip")
                    elif not is_prov and not faces_provider(ifdata, as_info, data) and igp == "OSPF":
                        cmds.append(" ip ospf 1 area 0")

                cmds.append(" no shutdown")

            if rinfo.get("nat_map"):
                cmds.extend(nat_map_cmds(rinfo["nat_map"]))

            rid = rids[rname]

            if igp == "OSPF":
                cmds.extend([f"router ospf 1", f" router-id {rid}"])
            elif igp == "RIP":
                cmds.extend(["router rip", " version 2"])
                for ifname, ifdata in interfaces.items():
                    if not ifdata.get("ip") or "Loopback" in ifname:
                        continue
                    skip_peer = "CE" if is_prov else "PE"
                    if is_prov and ifdata.get("vrf"):
                        continue
                    if not is_prov and faces_provider(ifdata, as_info, data):
                        continue
                    line = rip_network(ifdata)
                    if line:
                        cmds.append(f"  network {line}")
                cmds.append(" exit")

            if is_prov:
                cmds.extend([f"router bgp {as_s}", f" bgp router-id {rid}", " bgp log-neighbor-changes"])
                if is_pe:
                    pe_neighbors = [
                        orid
                        for other, orid in rids.items()
                        if other != rname and other.startswith("PE")
                    ]
                    for orid in pe_neighbors:
                        cmds.extend([
                            f" neighbor {orid} remote-as {as_s}",
                            f" neighbor {orid} update-source Loopback0",
                        ])
                    if pe_neighbors:
                        cmds.append(" address-family vpnv4")
                        for orid in pe_neighbors:
                            cmds.extend([
                                f"  neighbor {orid} activate",
                                f"  neighbor {orid} send-community extended",
                            ])
                        cmds.append(" exit-address-family")
                    for ifname, ifdata in interfaces.items():
                        if "vrf" not in ifdata or "ip" not in ifdata:
                            continue
                        ce_ip = peer_ip(ifdata)
                        if not ce_ip:
                            continue
                        ce_as = str(ifdata.get("customer_as") or "")
                        if not ce_as:
                            ce_as = router_as_for(data, ifdata.get("peer", "")) or "65000"
                        vrf_cmds = [
                            f" address-family ipv4 vrf {ifdata['vrf']}",
                            f"  neighbor {ce_ip} remote-as {ce_as}",
                            f"  neighbor {ce_ip} activate",
                            "  redistribute connected",
                        ]
                        if ifdata["vrf"] in inet_vrfs:
                            ce_name = ifdata.get("peer", "")
                            gw = inet.get("gateway_pe") if inet else None
                            skip_default = (
                                gw
                                and rname != gw
                                and ce_name
                                and is_dual_homed_ce(data, ce_name)
                            )
                            if not skip_default:
                                vrf_cmds.append(
                                    f"  neighbor {ce_ip} default-originate"
                                )
                        vrf_cmds.append(" exit-address-family")
                        cmds.extend(vrf_cmds)

                if inet and is_pe and rname != inet.get("gateway_pe"):
                    gw_rid = rids.get(inet["gateway_pe"])
                    if gw_rid:
                        for vname in inet.get("vrfs", []):
                            if vname not in as_info.get("vrfs", {}):
                                continue
                            cmds.append(
                                f"ip route vrf {vname} 0.0.0.0 0.0.0.0 {gw_rid} global"
                            )
                            if inet.get("prefix"):
                                pfx = ipaddress.ip_network(
                                    inet["prefix"], strict=False
                                )
                                cmds.append(
                                    f"ip route vrf {vname} {pfx.network_address} "
                                    f"{pfx.netmask} {gw_rid} global"
                                )

                if inet and rname == inet.get("gateway_pe"):
                    nh = internet_gateway_next_hop(data)
                    if nh:
                        cmds.append(f"ip route 0.0.0.0 0.0.0.0 {nh}")
                        if inet.get("prefix"):
                            pfx = ipaddress.ip_network(inet["prefix"], strict=False)
                            cmds.append(
                                f"ip route {pfx.network_address} {pfx.netmask} {nh}"
                            )
                        for vname in inet.get("vrfs", []):
                            cmds.append(
                                f"ip route vrf {vname} 0.0.0.0 0.0.0.0 {nh} global"
                            )
                            if inet.get("prefix"):
                                pfx = ipaddress.ip_network(inet["prefix"], strict=False)
                                cmds.append(
                                    f"ip route vrf {vname} {pfx.network_address} "
                                    f"{pfx.netmask} {nh} global"
                                )
                        cmds.extend([
                            "ip access-list standard ARCNET-TO-INET",
                            " permit any",
                        ])
                        for vname in inet.get("vrfs", []):
                            cmds.append(
                                f"ip nat inside source list ARCNET-TO-INET "
                                f"interface {inet_if} vrf {vname} overload"
                            )
            else:
                pe_as = str(as_info["upstream_as"])

                if inet and is_dual_homed_ce(data, rname):
                    inet_nh = gateway_pe_ce_next_hop(data, rinfo)
                    if inet_nh:
                        cmds.append(f"ip route 0.0.0.0 0.0.0.0 {inet_nh}")

                prepend_maps = {}
                for ifname, ifdata in interfaces.items():
                    n = ifdata.get("prepend")
                    if n and n > 0:
                        map_name = f"PREPEND-{n}x"
                        if map_name not in prepend_maps:
                            prepend_maps[map_name] = n
                for map_name, count in prepend_maps.items():
                    path = (f" {as_s}" * count).strip()
                    cmds.extend([
                        f"route-map {map_name} permit 10",
                        f" set as-path prepend {path}",
                    ])

                cmds.extend([f"router bgp {as_s}", " bgp log-neighbor-changes", " address-family ipv4"])
                for ifname, ifdata in interfaces.items():
                    if not ifdata.get("ip"):
                        continue
                    if not faces_provider(ifdata, as_info, data):
                        for prefix, mask in bgp_networks_for(ifdata):
                            cmds.append(f"  network {prefix} mask {mask}")
                    else:
                        pe = peer_ip(ifdata)
                        if pe:
                            cmds.extend([f"  neighbor {pe} remote-as {pe_as}", f"  neighbor {pe} activate"])
                            if ifdata.get("allowas_in"):
                                cmds.append(f"  neighbor {pe} allowas-in")
                            n = ifdata.get("prepend")
                            if n and n > 0:
                                cmds.append(f"  neighbor {pe} route-map PREPEND-{n}x out")
                cmds.append(" exit-address-family")

            build[rname] = cmds
    return build


# ── Push Telnet ─────────────────────────────────────────────

_TELNET_SLOW_PREFIXES = (
    "router bgp",
    "address-family",
    "vrf definition",
    "  neighbor",
    "  redistribute",
    "ip route vrf",
    "ip nat inside",
    "no router bgp",
    "no vrf definition",
    "default interface",
)


def _telnet_send_line(tn, cmd):
    """Envoie une ligne IOS et attend le prompt (evite commandes tronquees)."""
    tn.write(cmd.encode("ascii") + b"\r\n")
    delay = 0.06
    stripped = cmd.strip()
    if any(stripped.startswith(p) for p in _TELNET_SLOW_PREFIXES):
        delay = 0.18
    time.sleep(delay)
    try:
        tn.read_until(b"#", timeout=8)
    except EOFError:
        pass


def _telnet_session(port, timeout=10):
    tn = telnetlib.Telnet(HOST, port, timeout=timeout)
    tn.write(b"\r\n\r\n")
    time.sleep(0.8)
    tn.write(b"enable\r\n")
    time.sleep(0.4)
    try:
        tn.read_until(b"#", timeout=5)
    except EOFError:
        pass
    tn.write(b"configure terminal\r\n")
    time.sleep(0.4)
    try:
        tn.read_until(b"#", timeout=5)
    except EOFError:
        pass
    return tn


def push_to_router(name, port, commands):
    print(f"  [{name}] Connexion sur port {port}...")
    try:
        tn = _telnet_session(port)
        for cmd in commands:
            _telnet_send_line(tn, cmd)
        _telnet_send_line(tn, "end")
        tn.write(b"write memory\r\n")
        time.sleep(1.5)
        try:
            tn.read_until(b"#", timeout=10)
        except EOFError:
            pass
        tn.close()
        print(f"  [{name}] OK - {len(commands)} commandes injectees")
    except Exception as e:
        print(f"  [{name}] ERREUR: {e}")


def push_reset_to_router(name, port, commands):
    """Envoie les commandes de demontage (no / default interface) en Telnet."""
    if not commands:
        return
    print(f"  [{name}] Demontage IOS ({len(commands)} cmd)...")
    try:
        tn = _telnet_session(port, timeout=15)
        for cmd in commands:
            _telnet_send_line(tn, cmd)
        _telnet_send_line(tn, "end")
        tn.close()
        print(f"  [{name}] OK - demontage Telnet")
    except Exception as e:
        print(f"  [{name}] ERREUR Telnet: {e}")


# ── Chargement intent + state ───────────────────────────────


def load_and_prepare_intent(path="intent.json"):
    with open(path, "r") as f:
        intent = json.load(f)
    validate_intent(intent)
    apply_internet_link(intent)
    allocate_ips(intent)
    return intent


def load_state(path="state.json"):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_state(intent, path="state.json"):
    with open(path, "w") as f:
        json.dump(intent, f, indent=4)


# ── Sous-commandes CLI ──────────────────────────────────────


def cmd_validate(args):
    """Valide l'intent et affiche un resume."""
    try:
        intent = load_and_prepare_intent(args.intent)
    except ValueError as e:
        print(f"ERREUR\n{e}", file=sys.stderr)
        return 1

    rids = build_router_id_map(intent)
    links = discover_links(intent)
    n_routers = sum(len(info["routers"]) for info in intent["AS"].values())
    n_as = len(intent["AS"])
    has_pools = bool(intent.get("addressing"))

    print(f"Intent OK : {n_as} AS, {n_routers} routeurs, {len(links)} liens")
    print(f"Allocation : {'dynamique (pools)' if has_pools else 'manuelle'}")
    print(f"\nRouter-ID / Loopback :")
    for name, rid in rids.items():
        print(f"  {name:<6} -> {rid}")
    print(f"\nLiens ({len(links)}) :")
    for lk in links:
        ar, aif, ad = lk["a"]
        br, bif, bd = lk["b"]
        print(f"  [{lk['type']:<8}] {ar}.{aif} ({ad.get('ip','auto')}) <-> {br}.{bif} ({bd.get('ip','auto')})")
    return 0


def cmd_diff(args):
    """Affiche les commandes sans les pousser."""
    try:
        intent = load_and_prepare_intent(args.intent)
    except ValueError as e:
        print(f"ERREUR\n{e}", file=sys.stderr)
        return 1

    state = load_state()
    teardown = generate_teardown(state, intent)
    build = generate_build(intent)

    only = set(args.only.split(",")) if args.only else None

    for rname in sorted(set(list(teardown.keys()) + list(build.keys()))):
        if only and rname not in only:
            continue
        td = teardown.get(rname, [])
        bd = build.get(rname, [])
        total = len(td) + len(bd)
        print(f"\n{'='*50}")
        print(f" {rname} ({total} commandes)")
        print(f"{'='*50}")
        if td:
            print(" [TEARDOWN]")
            for c in td:
                print(f"   {c}")
        print(" [BUILD]")
        for c in bd:
            print(f"   {c}")
    return 0


def cmd_apply(args):
    """Pousse la configuration sur les routeurs GNS3."""
    try:
        intent = load_and_prepare_intent(args.intent)
    except ValueError as e:
        print(f"ERREUR\n{e}", file=sys.stderr)
        return 1

    consoles = get_gns3_consoles(GNS3_PROJECT_DIR)
    if not consoles:
        print("Aucune console GNS3 trouvee.", file=sys.stderr)
        return 1

    state = load_state()
    teardown = generate_teardown(state, intent)
    build = generate_build(intent)

    only = set(args.only.split(",")) if args.only else None

    print("1. Calcul du delta...")
    targets = sorted(set(list(teardown.keys()) + list(build.keys())))
    if only:
        targets = [r for r in targets if r in only]

    print(f"2. Push sur {len(targets)} routeur(s)...")
    for rname in targets:
        if rname not in consoles:
            print(f"  [{rname}] SKIP - pas de console GNS3")
            continue
        final = teardown.get(rname, []) + build.get(rname, [])
        push_to_router(rname, consoles[rname], final)

    save_state(intent)
    print("\nTermine.")
    return 0


def cmd_reset(args):
    """Reset GNS3 : demontage IOS (no ...) + vidage des startup-config Dynamips."""
    try:
        intent = load_and_prepare_intent(args.intent)
    except ValueError as e:
        print(f"ERREUR\n{e}", file=sys.stderr)
        return 1

    nodes = get_gns3_nodes(GNS3_PROJECT_DIR)
    if not nodes:
        print("Aucun noeud Dynamips trouve dans GNS3.", file=sys.stderr)
        return 1

    only = set(args.only.split(",")) if args.only else None
    targets = sorted(nodes.keys())
    if only:
        targets = [r for r in targets if r in only]

    teardown = generate_reset_commands(intent)
    print("1. Demontage IOS (intent actuel + suppression VRF legacy ex. Shared)...")

    for rname in targets:
        console = nodes[rname].get("console")
        cmds = teardown.get(rname, [])
        if console and cmds and not args.files_only:
            push_reset_to_router(rname, console, cmds)
        elif console and not cmds and not args.files_only:
            print(f"  [{rname}] (rien a demonter en Telnet)")

    print("2. Reinitialisation startup-config Dynamips (template + hostname)...")
    for rname in targets:
        node_id = nodes[rname].get("node_id")
        if not node_id:
            print(f"  [{rname}] SKIP - node_id inconnu")
            continue
        if args.telnet_only:
            continue
        cleared = reset_dynamips_saved_configs(GNS3_PROJECT_DIR, node_id, rname)
        if cleared:
            print(f"  [{rname}] template startup (hostname {rname}), private vide")
        else:
            print(
                f"  [{rname}] aucun .cfg trouve "
                f"(project-files/dynamips/{node_id}/configs/)"
            )

    if not args.keep_state and os.path.exists("state.json"):
        os.remove("state.json")
        print("state.json supprime.")

    stale = scan_configs_for_legacy_vrf(GNS3_PROJECT_DIR, nodes)
    if stale:
        print(
            "\nATTENTION : 'Shared' (ou autre VRF legacy) encore dans des .cfg "
            "sur disque :"
        )
        for rname, fname, legacy in stale:
            print(f"  - {rname} / {fname} (contient {legacy})")
        print("  -> Stop/Start obligatoire dans GNS3 apres reset.")

    print(
        "\n3. Dans GNS3 : arreter puis redemarrer TOUS les noeuds concernes "
        "(sinon l'ancienne config reste en RAM / startup)."
    )
    print("   Puis : python sdn_controller.py apply")
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="sdn_controller",
        description="ARCnet - Intent-based BGP/MPLS VPN provisioning",
    )
    parser.add_argument(
        "--intent", default="intent.json",
        help="Chemin vers le fichier intent (defaut: intent.json)",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("validate", help="Valider l'intent et afficher un resume")

    diff_p = sub.add_parser("diff", help="Afficher les commandes sans pousser")
    diff_p.add_argument("--only", help="Routeurs cibles, separes par des virgules (ex: PE1,CE)")

    apply_p = sub.add_parser("apply", help="Pousser la configuration sur les routeurs")
    apply_p.add_argument("--only", help="Routeurs cibles, separes par des virgules (ex: PE1,CE)")

    reset_p = sub.add_parser(
        "reset",
        help="Reset demo : demontage IOS + vidage des .cfg Dynamips (GNS3)",
    )
    reset_p.add_argument("--only", help="Routeurs cibles, separes par des virgules (ex: PE1,CE)")
    reset_p.add_argument(
        "--keep-state",
        action="store_true",
        help="Ne pas supprimer state.json apres le reset",
    )
    reset_p.add_argument(
        "--files-only",
        action="store_true",
        help="Vider seulement les startup-config Dynamips (pas de Telnet)",
    )
    reset_p.add_argument(
        "--telnet-only",
        action="store_true",
        help="Demontage Telnet seulement (ne pas toucher aux .cfg)",
    )

    args = parser.parse_args()

    if args.command is None:
        args.command = "apply"
        args.only = None

    if args.command == "validate":
        return cmd_validate(args)
    elif args.command == "diff":
        if not hasattr(args, "only"):
            args.only = None
        return cmd_diff(args)
    elif args.command == "apply":
        if not hasattr(args, "only"):
            args.only = None
        return cmd_apply(args)
    elif args.command == "reset":
        if not hasattr(args, "only"):
            args.only = None
        return cmd_reset(args)
    else:
        parser.print_help()
        return 2


if __name__ == "__main__":
    sys.exit(main())
