# ARCnet

Provisionnement **BGP / MPLS L3VPN** depuis un intent JSON : validation, allocation IP, commandes IOS, push **Telnet** sur GNS3 (sans reload des noeuds).

Projet **NAS** (3TC) — lab GNS etendu (MPLS, VPNv4, VRF, PE-CE, Internet simule).

**Intent de reference :** [`intent.example.json`](intent.example.json) (copie locale : `intent.json`).

---

## Demarrage

**Prerequis :** Python 3 (stdlib), GNS3 Dynamips, noms de routeurs = cles `routers` de l'intent, routeur **`Internet`** sur **PE1 Gi5/0**, dossier `GNS/`.

```bash
python sdn_controller.py validate
python sdn_controller.py diff
python sdn_controller.py apply
python sdn_controller.py apply --only PE1,CE4
python sdn_controller.py reset    # puis apply apres reboot (~1-2 min)
python -m unittest test_sdn_controller -v
```

| Commande | Role |
|----------|------|
| `validate` | Controle l'intent, affiche router-id et liens |
| `diff` | Affiche les commandes (teardown + build), sans push |
| `apply` | Push Telnet sur tous les routeurs (`state.json` cree) |
| `reset` | `write erase` + reload (efface `state.json` sauf `--keep-state`) |
| `--intent`, `--only` | Fichier intent / sous-ensemble de routeurs |

Sans argument : `python sdn_controller.py` = `apply`.

---

## Intent (resume)

| Bloc | Contenu |
|------|---------|
| `addressing` | Pools loopback / coeur / PE-CE ; `ip` manuel = override |
| `AS` / `3215` | Coeur OSPF, MPLS, VRF, PE-CE BGP, `import_customers` (hub SopraSteria) |
| `AS` / clients | `upstream_as`, `nat_map`, `prepend`, `allowas_in`, IGP RIP/OSPF |
| `internet` | PE1 Gi5/0 ↔ `Internet` (AS 65000), prefixe `203.0.113.0/24`, test `ping 203.0.113.1` |

**NAT :**
- **CE / CE2** — `nat_map` : LAN `192.168.67.0/24` → `10.1.x` / `10.2.x` (hub SopraSteria, pas Internet).
- **Internet** — NAT **overload par VRF sur PE1** (Gi5/0 outside, interfaces CE en inside).
- **CE4** — pas de `nat_map` (LAN `172.16.100.0/24`). Dual-home PE1+PE2 : Internet **uniquement via PE1** (defaut statique sur CE4, pas de defaut BGP PE2→CE4).

**CE4 Ingress TE :** `prepend: 2` sur le lien PE2 ; trafic entrant prefere PE1.

---

## Fichiers

| Fichier | Role |
|---------|------|
| `intent.example.json` | Reference versionnee |
| `intent.json` | Intent local (defaut) |
| `sdn_controller.py` | Controleur |
| `state.json` | Dernier apply (genere, non versionne) |
| `test_sdn_controller.py` | 28 tests unitaires |
| `GNS/` | Projet GNS3 |

---

## Verifs demo (IOS)

```text
show ip ospf neighbor
show ip bgp vpnv4 all summary
show ip route vrf <VRF>
ping vrf Arsium 10.2.67.1
CE# ping 203.0.113.1
PE1# show ip nat translations
```

---

## Sujet NAS (synthese)

| Theme | Statut |
|-------|--------|
| OSPF, MPLS LDP, VPNv4, VRF PE-CE | Oui |
| Multi-RT / hub SopraSteria, NAT client | Oui |
| Manageability (`validate`, `diff`, `apply`, `state.json`) | Oui |
| Ingress TE (CE4) | Oui |
| Internet (NAT VRF PE1) | Oui |
| RSVP | Non |

---

## Auteurs

Mèjdi, Hugo, Marc et Mehdi — projet ARCnet, module NAS (INSA / 3TC).
