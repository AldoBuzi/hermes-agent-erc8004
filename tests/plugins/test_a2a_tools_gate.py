"""a2a client tools config gate (#95681, maintainer-directed).

The 5 outbound a2a_* tools registered unconditionally — every session on
every install paid ~561 tok/call for a toolset whose only possible output
without config is "no peers configured". A2A is NOT the Bot Mode
mechanism (bots talk over gateway RPCs); it is opt-in foreign-agent
plumbing. Gate: serve only when a2a_agents is non-empty, the inbound
platform is enabled, or A2A_PORT is set. Fail closed.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import plugins.platforms.a2a.tools as a2at


class TestA2AToolsGate(unittest.TestCase):
    def setUp(self):
        os.environ.pop("A2A_PORT", None)

    def _avail(self, cfg):
        with patch.object(a2at, "_load_config", return_value=cfg):
            return a2at._a2a_tools_available()

    def test_unconfigured_install_serves_nothing(self):
        self.assertFalse(self._avail({}))
        self.assertFalse(self._avail({"a2a_agents": {}}))

    def test_peers_configured_serves(self):
        self.assertTrue(self._avail({"a2a_agents": {"r": {"url": "http://x"}}}))

    def test_inbound_platform_enabled_serves(self):
        self.assertTrue(self._avail({"platforms": {"a2a": {"enabled": True}}}))

    def test_a2a_port_env_serves(self):
        os.environ["A2A_PORT"] = "9999"
        try:
            self.assertTrue(self._avail({}))
        finally:
            os.environ.pop("A2A_PORT", None)

    def test_config_crash_fails_closed(self):
        with patch.object(a2at, "_load_config", side_effect=RuntimeError("boom")):
            self.assertFalse(a2at._a2a_tools_available())

    def test_all_five_tools_carry_the_gate(self):
        """Every a2a_* registration must pass the check_fn — a sixth tool
        added without it would silently reopen the hole. a2a_call and
        a2a_orchestrate get it through _peer_choice_tools_available, which
        also hides them when erc8004.delegate_only is set."""
        seen = {}

        class Ctx:
            def register_tool(self, name, **kw):
                seen[name] = kw.get("check_fn")

        a2at.register_tools(Ctx())
        self.assertTrue(seen)
        for name, fn in seen.items():
            self.assertIn(fn, (a2at._a2a_tools_available, a2at._peer_choice_tools_available), name)
        with patch.object(a2at, "_load_config", return_value={}):
            self.assertFalse(a2at._peer_choice_tools_available())  # closed whenever the gate is closed


if __name__ == "__main__":
    unittest.main()
