import json
import ipaddress
import os
import glob

GNS3_PROJECT_DIR = r"./GNS"

def cidr_to_netmask(cidr):
    network = ipaddress.IPv4Network(f"0.0.0.0{cidr}", strict=False)
    return str(network.netmask)

def get_gns3_mapping(project_dir):
    mapping = {}
    gns3_files = [f for f in os.listdir(project_dir) if f.endswith('.gns3')]
    
    if not gns3_files:
        print(f"Erreur : Aucun fichier .gns3 trouve dans {project_dir}")
        return mapping
        
    gns3_file = os.path.join(project_dir, gns3_files[0])
    
    with open(gns3_file, 'r') as f:
        gns3_data = json.load(f)
        
    for node in gns3_data.get('topology', {}).get('nodes', []):
        name = node.get('name')
        node_id = node.get('node_id')
        if name and node_id and node.get('node_type') == 'dynamips':
            mapping[name] = node_id
            
    return mapping

def main():
    print("Analyse du projet GNS3...")
    gns3_mapping = get_gns3_mapping(GNS3_PROJECT_DIR)
    
    if not gns3_mapping:
        print("Veuillez verifier le chemin GNS3_PROJECT_DIR.")
        return

    with open('intent.json', 'r') as f:
        data = json.load(f)

    # 1. PRE-CALCUL DES LOOPBACKS
    # Necessaire pour connaitre les IP iBGP des voisins avant meme de generer leur config
    loopbacks = {}
    lb_c1, lb_c2, lb_c3 = 1, 1, 1
    for as_num, as_info in data['AS'].items():
        for router_name, router_info in as_info['routers'].items():
            for intf_name in router_info.get('interfaces', {}):
                if "Loopback" in intf_name:
                    loopbacks[router_name] = f"1.{lb_c3}.{lb_c2}.{lb_c1}"
                    if lb_c1 < 255: 
                        lb_c1 += 1
                    elif lb_c1 == 255 and lb_c2 < 255: 
                        lb_c2 += 1
                        lb_c1 = 1
                    elif lb_c1 == 255 and lb_c2 == 255: 
                        lb_c3 += 1
                        lb_c1 = 1
                        lb_c2 = 1

    print("Generation et injection des configurations...")
    
    for as_num, as_info in data['AS'].items():
        is_provider = as_info.get('ldp', False)
        
        for router_name, router_info in as_info['routers'].items():
            config = []
            
            config.extend([
                "!",
                "version 15.2",
                f"hostname {router_name}",
                "!",
                "ip cef",
                "no ipv6 cef",
                "!"
            ])

            # 2. DEFINITION DES VRF (Seulement pour les PE)
            if is_provider and 'vrfs' in as_info:
                for vrf_name, vrf_data in as_info['vrfs'].items():
                    config.extend([
                        f"vrf definition {vrf_name}",
                        f" rd {vrf_data['rd']}",
                        " !",
                        " address-family ipv4",
                        f"  route-target export {vrf_data['route_target']}",
                        f"  route-target import {vrf_data['route_target']}",
                        " exit-address-family",
                        "!"
                    ])

            interfaces = router_info.get('interfaces', {})
            is_pe = any('vrf' in intf for intf in interfaces.values())
            
            # 3. CONFIGURATION DES INTERFACES
            for intf_name, intf_data in interfaces.items():
                config.append(f"interface {intf_name}")
                
                # Affectation VRF (A FAIRE ABSOLUMENT AVANT L'IP)
                if intf_data.get('vrf'):
                    config.append(f" vrf forwarding {intf_data['vrf']}")
                
                if "Loopback" in intf_name:
                    ip = loopbacks.get(router_name, "1.1.1.1")
                    mask = cidr_to_netmask(intf_data['mask'])
                    config.append(f" ip address {ip} {mask}")
                    if is_provider:
                        config.append(" ip ospf 1 area 0")
                
                elif intf_data.get('ipv4'):
                    ip = intf_data['ipv4']
                    mask = cidr_to_netmask(intf_data['mask'])
                    config.append(f" ip address {ip} {mask}")
                    config.append(" negotiation auto")
                    
                    if is_provider and "CE" not in intf_data.get('ngbr', ''):
                        config.append(" ip ospf 1 area 0")
                        config.append(" mpls ip")
                
                config.append(" no shutdown")
                config.append("!")

            # 4. ROUTAGE PROVIDER (OSPF + iBGP VPNv4 + eBGP VRF)
            if is_provider:
                router_id = loopbacks.get(router_name, "1.1.1.1")
                config.extend([
                    "router ospf 1",
                    f" router-id {router_id}",
                    "!"
                ])
                
                config.extend([
                    f"router bgp {as_num}",
                    f" bgp router-id {router_id}",
                    " bgp log-neighbor-changes",
                ])
                
                if is_pe:
                    # Session iBGP VPNv4 avec les autres PE
                    for other_router, other_lb in loopbacks.items():
                        if other_router != router_name and other_router.startswith("PE"):
                            config.extend([
                                f" neighbor {other_lb} remote-as {as_num}",
                                f" neighbor {other_lb} update-source Loopback0",
                                " !",
                                " address-family vpnv4",
                                f"  neighbor {other_lb} activate",
                                f"  neighbor {other_lb} send-community extended",
                                " exit-address-family",
                                " !"
                            ])
                    
                    # Session eBGP avec le CE dans la VRF
                    for intf_name, intf_data in interfaces.items():
                        if 'vrf' in intf_data:
                            vrf_name = intf_data['vrf']
                            # Deduit l'IP du CE (ex: .1 devient .2)
                            ip_parts = intf_data['ipv4'].split('.')
                            ip_parts[-1] = str(int(ip_parts[-1]) + 1)
                            ce_ip = ".".join(ip_parts)
                            ce_as = list(as_info.get('ngbr_AS', {}).keys())[0]
                            
                            config.extend([
                                f" address-family ipv4 vrf {vrf_name}",
                                f"  neighbor {ce_ip} remote-as {ce_as}",
                                f"  neighbor {ce_ip} activate",
                                " exit-address-family",
                                " !"
                            ])

            # 5. ROUTAGE CUSTOMER (eBGP global + allowas-in)
            else:
                config.extend([
                    f"router bgp {as_num}",
                    " bgp log-neighbor-changes",
                    " !",
                    " address-family ipv4"
                ])
                
                pe_as = list(as_info.get('ngbr_AS', {}).keys())[0]
                
                for intf_name, intf_data in interfaces.items():
                    # Annonce des reseaux LAN
                    if "network" in intf_data and "CE" not in intf_data.get('ngbr', '') and "PE" not in intf_data.get('ngbr', ''):
                        prefix = intf_data['network']['prefix']
                        mask = cidr_to_netmask(intf_data['mask'])
                        config.append(f"  network {prefix} mask {mask}")
                    
                    # Voisinage avec le PE
                    if "PE" in intf_data.get('ngbr', ''):
                        # Deduit l'IP du PE (ex: .2 devient .1)
                        ip_parts = intf_data['ipv4'].split('.')
                        ip_parts[-1] = str(int(ip_parts[-1]) - 1)
                        pe_ip = ".".join(ip_parts)
                        
                        config.append(f"  neighbor {pe_ip} remote-as {pe_as}")
                        config.append(f"  neighbor {pe_ip} activate")
                        
                        if intf_data.get('allowas_in'):
                            config.append(f"  neighbor {pe_ip} allowas-in")
                
                config.extend([
                    " exit-address-family",
                    "!"
                ])

            config.extend([
                "line con 0",
                " exec-timeout 0 0",
                " privilege level 15",
                " logging synchronous",
                "!",
                "end"
            ])

            # 6. INJECTION GNS3
            if router_name in gns3_mapping:
                uuid = gns3_mapping[router_name]
                config_dir = os.path.join(GNS3_PROJECT_DIR, 'project-files', 'dynamips', uuid, 'configs')
                
                if os.path.exists(config_dir):
                    cfg_files = glob.glob(os.path.join(config_dir, '*_startup-config.cfg'))
                    if cfg_files:
                        target_file = cfg_files[0]
                        with open(target_file, 'w') as f_out:
                            f_out.write("\n".join(config))
                        print(f"{router_name:<4} -> Config injectee dans {uuid[:8]}... ({os.path.basename(target_file)})")
                    else:
                        print(f"{router_name:<4} -> Dossier trouve mais aucun fichier _startup-config.cfg existant.")
                else:
                     print(f"{router_name:<4} -> Dossier 'configs' introuvable pour cet UUID.")
            else:
                print(f"{router_name:<4} -> Non trouve dans le fichier GNS3.")

if __name__ == '__main__':
    main()