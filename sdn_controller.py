import json
import ipaddress
import os
import telnetlib
import time

GNS3_PROJECT_DIR = r"./GNS"
HOST = "127.0.0.1"


def cidr_to_netmask(cidr):
    s = str(cidr)
    if not s.startswith("/"):
        s = "/" + s.lstrip("/")
    return str(ipaddress.IPv4Network(f"0.0.0.0{s}", strict=False).netmask)


def expand_vrf(provider_as, vrf_spec):
    """Construit rd / route-targets à partir de customer_as et import_customers."""
    provider_as = str(provider_as)
    ca = str(vrf_spec["customer_as"])
    rd = str(vrf_spec.get("rd") or f"{provider_as}:{ca}")
    export = f"{provider_as}:{ca}"
    imports = [export]
    for x in vrf_spec.get("import_customers", []):
        xs = str(x)
        rt = xs if ":" in xs else f"{provider_as}:{xs}"
        if rt not in imports:
            imports.append(rt)
    return {"rd": rd, "rt_export": [export], "rt_import": imports}


def iface_ipv4_string(intf_data):
    """Adresse IPv4 sans préfixe (pour les commandes IOS)."""
    if "ip" not in intf_data:
        return None
    return str(ipaddress.ip_interface(intf_data["ip"]).ip)


def iface_mask_cidr(intf_data):
    if "ip" not in intf_data:
        return intf_data.get("mask", "/32")
    return f"/{ipaddress.ip_interface(intf_data['ip']).network.prefixlen}"


def rip_network_line(intf_data):
    if "ip" not in intf_data:
        return None
    net = ipaddress.ip_interface(intf_data["ip"]).network
    return str(net.network_address)


def bgp_network_for_interface(intf_data):
    """Préfixe annoncé en BGP (announce si présent, sinon réseau de l'interface)."""
    if "announce" in intf_data:
        net = ipaddress.ip_network(intf_data["announce"], strict=False)
    elif "ip" in intf_data:
        net = ipaddress.ip_interface(intf_data["ip"]).network
    else:
        return None, None
    return str(net.network_address), str(net.netmask)


def remote_peer_ip(intf_data):
    """Autre extrémité d'un lien numéroté (typiquement /30)."""
    if "ip" not in intf_data:
        return None
    iface = ipaddress.ip_interface(intf_data["ip"])
    net = iface.network
    if net.prefixlen >= 31:
        for a in net:
            if a != iface.ip:
                return str(a)
        return None
    for a in net.hosts():
        if a != iface.ip:
            return str(a)
    return None


def router_as_for_name(intent, router_name):
    for asn, info in intent["AS"].items():
        if router_name in info.get("routers", {}):
            return asn
    return None


def faces_provider(intf_data, as_info, intent):
    up = str(as_info.get("upstream_as") or "")
    peer = intf_data.get("peer")
    if not up or not peer:
        return False
    return peer in intent["AS"].get(up, {}).get("routers", {})


def is_mpls_core_interface(intf_name, intf_data, is_provider):
    if not is_provider or not intf_data.get("ip"):
        return False
    if "Loopback" in intf_name:
        return False
    if intf_data.get("vrf"):
        return False
    return True


def nat_map_commands(nat_map):
    loc = ipaddress.ip_network(nat_map["local"], strict=False)
    glob = ipaddress.ip_network(nat_map["global"], strict=False)
    mask = str(loc.netmask)
    return [
        f"ip nat inside source static network {loc.network_address} {glob.network_address} {mask}",
        f"ip route {glob.network_address} {mask} Null0",
    ]


def get_gns3_consoles(project_dir):
    mapping = {}
    gns3_files = [f for f in os.listdir(project_dir) if f.endswith(".gns3")]
    if not gns3_files:
        return mapping
    with open(os.path.join(project_dir, gns3_files[0]), "r") as f:
        gns3_data = json.load(f)
    for node in gns3_data.get("topology", {}).get("nodes", []):
        name = node.get("name")
        console = node.get("console")
        if name and console and node.get("node_type") == "dynamips":
            mapping[name] = console
    return mapping


# Première adresse réservée aux loopbacks / router-id auto (10.200.0.1, 10.200.0.2, …).
# Pas de plafond « 255 » : incrément sur tout le plan d’adressage IPv4.
_LOOPBACK_BASE = int(ipaddress.IPv4Address("10.200.0.0"))


def build_router_identity_map(data):
    """
    Une adresse IPv4 unique par routeur (router-id, loopback cœur si présent).
    Séquence 10.200.0.1, 10.200.0.2, … selon l’ordre des AS puis des routeurs dans l’intent.
    """
    ids = {}
    idx = 0
    for _, as_info in data["AS"].items():
        for router_name in as_info["routers"]:
            idx += 1
            ids[router_name] = str(ipaddress.IPv4Address(_LOOPBACK_BASE + idx))
    return ids


def generate_teardown_commands(state, intent):
    teardown_cmds = {}
    for old_as, old_info in state.get("AS", {}).items():
        for router_name, old_router in old_info.get("routers", {}).items():
            cmds = []
            new_info = intent.get("AS", {}).get(old_as, {})
            new_router = new_info.get("routers", {}).get(router_name, {})

            if not new_info:
                cmds.append(f"no router bgp {old_as}")

            if old_info.get("vrfs"):
                new_vrfs = new_info.get("vrfs", {})
                for vrf_name in old_info["vrfs"]:
                    if vrf_name not in new_vrfs:
                        cmds.append(f"no vrf definition {vrf_name}")

            old_intfs = old_router.get("interfaces", {})
            new_intfs = new_router.get("interfaces", {})
            for intf_name in old_intfs:
                if intf_name not in new_intfs:
                    cmds.append(f"default interface {intf_name}")
                    cmds.append(f"interface {intf_name}")
                    cmds.append(" shutdown")
                    cmds.append(" exit")

            if cmds:
                teardown_cmds[router_name] = cmds
    return teardown_cmds


def generate_build_commands(data):
    build_cmds = {}
    router_ids = build_router_identity_map(data)

    for as_num, as_info in data["AS"].items():
        is_provider = as_info.get("mpls", as_info.get("ldp", False))
        igp = as_info.get("igp", "OSPF")
        as_num_s = str(as_num)

        for router_name, router_info in as_info["routers"].items():
            cmds = ["ip cef", "no ipv6 cef"]

            if is_provider and as_info.get("vrfs"):
                for vrf_name, vrf_spec in as_info["vrfs"].items():
                    v = expand_vrf(as_num_s, vrf_spec)
                    cmds.extend(
                        [
                            f"vrf definition {vrf_name}",
                            f" rd {v['rd']}",
                            " address-family ipv4",
                        ]
                    )
                    for rt in v["rt_export"]:
                        cmds.append(f"  route-target export {rt}")
                    for rt in v["rt_import"]:
                        cmds.append(f"  route-target import {rt}")
                    cmds.append(" exit-address-family")

            interfaces = router_info.get("interfaces", {})
            is_pe = any("vrf" in intf for intf in interfaces.values())

            for intf_name, intf_data in interfaces.items():
                cmds.append(f"interface {intf_name}")
                if intf_data.get("vrf"):
                    cmds.append(f" vrf forwarding {intf_data['vrf']}")
                if intf_data.get("nat"):
                    cmds.append(f" ip nat {intf_data['nat']}")

                if "Loopback" in intf_name:
                    ip = router_ids[router_name]
                    mask = cidr_to_netmask(intf_data.get("mask", "/32"))
                    cmds.append(f" ip address {ip} {mask}")
                    if is_provider and igp == "OSPF":
                        cmds.append(" ip ospf 1 area 0")
                elif intf_data.get("ip"):
                    ip = iface_ipv4_string(intf_data)
                    mask = cidr_to_netmask(iface_mask_cidr(intf_data))
                    cmds.append(f" ip address {ip} {mask}")
                    cmds.append(" negotiation auto")

                    if is_mpls_core_interface(intf_name, intf_data, is_provider):
                        if igp == "OSPF":
                            cmds.append(" ip ospf 1 area 0")
                        cmds.append(" mpls ip")
                    elif not is_provider and not faces_provider(intf_data, as_info, data) and igp == "OSPF":
                        cmds.append(" ip ospf 1 area 0")

                cmds.append(" no shutdown")

            if router_info.get("nat_map"):
                cmds.extend(nat_map_commands(router_info["nat_map"]))

            router_id = router_ids[router_name]

            if igp == "OSPF":
                cmds.extend([f"router ospf 1", f" router-id {router_id}"])
            elif igp == "RIP":
                cmds.extend(["router rip", " version 2"])
                for intf_name, intf_data in interfaces.items():
                    if not intf_data.get("ip") or "Loopback" in intf_name:
                        continue
                    if is_provider:
                        if intf_data.get("vrf"):
                            continue
                        line = rip_network_line(intf_data)
                        if line:
                            cmds.append(f"  network {line}")
                    else:
                        if faces_provider(intf_data, as_info, data):
                            continue
                        line = rip_network_line(intf_data)
                        if line:
                            cmds.append(f"  network {line}")
                cmds.append(" exit")

            if is_provider:
                cmds.extend(
                    [
                        f"router bgp {as_num_s}",
                        f" bgp router-id {router_id}",
                        " bgp log-neighbor-changes",
                    ]
                )
                if is_pe:
                    for other_router, other_rid in router_ids.items():
                        if other_router != router_name and other_router.startswith("PE"):
                            cmds.extend(
                                [
                                    f" neighbor {other_rid} remote-as {as_num_s}",
                                    f" neighbor {other_rid} update-source Loopback0",
                                    " address-family vpnv4",
                                    f"  neighbor {other_rid} activate",
                                    f"  neighbor {other_rid} send-community extended",
                                    " exit-address-family",
                                ]
                            )
                    for intf_name, intf_data in interfaces.items():
                        if "vrf" not in intf_data or "ip" not in intf_data:
                            continue
                        vrf_name = intf_data["vrf"]
                        ce_ip = remote_peer_ip(intf_data)
                        if not ce_ip:
                            continue
                        ce_as = str(intf_data.get("customer_as") or "")
                        if not ce_as:
                            ce_name = intf_data.get("peer", "")
                            ce_as = router_as_for_name(data, ce_name) or "65000"
                        cmds.extend(
                            [
                                f" address-family ipv4 vrf {vrf_name}",
                                f"  neighbor {ce_ip} remote-as {ce_as}",
                                f"  neighbor {ce_ip} activate",
                                " exit-address-family",
                            ]
                        )
            else:
                pe_as = str(as_info["upstream_as"])
                cmds.extend(
                    [
                        f"router bgp {as_num_s}",
                        " bgp log-neighbor-changes",
                        " address-family ipv4",
                    ]
                )
                for intf_name, intf_data in interfaces.items():
                    if not intf_data.get("ip"):
                        continue
                    if not faces_provider(intf_data, as_info, data):
                        prefix, mask = bgp_network_for_interface(intf_data)
                        if prefix:
                            cmds.append(f"  network {prefix} mask {mask}")
                    else:
                        pe_ip = remote_peer_ip(intf_data)
                        if pe_ip:
                            cmds.extend(
                                [
                                    f"  neighbor {pe_ip} remote-as {pe_as}",
                                    f"  neighbor {pe_ip} activate",
                                ]
                            )
                            if intf_data.get("allowas_in"):
                                cmds.append(f"  neighbor {pe_ip} allowas-in")
                cmds.append(" exit-address-family")

            build_cmds[router_name] = cmds
    return build_cmds


def push_to_router(name, port, commands):
    print(f"[{name}] Connexion sur port {port}...")
    try:
        tn = telnetlib.Telnet(HOST, port, timeout=5)
        tn.write(b"\r\n\r\n")
        time.sleep(1)
        tn.write(b"configure terminal\r\n")
        time.sleep(0.5)
        for cmd in commands:
            tn.write(cmd.encode("ascii") + b"\r\n")
            time.sleep(0.02)
        tn.write(b"end\r\n")
        tn.write(b"write memory\r\n")
        time.sleep(1)
        tn.close()
        print(f"[{name}] -> {len(commands)} commandes injectees avec succes !")
    except Exception as e:
        print(f"[{name}] ERREUR Telnet: {e}")


def main():
    print("=== Demarrage du SDN Controller ===")
    consoles = get_gns3_consoles(GNS3_PROJECT_DIR)
    if not consoles:
        return

    state = {}
    if os.path.exists("state.json"):
        with open("state.json", "r") as f:
            state = json.load(f)

    with open("intent.json", "r") as f:
        intent = json.load(f)

    print("1. Calcul du Delta (Diff)...")
    teardown = generate_teardown_commands(state, intent)
    build = generate_build_commands(intent)

    print("2. Provisionning en direct (Live Push)...")
    for router_name, port in consoles.items():
        if router_name in build or router_name in teardown:
            final_cmds = teardown.get(router_name, []) + build.get(router_name, [])
            push_to_router(router_name, port, final_cmds)

    with open("state.json", "w") as f:
        json.dump(intent, f, indent=4)

    print("=== Provisionning Termine sans aucun redemarrage ! ===")


if __name__ == "__main__":
    main()
