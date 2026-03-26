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

    loopback_counter = 1
    loopback_cnt2 = 1
    loopback_cnt3 = 1


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

            interfaces = router_info.get('interfaces', {})
            
            for intf_name, intf_data in interfaces.items():
                config.append(f"interface {intf_name}")
                
                if "Loopback" in intf_name:
                    ip = f"1.{loopback_cnt3}.{loopback_cnt2}.{loopback_counter}"
                    mask = cidr_to_netmask(intf_data['mask'])
                    config.append(f" ip address {ip} {mask}")
                    if is_provider:
                        config.append(f" ip ospf 1 area 0")
                    if loopback_counter < 255:
                        loopback_counter += 1
                    if loopback_counter == 255 and loopback_cnt2 < 255:
                        loopback_cnt2 += 1
                        loopback_counter = 1
                    if loopback_counter == 255 and loopback_cnt2 == 255:
                        loopback_cnt3 +=1
                        loopback_counter = 1
                        loopback_cnt2 = 1
                    if loopback_cnt3 == 255:
                        return "too much loopbacks"
                
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

            if is_provider:
                router_id = loopback_counter - 1
                config.extend([
                    "router ospf 1",
                    f" router-id {router_id}.{router_id}.{router_id}.{router_id}",
                    "!"
                ])
            else:
                config.extend([
                    f"router bgp {as_num}",
                    " bgp log-neighbor-changes"
                ])
                
                cust_net = as_info.get('network', {}).get('prefix')
                if cust_net:
                    config.append(f" network {cust_net}")
                
                pe_as = list(as_info.get('ngbr_AS', {}).keys())[0]
                
                for intf_name, intf_data in interfaces.items():
                    if "PE" in intf_data.get('ngbr', ''):
                        pe_ip = intf_data['ipv4'][:-1] + "1" 
                        config.append(f" neighbor {pe_ip} remote-as {pe_as}")
                config.append("!")

            config.extend([
                "line con 0",
                " exec-timeout 0 0",
                " privilege level 15",
                " logging synchronous",
                "!",
                "end"
            ])

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