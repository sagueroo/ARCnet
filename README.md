# ARCnet

Automatisation du provisionnement d'un reseau **BGP / MPLS L3VPN** a partir d'un fichier d'**intention** JSON. Le controleur genere des commandes Cisco IOS et les applique en **Telnet** sur les routeurs GNS3, sans rechargement des noeuds.

Projet de module **NAS** (3TC) -- prolongement logique d'un lab **GNS** (coeur IP) vers **MPLS, VPNv4, VRF et PE-CE**.

---

## Fichier d'entree (intent)

**Fichier d'exemple a consulter sur GitHub : [`intent.example.json`](intent.example.json)**

C'est la reference du format d'intention du projet (topologie complete : coeur MPLS, 3 clients, NAT, multihoming Ingress TE). Le controleur utilise par defaut `intent.json` (meme contenu en local) ; vous pouvez pointer vers l'exemple avec `--intent intent.example.json`.

---

## Fonctionnalites

### Reseau (sujet NAS)

| Fonctionnalite | Description |
|----------------|-------------|
| Coeur OSPF | PE1 -- PC1 -- PC2 -- PE2, loopbacks, area 0 |
| MPLS / LDP | `mpls ip` sur les liens coeur |
| iBGP VPNv4 | Sessions PE-PE via loopbacks |
| VRF L3VPN | RD/RT automatiques, eBGP PE-CE par VRF |
| Multi-RT / partage | VRF `SopraSteria` avec `import_customers` |
| NAT client | `nat_map` (NAT statique reseau + route Null0) |
| RIP / OSPF client | IGP configurable par AS client |
| `allowas-in` | CE multi-sites (meme AS annonce par plusieurs PE) |
| `redistribute connected` (BGP VRF) | Liens PE-CE annonces dans le VPN (retour ping) |
| Ingress TE (Phase 4.b) | CE dual-home + `prepend` AS-path (lien backup) |

### Logiciel

| Fonctionnalite | Description |
|----------------|-------------|
| Intent JSON simplifie | Peers, VRF, pools -- peu d'IPs en dur |
| Allocation IP dynamique | Pools `addressing` + override manuel par interface |
| Validation d'intent | Erreurs claires avant push (`validate`) |
| CLI | `validate`, `diff`, `apply`, `--only`, `--intent` |
| Mises a jour incrementales | `state.json` + teardown (add / delete / update) |
| Router-ID uniques | Pool loopback (`10.200.0.x`), scalable |
| Tests unitaires | `test_sdn_controller.py` (27 tests) |

---

## Prerequis

- **Python 3** (bibliotheque standard uniquement, pas de `pip`).
- **GNS3** avec topologie **Dynamips** ; les **noms des routeurs** dans GNS3 doivent correspondre aux cles `routers` de l'intent.
- Repertoire du projet contenant le dossier **`GNS/`** avec le fichier `.gns3` du lab.

---

## Demarrage rapide

1. Ouvrir le projet dans GNS3 et **demarrer** tous les routeurs.
2. Depuis la racine du depot :

```bash
# Valider l'intent (pas de push)
python sdn_controller.py validate

# Voir les commandes qui seraient envoyees
python sdn_controller.py diff

# Pousser la config sur tous les routeurs
python sdn_controller.py apply

# Utiliser le fichier d'exemple officiel
python sdn_controller.py validate --intent intent.example.json

# Pousser sur certains routeurs seulement
python sdn_controller.py apply --only PE1,CE
```

Sans sous-commande, `python sdn_controller.py` equivaut a `apply`.

### Tests

```bash
python -m unittest test_sdn_controller -v
```

---

## Sous-commandes CLI

| Commande | Description |
|----------|-------------|
| `validate` | Verifie la coherence de l'intent, affiche les router-id et les liens detectes. |
| `diff` | Genere et affiche les commandes IOS (teardown + build) sans toucher aux routeurs. |
| `diff --only PE1,CE` | Idem, filtre sur certains routeurs. |
| `apply` | Pousse la configuration sur tous les routeurs via Telnet. |
| `apply --only PE1,CE` | Pousse uniquement sur les routeurs indiques. |
| `--intent fichier.json` | Fichier intent (defaut : `intent.json`, exemple : `intent.example.json`). |

---

## Fichiers principaux

| Fichier | Role |
|---------|------|
| **`intent.example.json`** | **Fichier d'entree d'exemple** (reference GitHub). |
| `intent.json` | Intent utilise en local par defaut. |
| `sdn_controller.py` | Validation + allocation IP + generation IOS + push Telnet + diff. |
| `state.json` | Memoire du dernier deploiement (genere par `apply`, non versionne). |
| `test_sdn_controller.py` | Tests unitaires. |
| `GNS/` | Projet GNS3 (topologie, configs Dynamips). |

---

## Modele d'intention (`intent.json` / `intent.example.json`)

### Allocation IP dynamique

Le bloc `addressing` (optionnel) definit des **pools** d'adresses. Quand il est present, les interfaces P2P **sans** champ `ip` recoivent une adresse automatiquement :

```json
"addressing": {
    "loopback_pool": "10.200.0.0/24",
    "core_pool": "10.0.10.0/24",
    "core_prefix": 30,
    "customer_pool": "192.168.0.0/22",
    "customer_prefix": 30
}
```

- **`loopback_pool`** : pool pour les router-id / Loopback0 (1 adresse par routeur).
- **`core_pool` + `core_prefix`** : sous-reseaux pour les liens P2P du coeur (PE-P, P-P).
- **`customer_pool` + `customer_prefix`** : sous-reseaux pour les liens PE-CE.

**Override manuel** : si un champ `"ip": "x.x.x.x/n"` est present sur une interface, il est conserve tel quel. Le pair deduit son adresse depuis le meme sous-reseau.

### Fournisseur (coeur MPLS)

- `igp` : `"OSPF"` ou `"RIP"`.
- `mpls` : `true` active OSPF + `mpls ip` sur les liens coeur.
- `vrfs` : dictionnaire **nom VRF** -> `{ "customer_as": "...", "import_customers": [...] }`.
- `routers` : interfaces avec `peer` (nom du voisin), `vrf` optionnel, `Loopback0: {}`.

### Clients (CE)

- `upstream_as` : AS du fournisseur.
- `nat_map` (optionnel) : `{ "local": "192.168.x.0/24", "global": "10.x.x.0/24" }`.
- Interfaces : `peer` vers le PE, `nat`, `allowas_in`, `announce` (prefixe BGP si different du LAN).
- **`prepend`** (optionnel) : nombre de repetitions de l'AS local sur une session eBGP (Ingress TE, lien backup).

### Exemple Ingress TE (CE4 dual-home)

```json
"CE4": {
    "interfaces": {
        "Gigabitethernet1/0": { "peer": "PE1", "allowas_in": true },
        "Gigabitethernet2/0": { "peer": "PE2", "allowas_in": true, "prepend": 2 },
        "Gigabitethernet3/0": { "ip": "172.16.100.254/24" }
    }
}
```

Le trafic entrant prefere PE1 (AS-path court) ; PE2 sert de backup si le lien primaire tombe.

---

## Validation

La commande `validate` verifie avant tout push :

- Structure JSON (cles `AS`, `routers`, `interfaces`).
- IGP valide (`OSPF` / `RIP`).
- `upstream_as` reference un AS existant pour les clients.
- Chaque `peer` pointe vers un routeur existant.
- Les VRF referees sur les interfaces sont declarees.
- Les adresses IP (quand presentes) sont du CIDR valide.
- Les pools `addressing` sont des reseaux IPv4 valides.

---

## Correspondance avec le sujet NAS

| Phase / theme | Realisation |
|---------------|-------------|
| Phase 0 -- Setup OSPF, loopbacks | Oui (allocation auto) |
| Phase 1 -- MPLS LDP | Oui (`mpls ip`) |
| Phase 2 -- iBGP VPNv4 | Oui (PE-PE loopback) |
| Phase 3 -- VRF, eBGP PE-CE | Oui (+ NAT, RIP, allowas-in) |
| Phase 4.a -- Manageability | Oui (`state.json`, teardown, CLI diff/apply) |
| Phase 4.b -- Site sharing (multi-RT) | Oui (`import_customers`, VRF SopraSteria) |
| Phase 4.b -- Ingress TE | Oui (CE4 dual-home + `prepend`) |
| Phase 4.b -- Internet services | Non implemente |
| Phase 4.b -- RSVP | Non implemente |

---

## Verifications utiles en demo (IOS)

```text
show ip ospf neighbor
show ip bgp summary
show ip bgp vpnv4 all summary
show ip route vrf <VRF>
show mpls forwarding-table
ping vrf <VRF> <adresse>
```

Ingress TE (CE4) :

```text
PE2# show ip bgp vpnv4 vrf SopraSteria 172.16.100.0
```

---

## Auteurs

Projet **ARCnet** -- equipe du module NAS (INSA / 3TC).
