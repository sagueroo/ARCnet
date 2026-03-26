import json
import ipaddress
import os

def cidr_to_netmask(cidr):
    """Convertit une notation CIDR (ex: /30) en masque de sous-réseau (ex: 255.255.255.252)"""
    # On ajoute une IP factice pour utiliser la librairie ipaddress
    network = ipaddress.IPv4Network(f"0.0.0.0{cidr}", strict=False)
    return str(network.netmask)

def main():
    # 1. Charger le fichier d'intention
    with open('intent.json', 'r') as f:
        data = json.load(f)

    # Compteur pour générer automatiquement les Loopbacks (1.1.1.1, 2.2.2.2, etc.)
    loopback_counter = 1

    # 2. Parcourir les AS
    for as_num, as_info in data['AS'].items():
        is_provider = as_info.get('ldp', False) # True pour le Core (3215), False pour le Client (102)
        
        # 3. Parcourir les routeurs de chaque AS
        for router_name, router_info in as_info['routers'].items():
            config = []
            
            # --- En-tête standard Cisco ---
            config.append("!")
            config.append("version 15.2")
            config.append(f"hostname {router_name}")
            config.append("!")
            config.append("ip cef")
            config.append("no ipv6 cef")
            config.append("!")

            interfaces = router_info.get('interfaces', {})
            
            # --- Configuration des Interfaces ---
            for intf_name, intf_data in interfaces.items():
                config.append(f"interface {intf_name}")
                
                # Gestion dynamique des Loopbacks
                if "Loopback" in intf_name:
                    ip = f"{loopback_counter}.{loopback_counter}.{loopback_counter}.{loopback_counter}"
                    mask = cidr_to_netmask(intf_data['mask'])
                    config.append(f" ip address {ip} {mask}")
                    if is_provider:
                        config.append(f" ip ospf 1 area 0")
                    loopback_counter += 1
                
                # Gestion des interfaces physiques
                elif intf_data.get('ipv4'):
                    ip = intf_data['ipv4']
                    mask = cidr_to_netmask(intf_data['mask'])
                    config.append(f" ip address {ip} {mask}")
                    config.append(" negotiation auto")
                    
                    # Activation OSPF et LDP pour le coeur de réseau (Provider)
                    if is_provider:
                        # On n'active pas LDP/OSPF sur l'interface qui pointe vers le client (CE)
                        if "CE" not in intf_data.get('ngbr', ''):
                            config.append(" ip ospf 1 area 0")
                            config.append(" mpls ip")
                
                config.append(" no shutdown")
                config.append("!")

            # --- Configuration du Routage ---
            if is_provider:
                # Configuration OSPF pour les PE et P
                config.append("router ospf 1")
                router_id = loopback_counter - 1
                config.append(f" router-id {router_id}.{router_id}.{router_id}.{router_id}")
                config.append("!")
            else:
                # Configuration BGP pour le Client (basé sur ton exemple CE1)
                config.append(f"router bgp {as_num}")
                config.append(" bgp log-neighbor-changes")
                
                # Ajout du réseau client
                cust_net = as_info['network']['prefix']
                config.append(f" network {cust_net}")
                
                # Configuration du voisin PE
                # (Dans un script plus avancé, on irait chercher l'IP du voisin dynamiquement, 
                # ici on extrait le AS du provider depuis le JSON)
                pe_as = list(as_info.get('ngbr_AS', {}).keys())[0]
                
                # Trouver l'IP du PE (On déduit l'IP du voisin d'après le sous-réseau)
                for intf_name, intf_data in interfaces.items():
                    if "PE" in intf_data.get('ngbr', ''):
                        # Si le routeur client a l'IP .2, le PE a l'IP .1
                        pe_ip = intf_data['ipv4'][:-1] + "1" 
                        config.append(f" neighbor {pe_ip} remote-as {pe_as}")
                config.append("!")

            # --- Lignes de Console (issues de ton startup-config) ---
            config.append("line con 0")
            config.append(" exec-timeout 0 0")
            config.append(" privilege level 15")
            config.append(" logging synchronous")
            config.append("!")
            config.append("end")

            # 4. Sauvegarde dans un fichier texte
            filename = f"{router_name}_config.cfg"
            with open(filename, 'w') as f_out:
                f_out.write("\n".join(config))
            print(f"Fichier généré avec succès : {filename}")

if __name__ == '__main__':
    main()