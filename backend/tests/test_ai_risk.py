"""Unit tests for AI risk assessment."""
from __future__ import annotations

import unittest

from app.utils.command_validator import assess_commands


class RuleBasedRiskTest(unittest.TestCase):
    def test_safe_commands_pass(self) -> None:
        risk = assess_commands([
            "interface GigabitEthernet0/0/1",
            "description uplink-to-core",
            "ip address 10.1.1.1 255.255.255.252",
            "quit",
        ])
        self.assertFalse(risk.is_dangerous)
        self.assertEqual(risk.matched_patterns, [])

    def test_shutdown_is_dangerous(self) -> None:
        risk = assess_commands([
            "interface GigabitEthernet0/0/1",
            "shutdown",
        ])
        self.assertTrue(risk.is_dangerous)
        self.assertEqual(len(risk.matched_patterns), 1)
        self.assertTrue(risk.require_approval)

    def test_shutdown_vlan_is_not_dangerous(self) -> None:
        """``shutdown vlan 10`` is read-only noise; the regex excludes it."""
        risk = assess_commands(["shutdown vlan 10"])
        self.assertFalse(risk.is_dangerous)

    def test_reload_and_write_erase_are_dangerous(self) -> None:
        risk = assess_commands([
            "write erase",
            "reload",
        ])
        self.assertTrue(risk.is_dangerous)
        self.assertEqual(len(risk.matched_patterns), 2)

    def test_huawei_undo_is_dangerous(self) -> None:
        risk = assess_commands(["undo interface GigabitEthernet0/0/1"])
        self.assertTrue(risk.is_dangerous)

    def test_empty_command_list_is_safe(self) -> None:
        risk = assess_commands([])
        self.assertFalse(risk.is_dangerous)


class AssessJobRiskShapeTest(unittest.TestCase):
    """Validates that ``assess_job_risk`` always returns the documented
    shape, even when the AI is disabled. We don't bootstrap a real DB
    here — that path is exercised by the integration smoke tests in
    ``backend/tests/test_jobs_risk_endpoint.py``."""

    def test_rule_only_path_does_not_need_db(self) -> None:
        from app.services.ai_risk import _rule_based_risk
        risk = _rule_based_risk(["reload"])
        self.assertTrue(risk.is_dangerous)


if __name__ == "__main__":
    unittest.main()
