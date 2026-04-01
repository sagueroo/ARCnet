import json
import ipaddress
import os
import telnetlib
import time

GNS3_PROJECT_DIR = r"./TEST_SCRIPT" # Adapte ton chemin si besoin
HOST = "127.0.0.1"

def cidr_to_netmask(cidr):
    network = ipaddress.IPv4Network(f"0.0.0.0{cidr}", strict=False)
    return str(network.netmask)

def get_gns3_consoles(project_dir):
    mapping = {}
    gns3_files = [f for f in os.listdir(project_dir) if f.endswith('.gns3')]
    if not gns3_files: return mapping
    with open(os.path.join(project_dir, gns3_files[0]), 'r') as f:
        gns3_data = json.load(f)
    for node in gns3_data.get('topology', {}).get('nodes', []):
        name = node.get('name')
        console = node.get('console')
        if name and console and node.get('node_type') == 'dynamips':
            mapping[name] = console
    return mapping

def generate_teardown_commands(state, intent):
    teardown_cmds = {}
    for old_as, old_info in state.get('AS', {}).items():
        for router_name, old_router in old_info.get('routers', {}).items():
            cmds = []
            new_info = intent.get('AS', {}).get(old_as, {})
            new_router = new_info.get('routers', {}).get(router_name, {})
            
            if not new_info: cmds.append(f"no router bgp {old_as}")
            
            if old_info.get('vrfs'):
                new_vrfs = new_info.get('vrfs', {})
                for vrf_name in old_info['vrfs']:
                    if vrf_name not in new_vrfs:
                        cmds.append(f"no vrf definition {vrf_name}")
            
            old_intfs = old_router.get('interfaces', {})
            new_intfs = new_router.get('interfaces', {})
            for intf_name in old_intfs:
                if intf_name not in new_intfs:
                    cmds.append(f"default interface {intf_name}")
                    cmds.append(f"interface {intf_name}")
                    cmds.append(" shutdown")
                    cmds.append(" exit")

            if cmds: teardown_cmds[router_name] = cmds
    return teardown_cmds

def generate_build_commands(data):
    build_cmds = {}
    loopbacks = {}
    lb_c1, lb_c2, lb_c3 = 1, 1, 1
    for as_num, as_info in data['AS'].items():
        for router_name, router_info in as_info['routers'].items():
            for intf_name in router_info.get('interfaces', {}):
                if "Loopback" in intf_name:
                    loopbacks[router_name] = f"1.{lb_c3}.{lb_c2}.{lb_c1}"
                    if lb_c1 < 255: lb_c1 += 1
                    elif lb_c1 == 255 and lb_c2 < 255: lb_c2 += 1; lb_c1 = 1

    for as_num, as_info in data['AS'].items():
        is_provider = as_info.get('ldp', False)
        igp = as_info.get('igp', 'OSPF') # Integration de l'IGP
        
        for router_name, router_info in as_info['routers'].items():
            cmds = ["ip cef", "no ipv6 cef"]
            
            if is_provider and 'vrfs' in as_info:
                for vrf_name, vrf_data in as_info['vrfs'].items():
                    cmds.extend([
                        f"vrf definition {vrf_name}",
                        f" rd {vrf_data['rd']}",
                        " address-family ipv4"
                    ])
                    for rt in vrf_data.get('rt_export', []):
                        cmds.append(f"  route-target export {rt}")
                    for rt in vrf_data.get('rt_import', []):
                        cmds.append(f"  route-target import {rt}")
                    cmds.append(" exit-address-family")

            interfaces = router_info.get('interfaces', {})
            is_pe = any('vrf' in intf for intf in interfaces.values())
            
            # --- INTERFACES (Avec logique OSPF/RIP fusionnee) ---
            for intf_name, intf_data in interfaces.items():
                cmds.append(f"interface {intf_name}")
                if intf_data.get('vrf'): cmds.append(f" vrf forwarding {intf_data['vrf']}")
                if intf_data.get('nat'): cmds.append(f" ip nat {intf_data['nat']}")
                
                if "Loopback" in intf_name:
                    ip = loopbacks.get(router_name, "1.1.1.1")
                    mask = cidr_to_netmask(intf_data['mask'])
                    cmds.append(f" ip address {ip} {mask}")
                    if is_provider and igp == "OSPF": cmds.append(" ip ospf 1 area 0")
                elif intf_data.get('ipv4'):
                    ip = intf_data['ipv4']
                    mask = cidr_to_netmask(intf_data['mask'])
                    cmds.append(f" ip address {ip} {mask}")
                    cmds.append(" negotiation auto")
                    
                    if is_provider and "CE" not in intf_data.get('ngbr', ''):
                        if igp == "OSPF": cmds.append(" ip ospf 1 area 0")
                        cmds.append(" mpls ip")
                    elif not is_provider and "PE" not in intf_data.get('ngbr', '') and igp == "OSPF":
                        cmds.append(" ip ospf 1 area 0")
                        
                cmds.append(" no shutdown")

            if router_info.get('nat_static'): cmds.append(router_info['nat_static'])
            if router_info.get('static_routes'):
                for route in router_info['static_routes']: cmds.append(route)

            router_id = loopbacks.get(router_name, "1.1.1.1")

            # --- ROUTAGE IGP (OSPF vs RIP) ---
            if igp == "OSPF":
                cmds.extend([f"router ospf 1", f" router-id {router_id}"])
            elif igp == "RIP":
                cmds.extend(["router rip", " version 2"])
                for intf_name, intf_data in interfaces.items():
                    target_ngbr = "CE" if is_provider else "PE"
                    if target_ngbr not in intf_data.get('ngbr', '') and 'network' in intf_data:
                        cmds.append(f"  network {intf_data['network']['prefix']}")
                cmds.append(" exit")

            # --- ROUTAGE BGP ---
            if is_provider:
                cmds.extend([f"router bgp {as_num}", f" bgp router-id {router_id}", " bgp log-neighbor-changes"])
                if is_pe:
                    for other_router, other_lb in loopbacks.items():
                        if other_router != router_name and other_router.startswith("PE"):
                            cmds.extend([
                                f" neighbor {other_lb} remote-as {as_num}",
                                f" neighbor {other_lb} update-source Loopback0",
                                " address-family vpnv4",
                                f"  neighbor {other_lb} activate",
                                f"  neighbor {other_lb} send-community extended",
                                " exit-address-family"
                            ])
                    for intf_name, intf_data in interfaces.items():
                        if 'vrf' in intf_data:
                            vrf_name = intf_data['vrf']
                            ip_parts = intf_data['ipv4'].split('.')
                            ip_parts[-1] = str(int(ip_parts[-1]) + 1)
                            ce_ip = ".".join(ip_parts)
                            
                            ce_name = intf_data.get('ngbr', '')
                            ce_as = "102"
                            for test_as, test_info in data['AS'].items():
                                if ce_name in test_info.get('routers', {}):
                                    ce_as = test_as
                                    break

                            cmds.extend([
                                f" address-family ipv4 vrf {vrf_name}",
                                f"  neighbor {ce_ip} remote-as {ce_as}",
                                f"  neighbor {ce_ip} activate",
                                " exit-address-family"
                            ])
            else:
                cmds.extend([f"router bgp {as_num}", " bgp log-neighbor-changes", " address-family ipv4"])
                pe_as = list(as_info.get('ngbr_AS', {}).keys())[0]
                for intf_name, intf_data in interfaces.items():
                    if "network" in intf_data and "PE" not in intf_data.get('ngbr', ''):
                        prefix = intf_data['network']['prefix']
                        mask = cidr_to_netmask(intf_data['mask'])
                        cmds.append(f"  network {prefix} mask {mask}")
                    if "PE" in intf_data.get('ngbr', ''):
                        ip_parts = intf_data['ipv4'].split('.')
                        ip_parts[-1] = str(int(ip_parts[-1]) - 1)
                        pe_ip = ".".join(ip_parts)
                        cmds.extend([f"  neighbor {pe_ip} remote-as {pe_as}", f"  neighbor {pe_ip} activate"])
                        if intf_data.get('allowas_in'):
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
            tn.write(cmd.encode('ascii') + b"\r\n")
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
    if not consoles: return

    state = {}
    if os.path.exists('state.json'):
        with open('state.json', 'r') as f:
            state = json.load(f)
            
    with open('intent.json', 'r') as f:
        intent = json.load(f)

    print("1. Calcul du Delta (Diff)...")
    teardown = generate_teardown_commands(state, intent)
    build = generate_build_commands(intent)

    print("2. Provisionning en direct (Live Push)...")
    for router_name, port in consoles.items():
        if router_name in build or router_name in teardown:
            final_cmds = teardown.get(router_name, []) + build.get(router_name, [])
            push_to_router(router_name, port, final_cmds)

    with open('state.json', 'w') as f:
        json.dump(intent, f, indent=4)
        
    print("=== Provisionning Termine sans aucun redemarrage ! ===")

if __name__ == '__main__':
    main()