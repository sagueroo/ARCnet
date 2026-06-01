#!/usr/bin/env python3
"""Tests unitaires pour sdn_controller.py"""

import copy
import json
import unittest

from sdn_controller import (
    validate_intent,
    discover_links,
    allocate_ips,
    build_router_id_map,
    expand_vrf,
    generate_build,
    peer_ip,
    cidr_to_netmask,
    nat_map_cmds,
)

INTENT = json.load(open("intent.json"))


class TestValidation(unittest.TestCase):

    def test_valid_intent_passes(self):
        validate_intent(copy.deepcopy(INTENT))

    def test_missing_AS_key(self):
        with self.assertRaises(ValueError) as ctx:
            validate_intent({})
        self.assertIn("'AS' manquante", str(ctx.exception))

    def test_unknown_peer(self):
        bad = copy.deepcopy(INTENT)
        bad["AS"]["3215"]["routers"]["PE1"]["interfaces"]["Gigabitethernet1/0"]["peer"] = "FANTOME"
        with self.assertRaises(ValueError) as ctx:
            validate_intent(bad)
        self.assertIn("peer 'FANTOME' inconnu", str(ctx.exception))

    def test_invalid_igp(self):
        bad = copy.deepcopy(INTENT)
        bad["AS"]["3215"]["igp"] = "EIGRP"
        with self.assertRaises(ValueError) as ctx:
            validate_intent(bad)
        self.assertIn("EIGRP", str(ctx.exception))

    def test_client_without_upstream(self):
        bad = copy.deepcopy(INTENT)
        del bad["AS"]["102"]["upstream_as"]
        with self.assertRaises(ValueError) as ctx:
            validate_intent(bad)
        self.assertIn("upstream_as", str(ctx.exception))

    def test_invalid_ip_cidr(self):
        bad = copy.deepcopy(INTENT)
        bad["AS"]["102"]["routers"]["CE"]["interfaces"]["Gigabitethernet2/0"]["ip"] = "999.999.999.999/24"
        with self.assertRaises(ValueError) as ctx:
            validate_intent(bad)
        self.assertIn("ip invalide", str(ctx.exception))

    def test_vrf_not_declared(self):
        bad = copy.deepcopy(INTENT)
        bad["AS"]["3215"]["routers"]["PE1"]["interfaces"]["Gigabitethernet2/0"]["vrf"] = "Inexistante"
        with self.assertRaises(ValueError) as ctx:
            validate_intent(bad)
        self.assertIn("vrf 'Inexistante' non declaree", str(ctx.exception))


class TestDiscoverLinks(unittest.TestCase):

    def test_finds_all_links(self):
        intent = copy.deepcopy(INTENT)
        links = discover_links(intent)
        self.assertEqual(len(links), 10)

    def test_core_vs_customer(self):
        intent = copy.deepcopy(INTENT)
        links = discover_links(intent)
        core = [l for l in links if l["type"] == "core"]
        cust = [l for l in links if l["type"] == "customer"]
        self.assertEqual(len(core), 3)
        self.assertEqual(len(cust), 7)

    def test_no_loopback_in_links(self):
        intent = copy.deepcopy(INTENT)
        links = discover_links(intent)
        for lk in links:
            self.assertNotIn("Loopback", lk["a"][1])
            self.assertNotIn("Loopback", lk["b"][1])


class TestAllocateIps(unittest.TestCase):

    def test_fills_missing_ips(self):
        intent = copy.deepcopy(INTENT)
        allocate_ips(intent)
        pe1 = intent["AS"]["3215"]["routers"]["PE1"]["interfaces"]
        self.assertIn("ip", pe1["Gigabitethernet1/0"])
        self.assertIn("ip", pe1["Gigabitethernet2/0"])

    def test_preserves_manual_ips(self):
        intent = copy.deepcopy(INTENT)
        allocate_ips(intent)
        ce_lan = intent["AS"]["102"]["routers"]["CE"]["interfaces"]["Gigabitethernet2/0"]
        self.assertEqual(ce_lan["ip"], "192.168.67.254/24")

    def test_peer_ips_in_same_subnet(self):
        intent = copy.deepcopy(INTENT)
        allocate_ips(intent)
        links = discover_links(intent)
        for lk in links:
            a_ip = lk["a"][2].get("ip")
            b_ip = lk["b"][2].get("ip")
            if a_ip and b_ip:
                a_net = str(ipaddress.ip_interface(a_ip).network)
                b_net = str(ipaddress.ip_interface(b_ip).network)
                self.assertEqual(a_net, b_net, f"Lien {lk['a'][0]}<->{lk['b'][0]}")

    def test_no_allocation_without_addressing(self):
        intent = copy.deepcopy(INTENT)
        del intent["addressing"]
        allocate_ips(intent)
        pe1_gi1 = intent["AS"]["3215"]["routers"]["PE1"]["interfaces"]["Gigabitethernet1/0"]
        self.assertNotIn("ip", pe1_gi1)


class TestBuildRouterIdMap(unittest.TestCase):

    def test_unique_ids(self):
        ids = build_router_id_map(INTENT)
        self.assertEqual(len(ids), len(set(ids.values())))

    def test_all_routers_present(self):
        ids = build_router_id_map(INTENT)
        self.assertEqual(len(ids), 10)
        self.assertIn("PE1", ids)
        self.assertIn("CE5", ids)

    def test_ids_in_loopback_pool(self):
        ids = build_router_id_map(INTENT)
        pool = ipaddress.ip_network("10.200.0.0/24")
        for name, addr in ids.items():
            self.assertIn(ipaddress.ip_address(addr), pool, f"{name} hors du pool")


class TestExpandVrf(unittest.TestCase):

    def test_basic_vrf(self):
        v = expand_vrf("3215", {"customer_as": "102"})
        self.assertEqual(v["rd"], "3215:102")
        self.assertEqual(v["rt_export"], ["3215:102"])
        self.assertEqual(v["rt_import"], ["3215:102"])

    def test_with_import_customers(self):
        v = expand_vrf("3215", {"customer_as": "102", "import_customers": ["206897"]})
        self.assertIn("3215:206897", v["rt_import"])
        self.assertIn("3215:102", v["rt_import"])

    def test_custom_rd(self):
        v = expand_vrf("3215", {"customer_as": "102", "rd": "65000:999"})
        self.assertEqual(v["rd"], "65000:999")


class TestGenerateBuild(unittest.TestCase):

    def test_all_routers_have_commands(self):
        intent = copy.deepcopy(INTENT)
        allocate_ips(intent)
        build = generate_build(intent)
        self.assertEqual(len(build), 10)

    def test_pe_has_vrf_and_bgp(self):
        intent = copy.deepcopy(INTENT)
        allocate_ips(intent)
        build = generate_build(intent)
        pe1 = "\n".join(build["PE1"])
        self.assertIn("vrf definition Arsium", pe1)
        self.assertIn("router bgp 3215", pe1)
        self.assertIn("address-family vpnv4", pe1)
        self.assertIn("mpls ip", pe1)

    def test_ce_has_nat_and_bgp(self):
        intent = copy.deepcopy(INTENT)
        allocate_ips(intent)
        build = generate_build(intent)
        ce = "\n".join(build["CE"])
        self.assertIn("ip nat inside source static network", ce)
        self.assertIn("router bgp 102", ce)
        self.assertIn("allowas-in", ce)

    def test_p_router_has_mpls_but_no_vpnv4(self):
        intent = copy.deepcopy(INTENT)
        allocate_ips(intent)
        build = generate_build(intent)
        pc1 = "\n".join(build["PC1"])
        self.assertNotIn("address-family vpnv4", pc1)
        self.assertIn("mpls ip", pc1)
        self.assertIn("router ospf 1", pc1)


class TestUtilities(unittest.TestCase):

    def test_cidr_to_netmask(self):
        self.assertEqual(cidr_to_netmask("/30"), "255.255.255.252")
        self.assertEqual(cidr_to_netmask("/24"), "255.255.255.0")
        self.assertEqual(cidr_to_netmask("/32"), "255.255.255.255")

    def test_peer_ip(self):
        self.assertEqual(peer_ip({"ip": "10.0.0.1/30"}), "10.0.0.2")
        self.assertEqual(peer_ip({"ip": "192.168.1.1/30"}), "192.168.1.2")
        self.assertIsNone(peer_ip({}))

    def test_nat_map_cmds(self):
        cmds = nat_map_cmds({"local": "192.168.67.0/24", "global": "10.1.67.0/24"})
        self.assertEqual(len(cmds), 2)
        self.assertIn("ip nat inside source static network", cmds[0])
        self.assertIn("ip route", cmds[1])
        self.assertIn("Null0", cmds[1])


import ipaddress

if __name__ == "__main__":
    unittest.main()
