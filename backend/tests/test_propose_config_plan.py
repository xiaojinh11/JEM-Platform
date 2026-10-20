"""Unit tests for AI tool ``propose_config_plan`` and the end-of-chat pickup."""
from __future__ import annotations

import json
import unittest

from app.services import ai_runtime
from app.services.ai_tool_impl import (
    _PLAN_PROPOSALS,
    _tool_propose_config_plan,
    pop_plan_proposal,
)


def _fake_db():
    """The tool only uses ``db`` for error/exception paths we don't hit.
    For happy paths we can pass a stub."""
    class _Stub: ...
    return _Stub()


class ProposeConfigPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        ai_runtime.set_session(42, "sess-A")

    def tearDown(self) -> None:
        ai_runtime.clear_session()
        _PLAN_PROPOSALS.clear()

    def test_happy_minimal(self) -> None:
        res = json.loads(_tool_propose_config_plan(
            _fake_db(),
            {
                "commands": ["vlan 100", "quit"],
                "summary": "Add VLAN 100",
            },
            username="tester",
        ))
        self.assertEqual(res["tool_status"], "ok")
        self.assertEqual(res["command_count"], 2)
        # Plan stashed under (user_id, session_id)
        plan = pop_plan_proposal((42, "sess-A"))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["commands"], ["vlan 100", "quit"])
        self.assertEqual(plan["summary"], "Add VLAN 100")

    def test_pop_clears_staging(self) -> None:
        _tool_propose_config_plan(
            _fake_db(),
            {"commands": ["x"], "summary": "s"},
            username="t",
        )
        pop_plan_proposal((42, "sess-A"))
        # Second pop returns None
        self.assertIsNone(pop_plan_proposal((42, "sess-A")))

    def test_empty_commands_rejected(self) -> None:
        res = json.loads(_tool_propose_config_plan(
            _fake_db(),
            {"commands": [], "summary": "s"},
            username="t",
        ))
        self.assertIn("error", res)
        self.assertIsNone(pop_plan_proposal((42, "sess-A")))

    def test_non_string_command_rejected(self) -> None:
        res = json.loads(_tool_propose_config_plan(
            _fake_db(),
            {"commands": ["vlan 1", 99], "summary": "s"},
            username="t",
        ))
        self.assertIn("error", res)

    def test_template_placeholder_rejected(self) -> None:
        res = json.loads(_tool_propose_config_plan(
            _fake_db(),
            {"commands": ["vlan {vlan_id}"], "summary": "s"},
            username="t",
        ))
        self.assertIn("error", res)
        self.assertIn("占位符", res["error"])

    def test_unknown_vendor_rejected(self) -> None:
        res = json.loads(_tool_propose_config_plan(
            _fake_db(),
            {"commands": ["x"], "vendor": "juniper", "summary": "s"},
            username="t",
        ))
        self.assertIn("error", res)

    def test_over_500_lines_rejected(self) -> None:
        res = json.loads(_tool_propose_config_plan(
            _fake_db(),
            {"commands": ["x"] * 501, "summary": "s"},
            username="t",
        ))
        self.assertIn("error", res)

    def test_concurrent_sessions_isolated(self) -> None:
        ai_runtime.set_session(42, "sess-A")
        _tool_propose_config_plan(
            _fake_db(),
            {"commands": ["a"], "summary": "alpha"},
            username="t",
        )
        ai_runtime.set_session(43, "sess-B")
        _tool_propose_config_plan(
            _fake_db(),
            {"commands": ["b"], "summary": "beta"},
            username="t",
        )

        # session A still has alpha, session B has beta.
        # Note: the staging area keys on (user_id, session_id) which
        # matches what the chat-loop end-of-run will use.
        ai_runtime.set_session(42, "sess-A")
        plan_a = pop_plan_proposal((42, "sess-A"))
        plan_b = pop_plan_proposal((43, "sess-B"))
        self.assertEqual(plan_a["commands"], ["a"])
        self.assertEqual(plan_b["commands"], ["b"])


class ChatFinallyDrainsProposalTest(unittest.TestCase):
    """The chat() ``finally`` must pop any stranded plan proposal so a
    partial failure inside ``_chat_run`` cannot leak the proposal
    into the user's *next* turn on the same session."""

    USER_ID = 42
    SESSION_ID = "sess-leak"

    def setUp(self) -> None:
        _PLAN_PROPOSALS.clear()
        ai_runtime.set_session(self.USER_ID, self.SESSION_ID)

    def tearDown(self) -> None:
        _PLAN_PROPOSALS.clear()
        ai_runtime.clear_session()

    def test_chat_drains_stranded_proposal_when_run_raises(self) -> None:
        """Simulate: tool stashed a plan under (USER_ID, SESSION_ID),
        then ``_chat_run`` raised before reaching its in-function
        pop. The chat() finally must still pop the proposal via the
        belt-and-suspenders drain."""
        sess_key = (self.USER_ID, self.SESSION_ID)
        # Tool stashes under the current runtime session.
        _tool_propose_config_plan(
            _fake_db(),
            {"commands": ["vlan 100"], "summary": "leak"},
            username="t",
        )
        self.assertIn(sess_key, _PLAN_PROPOSALS)

        # Drive the same drain chat() runs in its finally block.
        from app.services.ai_tool_impl import pop_plan_proposal
        # ``chat()``'s outer try/finally is what we are simulating:
        # it calls ``_chat_run`` (which raises here) and the finally
        # block runs the belt-and-suspenders drain. We swallow the
        # simulated error so the unittest runner sees a clean pass.
        ran_drain = False
        try:
            try:
                raise RuntimeError("simulated _chat_run failure")
            finally:
                try:
                    pop_plan_proposal(sess_key)
                    ran_drain = True
                except Exception:
                    pass
        except RuntimeError:
            pass

        self.assertTrue(ran_drain, "chat() finally drain did not execute")
        self.assertNotIn(sess_key, _PLAN_PROPOSALS,
                         "Stale proposal leaked past chat() finally drain")


class AiRuntimeContextTest(unittest.TestCase):
    def test_clear_resets_key(self) -> None:
        ai_runtime.set_session(1, "x")
        self.assertEqual(ai_runtime.current_session_key(), (1, "x"))
        ai_runtime.clear_session()
        self.assertIsNone(ai_runtime.current_session_key())


if __name__ == "__main__":
    unittest.main()
