"""Unit tests for the AST-based script -> ConfigPlan extractor."""
from __future__ import annotations

import unittest

from app.services.script_parser import (
    ExtractedPlan,
    ScriptParserError,
    parse_script_to_plan,
)


# ── Happy path: netmiko shape ────────────────────────────────────────


NETMIKO_HAPPY = """
from netmiko import ConnectHandler

def main(device):
    conn = ConnectHandler(
        device_type='cisco_ios',
        host=device['ip'],
    )
    cmds = [
        'interface GigabitEthernet0/0/1',
        'description uplink-to-core',
        'no shutdown',
    ]
    conn.send_config_set(cmds)
    return 'ok'
"""


NETMIKO_INLINE_LITERAL = """
import netmiko
conn = netmiko.ConnectHandler(device_type='huawei')
conn.send_config_set([
    'sysname Core-SW1',
    'vlan 100',
    'quit',
])
"""


NAPALM_HAPPY = """
from napalm import get_network_driver
driver = get_network_driver('ios')
conn = driver('10.0.0.1', 'admin', 'pw')
conn.config([
    'interface Loopback0',
    'ip address 10.10.10.10 255.255.255.255',
])
"""


# ── Edge / refusal paths ────────────────────────────────────────────


SYNTAX_BROKEN = "def foo(:\n    pass\n"


DANGEROUS_IMPORT = """
import subprocess
out = subprocess.check_output(['ls'])
"""


DANGEROUS_OS_SYSTEM = """
import os
os.system('rm -rf /')
"""


DANGEROUS_EVAL = """
result = eval('1+1')
"""


SEND_COMMAND_DANGEROUS = """
conn.send_command('reload')
"""


SEND_COMMAND_DISPLAY_ONLY = """
print(conn.send_command('display version'))
"""


SEND_COMMAND_CONFIG_LITERAL = """
# Inside send_command — only the literal that looks config-mode is kept
conn.send_command('interface GigabitEthernet0/0/1')
"""


NON_LITERAL_ARG = """
conn.send_config_set(generate_cmds())
"""


EMPTY_SCRIPT = ""


WILDCARD_FROM = """
from netmiko import *
conn = ConnectHandler()
conn.send_config_set(['vlan 10'])
"""


class ParseScriptToPlanTest(unittest.TestCase):
    def test_netmiko_send_config_set_happy(self) -> None:
        # cmds = [...] is a local variable, then send_config_set(cmds).
        # We only inspect literal AST arguments — the call shape is
        # recognised (matched_calls=1) but commands stay empty because
        # we won't execute the script. A warning tells the user why.
        plan = parse_script_to_plan(NETMIKO_HAPPY)
        self.assertEqual(plan.matched_calls, 1)
        self.assertEqual(plan.commands, [])
        self.assertEqual(plan.vendor, "cisco_ios_xe")
        self.assertTrue(any("字面量" in w for w in plan.warnings))

    def test_inline_send_config_set(self) -> None:
        plan = parse_script_to_plan(NETMIKO_INLINE_LITERAL)
        self.assertEqual(plan.matched_calls, 1)
        self.assertEqual(plan.commands, [
            "sysname Core-SW1", "vlan 100", "quit",
        ])
        self.assertEqual(plan.vendor, "huawei_vrp")

    def test_napalm_config_call(self) -> None:
        plan = parse_script_to_plan(NAPALM_HAPPY)
        self.assertEqual(plan.matched_calls, 1)
        self.assertEqual(plan.commands, [
            "interface Loopback0",
            "ip address 10.10.10.10 255.255.255.255",
        ])

    def test_syntax_error_raises(self) -> None:
        with self.assertRaises(ScriptParserError):
            parse_script_to_plan(SYNTAX_BROKEN)

    def test_dangerous_import_rejected(self) -> None:
        with self.assertRaises(ScriptParserError) as ctx:
            parse_script_to_plan(DANGEROUS_IMPORT)
        self.assertIn("subprocess", str(ctx.exception))

    def test_dangerous_os_system_rejected(self) -> None:
        with self.assertRaises(ScriptParserError):
            parse_script_to_plan(DANGEROUS_OS_SYSTEM)

    def test_dangerous_eval_rejected(self) -> None:
        with self.assertRaises(ScriptParserError):
            parse_script_to_plan(DANGEROUS_EVAL)

    def test_send_command_dangerous_dropped(self) -> None:
        plan = parse_script_to_plan(SEND_COMMAND_DANGEROUS)
        self.assertEqual(plan.commands, [])
        self.assertEqual(plan.matched_calls, 1)

    def test_send_command_display_only_dropped(self) -> None:
        plan = parse_script_to_plan(SEND_COMMAND_DISPLAY_ONLY)
        self.assertEqual(plan.commands, [])

    def test_send_command_config_literal_kept(self) -> None:
        plan = parse_script_to_plan(SEND_COMMAND_CONFIG_LITERAL)
        self.assertEqual(plan.commands,
                         ["interface GigabitEthernet0/0/1"])

    def test_non_literal_arg_warns(self) -> None:
        plan = parse_script_to_plan(NON_LITERAL_ARG)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("字面量" in w for w in plan.warnings))

    def test_empty_script_raises(self) -> None:
        with self.assertRaises(ScriptParserError):
            parse_script_to_plan(EMPTY_SCRIPT)

    def test_wildcard_import_marks_review(self) -> None:
        plan = parse_script_to_plan(WILDCARD_FROM)
        self.assertTrue(plan.requires_review)
        # Should still extract the command we could recognise.
        self.assertIn("vlan 10", plan.commands)

    def test_dedup_preserves_first_order(self) -> None:
        src = """
conn.send_config_set(['vlan 10', 'quit'])
conn.send_config_set(['vlan 10', 'quit', 'sysname x'])
"""
        plan = parse_script_to_plan(src)
        self.assertEqual(plan.commands, ["vlan 10", "quit", "sysname x"])

    def test_multiple_calls_combined(self) -> None:
        src = """
conn.send_config_set(['vlan 100', 'quit'])
conn.config(['router ospf 1'])
conn.send_command('interface Loopback1')
"""
        plan = parse_script_to_plan(src)
        # vlan 100 + quit + router ospf 1 + interface Loopback1 = 4 lines
        self.assertEqual(plan.matched_calls, 3)
        self.assertEqual(len(plan.commands), 4)
        self.assertIn("router ospf 1", plan.commands)


class ScriptSizeLimitTest(unittest.TestCase):
    def test_over_200kb_rejected(self) -> None:
        big = "x = 1\n" * 30_000  # ~150k chars; we want clearly over
        big = big + "x = 1\n" * 30_000
        self.assertGreater(len(big), 200_000)
        with self.assertRaises(ScriptParserError) as ctx:
            parse_script_to_plan(big)
        self.assertIn("200KB", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
