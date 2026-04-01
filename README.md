# ARCnet

Automatisation du provisionnement d’un réseau **BGP / MPLS L3VPN** à partir d’un fichier d’**intention** JSON. Le contrôleur génère des commandes Cisco IOS et les applique en **Telnet** sur les routeurs GNS3, sans rechargement des nœuds.

Projet de module **NAS** (3TC) — prolongement logique d’un lab **GNS** (cœur IP) vers **MPLS, VPNv4, VRF et PE–CE**.

---

## Prérequis

- **Python 3** (bibliothèque standard : `json`, `ipaddress`, `telnetlib`, pas de `pip` obligatoire).
- **GNS3** avec topologie **Dynamips** ; les **noms des routeurs** dans GNS3 doivent **correspondre exactement** aux clés `routers` de `intent.json` (ex. `PE1`, `PC1`, `CE`, …).
- Répertoire du projet contenant le dossier **`GNS/`** avec le fichier **`.gns3`** du lab.

---

## Démarrage rapide

1. Ouvrir le projet dans GNS3 et **démarrer** tous les routeurs.
2. Depuis la racine du dépôt (là où se trouvent `intent.json` et `sdn_controller.py`) :

```bash
python sdn_controller.py
```

3. Le script lit `GNS/*.gns3`, récupère les **ports console**, pousse la configuration, puis enregistre l’intention courante dans `state.json`.

**Hôte Telnet :** par défaut `127.0.0.1` (GNS3 en local). Adapter `HOST` dans `sdn_controller.py` si besoin.

**Chemin GNS3 :** par défaut `GNS3_PROJECT_DIR = "./GNS"`. Modifier la constante en tête de `sdn_controller.py` si votre arborescence diffère.

---

## Fichiers principaux

| Fichier | Rôle |
|--------|------|
| `intent.json` | Source de vérité : AS, routeurs, interfaces, VRF, NAT, etc. |
| `sdn_controller.py` | Génération des commandes IOS + push Telnet + gestion du diff avec `state.json`. |
| `state.json` | **Mémoire du dernier déploiement** : comparaison avec `intent.json` pour générer un **teardown** partiel (suppression de VRF, interfaces, AS retirés). Créé/mis à jour automatiquement. |
| `GNS/` | Projet GNS3 (topologie, configs Dynamips). |

En cas de **changement majeur** de schéma ou de repartir de zéro sur les routeurs, supprimer `state.json` avant un nouveau run évite un teardown incohérent avec l’ancien modèle.

---

## Modèle d’intention (`intent.json`)

La racine contient un objet **`AS`** : chaque clé est un **numéro d’AS** (chaîne), chaque valeur décrit ce AS.

### Fournisseur (cœur MPLS / PE)

- `igp` : `"OSPF"` ou `"RIP"`.
- `mpls` : `true` active **OSPF + `mpls ip`** sur les liens cœur (interfaces **sans** VRF, hors loopback).
- `vrfs` : dictionnaire **nom de VRF** → objet :
  - `customer_as` : AS du client (sert à former RD et RT d’export : `{AS_fournisseur}:{customer_as}`).
  - `import_customers` : liste d’AS (ou RT complets avec `:`) pour les **route-target import** additionnels (partage de routes entre VPN).
  - `rd` *(optionnel)* : surcharge du **Route Distinguisher**.
- `routers` : pour chaque routeur, `interfaces` avec au minimum :
  - Liens numérotés : `"ip": "A.B.C.D/prefix"`, `"peer": "NomRouteurVoisin"`.
  - Lien **PE–CE** côté PE : ajouter `"vrf": "NomVRF"` ; `customer_as` sur l’interface est optionnel (sinon déduit via le nom du routeur `peer`).
  - **Loopback** cœur : `"Loopback0": { }` (masque `/32` par défaut) ; l’adresse est **attribuée automatiquement** (voir ci-dessous).

### Clients (CE)

- `upstream_as` : AS du fournisseur (pour le voisin **eBGP** vers le PE).
- `igp` : `"OSPF"` ou `"RIP"`.
- `routers` : interfaces avec `ip`, `peer` vers le PE pour le lien WAN ; champs optionnels :
  - `nat` : `"inside"` / `"outside"` sur l’interface.
  - `allowas_in` : `true` sur le lien vers le PE si nécessaire.
  - `announce` : préfixe CIDR **annoncé en BGP** lorsqu’il diffère du réseau de l’interface (ex. préfixe « public » derrière NAT).
- `nat_map` au niveau routeur *(optionnel)* :  
  `"nat_map": { "local": "192.168.x.0/24", "global": "10.x.x.0/24" }`  
  génère la **NAT statique réseau** IOS et une route vers **Null0** pour le préfixe global.

### Router-id et loopbacks

Tous les routeurs reçoivent un **router-id unique** sous forme d’adresses **incrémentales** dans la plage **`10.200.0.1`**, `10.200.0.2`, … (à partir de `10.200.0.0` + index), selon l’**ordre** des AS puis des routeurs dans `intent.json`. Ce schéma **n’est pas limité à 255 routeurs** (contrairement à une séquence du type `1.1.1.1`, `2.2.2.2`, …). La même valeur sert d’adresse **Loopback0** lorsque cette interface est définie. Les CE **sans** loopback dans le JSON utilisent quand même ce router-id pour **OSPF / BGP**.

---

## Correspondance avec le sujet NAS (rappel)

| Thème | Réalisation dans ARCnet |
|--------|-------------------------|
| Cœur IP (OSPF, loopbacks) | AS fournisseur, loopbacks auto, OSPF area 0 sur liens cœur. |
| MPLS sur le cœur | `mpls ip` sur interfaces cœur ; à compléter en lab si besoin (LDP explicite selon l’image IOS). |
| iBGP **VPNv4** PE–PE | Sessions vers loopbacks des routeurs dont le nom commence par `PE`. |
| VRF / PE–CE **eBGP** | VRF + `address-family ipv4 vrf` + voisin déduit du lien / AS client. |
| Intent & automatisation | Édition de `intent.json` + exécution du contrôleur. |
| Évolutions sans reboot | Push incrémental Telnet ; `state.json` pour partie du diff. |

---

## Vérifications utiles en démo (IOS)

À adapter aux noms de VRF et d’interfaces du lab.

```text
show ip ospf neighbor
show ip bgp summary
show ip bgp vpnv4 all summary
show ip route vrf <VRF>
show mpls forwarding-table
ping vrf <VRF> <adresse>
```

---

## Limitations connues

- Le **teardown** ne retire pas toutes les subtilités possibles d’IOS (voisins BGP modifiés, etc.) : pour un gros changement, repartir d’une config propre ou effacer `state.json` peut être nécessaire.
- **`generate_configs.py`** (si présent) n’est **pas** aligné sur le schéma actuel de `intent.json` ; le flux supporté est **`sdn_controller.py` + Telnet**.

---

## Auteurs

Projet **ARCnet** — équipe du module NAS (INSA / 3TC).
