"""ERC-8004 additions to the a2a client tools. These tests need no blockchain.

They cover the choice of a peer by rating (S2 and S3), the check that a registration file
points back to its agent, how on-chain peers join the hand-written ones, and the tools' input
checks.
"""
import os
import random
import sys
import unittest
from collections import Counter
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


US = "0xUs"  # the trusted reviewer


def _history(agent_id, values, reviewer=US, first_block=1):
    """Ratings of one agent in this order, one per block, starting at first_block."""
    return [erc.Rating(agent_id, reviewer, value, first_block + step) for step, value in enumerate(values)]


def _peers(*entries):
    """Peers from (name, agent_id, owner)."""
    return {name: {"agent_id": agent_id, "owner": owner} for name, agent_id, owner in entries}


def _best(peers, found, seed=0, **trust):
    """S3's choice with the highest mean. The other signals are off unless a test switches one on."""
    settings = erc.TrustSettings(**{"trusted": {US.lower()}, "half_life": None, "owner_weight": 0,
                                    "choice": "mean", **trust})
    return erc.choose_trust_aware(erc.beliefs(peers, found, settings), random.Random(seed), settings.choice)


class TestTrustAware(unittest.TestCase):
    """S3. Each test shows what one signal adds."""

    def test_events_keep_this_capability_and_drop_revoked_ratings(self):
        def event(agent_id, index, value, tag2="flight-search"):
            return {"args": {"agentId": agent_id, "clientAddress": US, "feedbackIndex": index, "value": value,
                             "valueDecimals": 0, "tag1": "task", "tag2": tag2}, "blockNumber": 7}
        revoked = [{"args": {"agentId": 1, "clientAddress": US, "feedbackIndex": 2}}]
        new = [event(1, 1, 100), event(1, 2, 0), event(2, 1, 100, tag2="hotel-search")]
        self.assertEqual(erc.ratings_from_logs(new, revoked, "flight-search"), [erc.Rating(1, US, 100, 7)])

    def test_fake_reviewers_barely_count(self):
        peers = _peers(("honest#1", 1, "0xA"), ("liar#2", 2, "0xB"))
        found = _history(1, [100, 100, 0]) + _history(2, [0])
        found += _history(2, [100] * 10, reviewer="0xSybil", first_block=10)
        self.assertEqual(_best(peers, found), "honest#1")
        self.assertEqual(_best(peers, found, other_weight=1), "liar#2")  # every reviewer counts, as in S2

    def test_newer_ratings_drop_an_agent_that_turned_bad(self):
        peers = _peers(("turned#1", 1, "0xA"), ("steady#2", 2, "0xB"))
        found = _history(1, [100] * 20 + [0] * 4) + _history(2, [100, 100, 100, 0])
        self.assertEqual(_best(peers, found), "turned#1")  # without recency, 20 old ratings still win
        self.assertEqual(_best(peers, found, half_life=2), "steady#2")

    def test_a_clone_inherits_its_owners_record(self):
        peers = _peers(("bad#1", 1, "0xBad"), ("clone#2", 2, "0xBad"), ("new#3", 3, "0xGood"))
        found = _history(1, [0] * 5)
        chosen = {owner_weight: {_best(peers, found, seed, owner_weight=owner_weight) for seed in range(20)}
                  for owner_weight in (0, 0.5)}
        self.assertEqual(chosen[0], {"clone#2", "new#3"})  # the clone looks as fresh as a newcomer
        self.assertEqual(chosen[0.5], {"new#3"})

    def test_sampling_mostly_exploits_but_still_tries_a_newcomer(self):
        peers = _peers(("good#1", 1, "0xA"), ("new#2", 2, "0xB"), ("bad#3", 3, "0xC"))
        found = _history(1, [100] * 10) + _history(3, [0])
        belief = erc.beliefs(peers, found, erc.TrustSettings(trusted={US.lower()}))
        rng = random.Random(42)
        chosen = Counter(erc.choose_trust_aware(belief, rng) for _ in range(200))
        self.assertEqual(chosen.most_common(1)[0][0], "good#1")
        self.assertGreater(chosen["new#2"], 0)

    def test_each_choice_draws_new_numbers_but_a_rerun_repeats_them(self):
        def session(found, choices=2):  # a new process: its count of S3 choices starts at zero
            with patch.object(erc, "_s3_choices", 0):
                return [erc._draws({"seed": 42}, "flight-search", found).random() for _ in range(choices)]
        first, retry = session([])
        self.assertNotEqual(retry, first)  # a retry in the same session, with the same ratings
        self.assertEqual(session([]), [first, retry])  # a rerun on the same chain
        self.assertNotEqual(session(_history(1, [100]), choices=1), [first])  # the next session, after a rating

    def test_a_retry_does_not_always_go_to_the_same_failing_peer(self):
        """Inside a session nobody rates the answers, as in R000 of the run of 22 November 2026."""
        peers = _peers(("fails#1", 1, "0xA"), ("other#2", 2, "0xB"), ("third#3", 3, "0xC"))
        cfg = {**CHAIN, "reviewers": [US], "seed": 42, "strategy": "S3"}
        with patch.object(erc, "_s3_choices", 0), patch.object(erc, "all_ratings", return_value=[]):
            chosen = {erc.pick(cfg, peers, "flight-search")[0] for _ in range(10)}
        self.assertGreater(len(chosen), 1)

    def test_pick_follows_the_strategy_of_the_config(self):
        peers = _peers(("a#1", 1, "0xA"))
        cfg = {**CHAIN, "reviewers": [US], "trust": {"half_life": None}}
        with patch.object(erc, "ratings", return_value={1: (2, 50.0)}), \
                patch.object(erc, "all_ratings", return_value=_history(1, [100, 0])):
            self.assertEqual(erc.pick(cfg, peers, "c"), ("a#1", "rated 50/100 by 2 rating(s)"))
            self.assertEqual(erc.pick({**cfg, "strategy": "S3"}, peers, "c"), ("a#1", "trust 0.50 from 2 rating(s)"))
            with self.assertRaises(ValueError):
                erc.pick({**cfg, "strategy": "S9"}, peers, "c")


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

    def test_delegate_only_hides_the_tools_that_let_the_model_choose_the_peer(self):
        with self._config({"erc8004": CHAIN}):
            self.assertTrue(a2at._peer_choice_tools_available())
        with self._config({"erc8004": {**CHAIN, "delegate_only": True}}):
            self.assertFalse(a2at._peer_choice_tools_available())
            self.assertTrue(a2at._a2a_tools_available())  # a2a_delegate and the other tools stay

    def test_only_a2a_call_and_a2a_orchestrate_get_the_delegate_only_check(self):
        checks = {}
        a2at.register_tools(SimpleNamespace(register_tool=lambda **tool: checks.update({tool["name"]: tool["check_fn"]})))
        hidden = {name for name, check in checks.items() if check is a2at._peer_choice_tools_available}
        self.assertEqual(hidden, {"a2a_call", "a2a_orchestrate"})
        self.assertIn("a2a_delegate", checks)

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

    def test_delegate_with_s3_reads_every_rating(self):
        found = {"flights#3": {"url": "http://y.invalid", "capabilities": ["flight-search"], "agent_id": 3,
                               "owner": "0xA"}}
        cfg = {"erc8004": {**CHAIN, "strategy": "S3", "reviewers": [US]}}
        with self._config(cfg), patch.object(erc, "discover_peers", return_value=found), \
                patch.object(erc, "all_ratings", return_value=[]), \
                patch.object(a2at, "_send_task", return_value=("ok", "ctx-1", "")):
            reply = a2at.a2a_delegate({"capability": "flight-search", "message": "Pisa to Rome"})
        self.assertIn("[delegate: flights#3 · trust 0.50 from 0 rating(s)", reply)

    def test_feedback_checks_its_inputs(self):
        with self._config({"erc8004": CHAIN}):
            self.assertIn("0 to 100", a2at.a2a_feedback({"agent": "a#1", "value": 150, "capability": "c"}))
            self.assertIn("0 to 100", a2at.a2a_feedback({"agent": "a#1", "value": "high", "capability": "c"}))
            self.assertIn("name#agentId", a2at.a2a_feedback({"agent": "a", "value": 100, "capability": "c"}))


if __name__ == "__main__":
    unittest.main()
