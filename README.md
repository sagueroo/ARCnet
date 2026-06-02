# ARCnet

Controleur intent-based pour un lab **BGP / MPLS VPN** sur GNS3. Lit `intent.json`, genere des commandes IOS, les pousse en Telnet.

Projet NAS (3TC) - INSA.

Reference : `[intent.example.json](intent.example.json)`

---

## Sujet NAS (synthèse)


| Theme                                                              | Statut |
| ------------------------------------------------------------------ | ------ |
| OSPF, MPLS LDP, VPNv4, VRF PE-CE                                   | Oui    |
| Multi-RT / hub SopraSteria, NAT client                             | Oui    |
| Manageability (`validate`, `diff`, `apply`, `reset`, `state.json`) | Oui    |
| Ingress TE (CE4 dual-home)                                         | Oui    |
| Internet (NAT VRF PE1 + routeur ISP)                               | Oui    |
| RSVP                                                               | Non    |


---

## Installation

- Python 3 (stdlib)
- GNS3 : routeurs nommes comme dans l'intent (`PE1`, `CE`, …)
- Dossier `GNS/` avec le projet `.gns3`

---

## Utilisation

```bash
python sdn_controller.py validate
python sdn_controller.py diff
python sdn_controller.py apply
python sdn_controller.py apply --only PE1,CE2
python sdn_controller.py reset
python -m unittest test_sdn_controller -v
```


| Commande   | Description                                   |
| ---------- | --------------------------------------------- |
| `validate` | Vérifie l'intent                              |
| `diff`     | Affiche les commandes sans les envoyer        |
| `apply`    | Configure les routeurs (crée `state.json`)    |
| `reset`    | Efface la config (Telnet + fichiers Dynamips) |


Options communes : `--intent`, `--only`.  
Après `reset` : **Stop puis Start** les noeuds dans GNS3, puis `apply`.

---

## Intent (essentiel)


| Section        | Role                                                |
| -------------- | --------------------------------------------------- |
| `addressing`   | Pools IP (loopback, coeur, PE-CE)                   |
| `AS` / `3215`  | Coeur MPLS, VRF `Arsium`, `EuroInfo`, `SopraSteria` |
| `AS` / clients | CE avec `upstream_as`, `nat_map`, `prepend`, …      |
| `internet`     | Lien PE1 ↔ ISP simule (`203.0.113.1`)               |


Champs CE utiles : `nat_map` (NAT vers prefixe global), `announce` (préfixe global BGP en plus du LAN), `prepend` (Ingress TE).

---

## Tests rapides

```text
CE#  ping 192.168.69.254      ! CE -> CE1 (Arsium)
CE2# ping 192.168.69.254      ! CE2 -> CE3 (EuroInfo)
CE#  ping 203.0.113.1         ! Internet
CE4# ping 172.16.200.254      ! CE4 -> CE5 (SopraSteria)
```

---

## Fichiers


| Fichier                               | Description                     |
| ------------------------------------- | ------------------------------- |
| `sdn_controller.py`                   | Controleur                      |
| `intent.json` / `intent.example.json` | Intention reseau                |
| `state.json`                          | Etat du dernier `apply` (local) |
| `test_sdn_controller.py`              | Tests unitaires                 |
| `GNS/`                                | Topologie GNS3                  |


---

## Auteurs

Mèjdi, Hugo, Marc et Mehdi