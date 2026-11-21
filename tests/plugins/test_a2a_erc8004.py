"""ERC-8004 additions to the a2a client tools. These tests need no blockchain.

They cover the choice of a peer by rating, the check that a registration file points back
to its agent, how on-chain peers join the hand-written ones, and the tools' input checks.
"""
import os
import random
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import plugins.platforms.a2a.erc8004 as erc
import plugins.platforms.a2a.tools as a2at

CHAIN = {"rpc_url": "http://chain.invalid"}


class TestChoose(unittest.TestCase):
    def test_unrated_peers_go_first(self):
        self.assertEqual(erc.choose({"a#1": (3, 100.0), "b#2": (0, 0.0)}, random.Random(0)), "b#2")

    def test_highest_average_wins(self):
        self.assertEqual(erc.choose({"a#1": (3, 66.7), "b#2": (2, 100.0)}, random.Random(0)), "b#2")

    def test_ties_are_not_broken_by_name(self):
        rated = {"a#1": (2, 100.0), "z#2": (5, 100.0)}
        self.assertEqual({erc.choose(rated, random.Random(seed)) for seed in range(20)}, {"a#1", "z#2"})


class TestAgentIdFromName(unittest.TestCase):
    def test_names_and_numbers(self):
        self.assertEqual(erc.agent_id_from_name("flights-honest-0#10582"), 10582)
        self.assertEqual(erc.agent_id_from_name("7"), 7)
        self.assertIsNone(erc.agent_id_from_name("flights-honest-0"))


class TestRegistrationCheck(unittest.TestCase):
    """agentId -> agentURI -> registration file -> Agent Card, with documents served by a stub."""

    REGISTRY = "eip155:1:0xabc"

    def _identity(self):
        return SimpleNamespace(functions=SimpleNamespace(
            tokenURI=lambda agent_id: SimpleNamespace(call=lambda: "http://files.invalid/a.json"),
            ownerOf=lambda agent_id: SimpleNamespace(call=lambda: "0xOwner")))

    def _peer(self, files, agent_id=5):
        with patch.object(erc, "_get_json", side_effect=files.__getitem__):
            return erc._peer(self._identity(), agent_id, self.REGISTRY)

    def test_valid_chain_gives_a_peer_with_the_cards_skills(self):
        files = {
            "http://files.invalid/a.json": {
                "registrations": [{"agentId": 5, "agentRegistry": "eip155:1:0xABC"}],
                "services": [{"name": "A2A", "endpoint": "http://agent.invalid:9001/.well-known/agent-card.json"}]},
            "http://agent.invalid:9001/.well-known/agent-card.json": {
                "name": "flights", "skills": [{"id": "flight-search"}]},
        }
        peer = self._peer(files)
        self.assertEqual(peer["url"], "http://agent.invalid:9001")
        self.assertEqual(peer["capabilities"], ["flight-search"])
        self.assertEqual((peer["agent_id"], peer["card_name"]), (5, "flights"))

    def test_file_that_names_another_agent_is_rejected(self):
        files = {"http://files.invalid/a.json": {
            "registrations": [{"agentId": 9, "agentRegistry": self.REGISTRY}], "services": []}}
        with self.assertRaises(ValueError):
            self._peer(files)

    def test_only_http_uris_are_fetched(self):
        with self.assertRaises(ValueError):
            erc._get_json("file:///etc/passwd")


class TestToolsWithErc8004(unittest.TestCase):
    def _config(self, cfg):
        return patch.object(a2at, "_load_config", return_value=cfg)

    def test_erc8004_section_opens_the_gate(self):
        with self._config({"erc8004": CHAIN}):
            self.assertTrue(a2at._a2a_tools_available())

    def test_on_chain_peers_join_the_hand_written_ones(self):
        found = {"flights#3": {"url": "http://y.invalid", "capabilities": ["flight-search"], "agent_id": 3}}
        cfg = {"a2a_agents": {"r": {"url": "http://x.invalid"}}, "erc8004": CHAIN}
        with self._config(cfg), patch.object(erc, "discover_peers", return_value=found):
            self.assertEqual(set(a2at._configured_peers()), {"r", "flights#3"})

    def test_chain_failure_keeps_the_hand_written_peers(self):
        cfg = {"a2a_agents": {"r": {"url": "http://x.invalid"}}, "erc8004": CHAIN}
        with self._config(cfg), patch.object(erc, "discover_peers", side_effect=ConnectionError("down")):
            self.assertEqual(set(a2at._configured_peers()), {"r"})

    def test_delegate_without_erc8004_explains_the_setup(self):
        with self._config({}):
            self.assertIn("erc8004", a2at.a2a_delegate({"capability": "c", "message": "m"}))

    def test_delegate_sends_an_object_as_json(self):
        found = {"flights#3": {"url": "http://y.invalid", "capabilities": ["flight-search"], "agent_id": 3}}
        with self._config({"erc8004": CHAIN}), patch.object(erc, "discover_peers", return_value=found), \
                patch.object(erc, "ratings", return_value={3: (0, 0.0)}), \
                patch.object(a2at, "_send_task", return_value=("ok", "ctx-1", "")) as send:
            a2at.a2a_delegate({"capability": "flight-search", "message": {"from": "Pisa", "to": "Rome"}})
        self.assertEqual(send.call_args.args[2], '{"from": "Pisa", "to": "Rome"}')

    def test_feedback_checks_its_inputs(self):
        with self._config({"erc8004": CHAIN}):
            self.assertIn("0 to 100", a2at.a2a_feedback({"agent": "a#1", "value": 150, "capability": "c"}))
            self.assertIn("0 to 100", a2at.a2a_feedback({"agent": "a#1", "value": "high", "capability": "c"}))
            self.assertIn("name#agentId", a2at.a2a_feedback({"agent": "a", "value": 100, "capability": "c"}))


if __name__ == "__main__":
    unittest.main()
