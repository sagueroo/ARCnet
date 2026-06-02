# ARCnet

Provisionnement **BGP / MPLS L3VPN** depuis un intent JSON : validation, allocation IP, generation IOS, push **Telnet** sur GNS3.

Projet **NAS** (3TC) — coeur MPLS, VRF clients, hub SopraSteria, Ingress TE, Internet simule.

**Intent de reference :** [`intent.example.json`](intent.example.json) (copie locale : `intent.json`).

---

## Demarrage

**Prerequis :** Python 3 (stdlib), GNS3 Dynamips, noms de routeurs = cles `routers` de l'intent, routeur **`Internet`** sur **PE1 Gi5/0**, dossier `GNS/`.

```bash
python sdn_controller.py validate
python sdn_controller.py diff
python sdn_controller.py apply
python sdn_controller.py apply --only PE1,CE2,CE3
python sdn_controller.py reset
python -m unittest test_sdn_controller -v
```

Sans argument : `python sdn_controller.py` = `apply`.

### Commandes

| Commande | Role |
|----------|------|
| `validate` | Controle l'intent, affiche router-id et liens detectes |
| `diff` | Affiche teardown + build sans push |
| `apply` | Push Telnet (`state.json` cree) ; attente du prompt `#` entre commandes |
| `reset` | Demontage IOS + reecriture des `*_startup-config.cfg` (template + hostname) |
| `--intent fichier.json` | Intent (defaut : `intent.json`) |
| `--only PE1,CE` | Sous-ensemble de routeurs |

**Options `reset` :**

| Option | Role |
|--------|------|
| (defaut) | Telnet (inverse de l'intent + supprime VRF `Shared`) + template startup |
| `--files-only` | Seulement les `.cfg` Dynamips (routeurs peuvent etre eteints) |
| `--telnet-only` | Seulement le demontage Telnet |
| `--keep-state` | Garde `state.json` |

**Apres `reset` :** dans GNS3, **Stop** puis **Start** les noeuds (obligatoire). Puis `apply`.  
`write erase` / `reload` ne sont pas utilises (peu fiables sous Dynamips).

---

## Intent (resume)

| Bloc | Contenu |
|------|---------|
| `addressing` | Pools loopback / coeur / PE-CE ; `"ip"` manuel = override |
| `AS` / `3215` | OSPF, MPLS, VRF (`Arsium`, `EuroInfo`, `SopraSteria`), `import_customers` |
| `AS` / clients | `upstream_as`, `nat_map`, `allowas_in`, `prepend`, IGP RIP/OSPF |
| `internet` | PE1 Gi5/0 ↔ `Internet` (AS 65000), `203.0.113.0/24`, test `ping 203.0.113.1` |

Champs utiles sur les CE :

| Champ | Role |
|-------|------|
| `nat_map` | NAT statique LAN → global (hub SopraSteria) |
| `announce` | Prefixe global BGP supplementaire (ex. `10.2.67.0/24`) |
| `prepend` | Ingress TE (AS-path allonge vers un PE backup) |

Si **`ip`** (LAN) et **`announce`** (global) sont tous deux presents sur une interface LAN, le controleur annonce **les deux** en BGP : le LAN pour les pings inter-sites (ex. CE2 → CE3 en `192.168.69.254`), le global pour le hub.

---

## Comportements importants

### NAT

- **CE / CE2** — `nat_map` : `192.168.67.0/24` → `10.1.67.0/24` ou `10.2.67.0/24` (hub, pas Internet).
- **Internet** — NAT **overload par VRF sur PE1** (`ip nat … interface Gi5/0 vrf <nom> overload`).
- **CE4** — pas de `nat_map` ; dual-home PE1 + PE2.

### CE4 (Ingress TE + Internet)

- `prepend: 2` sur le lien **PE2** → trafic **entrant** prefere PE1.
- Internet et NAT : **PE1 uniquement** → `ip route 0.0.0.0 0.0.0.0` vers PE1 sur CE4 ; pas de `default-originate` PE2 → CE4.

### Renommage VRF `Shared` → `SopraSteria`

- Chaque **`apply` sur un PE** : `no vrf definition Shared`, recreation propre des VRF/BGP.
- **`reset`** : supprime aussi les VRF legacy avant rebuild.

### Reset / fichiers Dynamips

Les startup-config sont reecrites avec un **template minimal** et `hostname <nom du noeud GNS3>` (ex. `hostname PE1`), dans :

`GNS/project-files/dynamips/<node_id>/configs/*_startup-config.cfg`

---

## Tests de connectivite

| Test | Attendu |
|------|---------|
| CE → CE1 | `ping 192.168.69.254` (VRF Arsium, LAN annonce) |
| CE2 → CE3 | `ping 192.168.69.254` (VRF EuroInfo, LAN + `announce`) |
| CE → CE2 | **Echec** (VRF differentes) ou ping vers `10.2.67.x` selon routes |
| CE / CE2 → hub | `ping 172.16.100.254` depuis CE4 (SopraSteria) |
| Internet | `ping 203.0.113.1` depuis un CE |

```text
show ip route
show ip bgp vpnv4 all summary
show vrf
PE1# show ip nat translations
```

---

## Fichiers

| Fichier | Role |
|---------|------|
| `intent.example.json` | Reference versionnee |
| `intent.json` | Intent local |
| `sdn_controller.py` | Controleur |
| `state.json` | Dernier apply (genere, non versionne) |
| `test_sdn_controller.py` | 32 tests unitaires |
| `GNS/` | Projet GNS3 + `project-files/dynamips/` |

---

## Sujet NAS (synthese)

| Theme | Statut |
|-------|--------|
| OSPF, MPLS LDP, VPNv4, VRF PE-CE | Oui |
| Multi-RT / hub SopraSteria, NAT client | Oui |
| Manageability (`validate`, `diff`, `apply`, `reset`, `state.json`) | Oui |
| Ingress TE (CE4 dual-home) | Oui |
| Internet (NAT VRF PE1 + routeur ISP) | Oui |
| RSVP | Non |

---

## Auteurs

Mèjdi, Hugo, Marc et Mehdi — projet ARCnet, module NAS (INSA / 3TC).
