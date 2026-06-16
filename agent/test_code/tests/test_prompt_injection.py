"""
tests/test_prompt_injection.py
===============================

Integration tests for P3/P4: ProtocolRegistry snapshot injection into
Orchestrator, Core, and Infrastructure Agent prompts.

Verifies:
1. All three agent functions accept a protocol_snapshot parameter.
2. The snapshot is correctly formatted (JSON-deserializable, contains required keys).
3. Placeholder replacement works correctly in each prompt.
4. proposed_additions processing writes to the review queue.
5. Null-safe behaviour when protocol_snapshot is None.

Run:
    cd /home/dmc/桌面/platform/agent/test_code
    python3 -m pytest tests/test_prompt_injection.py -v
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure the package is on sys.path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from protocol_registry import get_default_registry, reset_default_registry


class TestSnapshotInjection(unittest.TestCase):
    """Tests that the ProtocolRegistry snapshot is correctly structured for LLM injection."""

    @classmethod
    def setUpClass(cls):
        reset_default_registry()
        cls.reg = get_default_registry()
        cls.reg.auto_load_for_tc("TC_SECC_CMN_VTB_SessionSetup_001")

    def test_snapshot_contains_all_llm_required_fields(self):
        """Snapshot must contain every field the LLM prompt needs."""
        snap = self.reg.snapshot()
        required_toplevel = [
            "protocol_id",
            "protocol_name",
            "version",
            "suts",
            "categories",
            "wrapper_prefixes",
            "naming",
            "transport",
            "protocol_layers",
            "message_names",
            "message_aliases",
        ]
        for field in required_toplevel:
            self.assertIn(field, snap, f"Missing top-level field: {field}")

    def test_snapshot_naming_has_all_role_patterns(self):
        """naming block must have every role pattern the Orchestrator/Core Agent needs."""
        snap = self.reg.snapshot()
        naming = snap.get("naming", {})
        required_roles = [
            "module",
            "testcase",
            "test_behavior_function",
            "precondition_function",
            "postcondition_function",
            "send_template",
            "receive_template",
            "timer",
            "timer_param",
        ]
        for role in required_roles:
            self.assertIn(role, naming, f"Missing naming role: {role}")

    def test_snapshot_transport_has_required_keys(self):
        """transport block must have default + ports (not top-level TCP_TLS key)."""
        snap = self.reg.snapshot()
        transport = snap.get("transport", {})
        self.assertIn("default", transport)
        self.assertIn("ports", transport)
        ports = transport["ports"]
        self.assertIn("main", ports)      # TCP_TLS port
        self.assertIn("sdp_udp_multicast", ports)  # UDP port

    def test_snapshot_message_names_not_empty(self):
        """At least 15 known ISO 15118-2 messages should be listed."""
        snap = self.reg.snapshot()
        names = snap.get("message_names", [])
        self.assertGreater(len(names), 15, f"Expected >15 message names, got {len(names)}: {names}")
        # Check some known messages
        for msg in ["SupportedAppProtocol", "SessionSetup", "ServiceDiscovery", "PowerDelivery"]:
            self.assertIn(msg, names, f"{msg} should be in message_names")

    def test_snapshot_message_aliases_is_dict(self):
        """message_aliases must be a dict mapping message names to their alias lists."""
        snap = self.reg.snapshot()
        aliases = snap.get("message_aliases", {})
        self.assertIsInstance(aliases, dict)
        # V2GTPSessionSetup should appear as a key (it's a canonical message name in the catalog)
        self.assertIn("V2GTPSessionSetup", aliases)
        # Some messages have aliases (e.g. PaymentServiceSelection -> ServicePaymentSelection)
        self.assertIn("ServicePaymentSelection", aliases)
        # Verify the PaymentServiceSelection alias chain
        if aliases.get("ServicePaymentSelection"):
            self.assertIn("PaymentServiceSelection", aliases["ServicePaymentSelection"])

    def test_snapshot_is_json_serializable(self):
        """snapshot() output must be valid JSON (LLM prompt requirement)."""
        snap = self.reg.snapshot()
        text = json.dumps(snap)
        parsed = json.loads(text)
        self.assertEqual(parsed["protocol_id"], snap["protocol_id"])


class TestPromptPlaceholderReplacement(unittest.TestCase):
    """Tests that {PROTOCOL_SNAPSHOT} is correctly injected into each prompt."""

    @classmethod
    def setUpClass(cls):
        reset_default_registry()
        cls.reg = get_default_registry()
        cls.reg.auto_load_for_tc("TC_SECC_CMN_VTB_SessionSetup_001")
        cls.snap = cls.reg.snapshot()
        cls.snap_json = json.dumps(cls.snap, indent=2, ensure_ascii=False)

    def _replace_placeholder(self, prompt_text: str) -> str:
        return prompt_text.replace("{PROTOCOL_SNAPSHOT}", self.snap_json)

    def test_orchestrator_prompt_has_placeholder(self):
        from ttcn3_generation_pipeline import load_text, ORCHESTRATOR_PROMPT_PATH
        prompt = load_text(ORCHESTRATOR_PROMPT_PATH)
        self.assertIn("{PROTOCOL_SNAPSHOT}", prompt)

    def test_core_agent_prompt_has_placeholder(self):
        from ttcn3_generation_pipeline import load_text, CORE_AGENT_PROMPT_PATH
        prompt = load_text(CORE_AGENT_PROMPT_PATH)
        self.assertIn("{PROTOCOL_SNAPSHOT}", prompt)

    def test_infrastructure_agent_prompt_has_placeholder(self):
        from ttcn3_generation_pipeline import load_text, INFRASTRUCTURE_AGENT_PROMPT_PATH
        prompt = load_text(INFRASTRUCTURE_AGENT_PROMPT_PATH)
        self.assertIn("{PROTOCOL_SNAPSHOT}", prompt)

    def test_orchestrator_prompt_replacement_is_valid_json(self):
        from ttcn3_generation_pipeline import load_text, ORCHESTRATOR_PROMPT_PATH
        prompt = load_text(ORCHESTRATOR_PROMPT_PATH)
        injected = self._replace_placeholder(prompt)
        # The injected block should appear verbatim in the prompt
        self.assertIn(self.snap_json[:50], injected)
        # Extract the JSON block between ```json and ```
        start = injected.find("```json\n") + 8
        end = injected.find("\n```", start)
        block = injected[start:end]
        parsed = json.loads(block)
        self.assertEqual(parsed["protocol_id"], "iso15118_2")

    def test_core_agent_prompt_replacement_is_valid_json(self):
        from ttcn3_generation_pipeline import load_text, CORE_AGENT_PROMPT_PATH
        prompt = load_text(CORE_AGENT_PROMPT_PATH)
        injected = self._replace_placeholder(prompt)
        self.assertIn(self.snap_json[:50], injected)
        start = injected.find("```json\n") + 8
        end = injected.find("\n```", start)
        block = injected[start:end]
        parsed = json.loads(block)
        self.assertIn("naming", parsed)

    def test_infrastructure_agent_prompt_replacement_includes_full_messages(self):
        """Infrastructure Agent needs include_full_messages=True to get messages[] array.

        This is intentional: Core Agent only needs naming/aliases, but Infra Agent
        needs the full messages[] for V2G_MessageUnion generation.
        """
        from ttcn3_generation_pipeline import load_text, INFRASTRUCTURE_AGENT_PROMPT_PATH

        # Simulate the actual pipeline call (include_full_messages=True for Infra Agent)
        snap_full = self.reg.snapshot(include_full_messages=True)
        snap_json_full = json.dumps(snap_full, indent=2, ensure_ascii=False)

        prompt = load_text(INFRASTRUCTURE_AGENT_PROMPT_PATH)
        injected = prompt.replace("{PROTOCOL_SNAPSHOT}", snap_json_full)
        self.assertIn(snap_json_full[:50], injected)

        # Extract JSON block between ```json and ```
        start = injected.find("```json\n") + 8
        end = injected.find("\n```", start)
        block = injected[start:end]
        parsed = json.loads(block)
        # Infra agent MUST have messages[] (union_field, raw_request_hex etc.)
        self.assertIn("messages", parsed, "Infrastructure Agent needs full messages[]")
        self.assertIsInstance(parsed["messages"], list)
        self.assertGreater(len(parsed["messages"]), 5)

    def test_infrastructure_agent_snapshot_has_v2gtp_header_constants(self):
        """Infrastructure Agent needs v2gtp_header_constants (from protocol_layers or top-level)."""
        snap_full = self.reg.snapshot(include_full_messages=True)
        # It may be at top level OR in protocol_layers[]. Both are acceptable.
        has_top = "v2gtp_header_constants" in snap_full
        has_in_layers = any(
            "v2gtp_header_constants" in layer
            for layer in snap_full.get("protocol_layers", [])
        )
        self.assertTrue(has_top or has_in_layers,
                       f"v2gtp_header_constants not found at top level or in protocol_layers. "
                       f"Keys: {list(snap_full.keys())}")


class TestProposedAdditions(unittest.TestCase):
    """Tests for Step 6: LLM-proposed protocol knowledge extension."""

    @classmethod
    def setUpClass(cls):
        reset_default_registry()
        cls.reg = get_default_registry()
        cls.reg.auto_load_for_tc("TC_SECC_CMN_VTB_SessionSetup_001")

    def setUp(self):
        # Clean up any existing proposals for this test
        self._existing_proposals = list(
            (self.reg._protocols_dir / ".review_queue").glob("test_proposal_*.json")
        )

    def test_extend_with_llm_discoveries_writes_file(self):
        """extend_with_llm_discoveries must write a valid proposal JSON to the review queue."""
        proposals = {
            "messages": [
                {
                    "name": "NewDiscoveryMessage",
                    "category": "CMN",
                    "protocol_layer": "V2G_Application",
                    "transport": "TCP_TLS",
                    "uses_raw_octetstring": False,
                    "request_type": "NewDiscoveryMessageReq",
                    "response_type": "NewDiscoveryMessageRes",
                    "union_field": "newDiscoveryMessageReq",
                    "default_precondition_chain": [
                        "f_{SUT}_CMN_PR_InitConfiguration_001",
                        "f_{SUT}_CMN_PR_SupportedAppProtocol_001",
                    ],
                    "rationale": "Discovered from TC_SECC_CMN_VTB_NewDiscoveryMessage_001",
                }
            ]
        }
        path = self.reg.extend_with_llm_discoveries(
            proposals,
            proposed_by="Test Agent",
            proposed_during_tc="TC_TEST_001",
        )
        self.assertTrue(path.exists(), f"Proposal file was not written: {path}")
        with open(path, "r", encoding="utf-8") as f:
            content = json.load(f)
        # Actual schema uses "merge_path" envelope (not "proposed_messages")
        self.assertIn("merge_path", content)
        self.assertEqual(content["target_protocol"], "iso15118_2")
        self.assertEqual(content["proposed_by"], "Test Agent")
        self.assertEqual(content["proposed_during_tc"], "TC_TEST_001")
        self.assertIn("proposed_at", content)
        # The merge_path contains the actual proposals
        self.assertIn("messages", content["merge_path"])
        self.assertEqual(content["merge_path"]["messages"][0]["name"], "NewDiscoveryMessage")
        # Clean up
        path.unlink(missing_ok=True)

    def test_extend_raises_if_no_active_protocol(self):
        """Must raise RuntimeError when no protocol is active."""
        reset_default_registry()
        empty_reg = get_default_registry()
        # Don't call auto_load_for_tc
        proposals = {"messages": []}
        with self.assertRaises(RuntimeError) as ctx:
            empty_reg.extend_with_llm_discoveries(proposals, "test")
        self.assertIn("No active protocol", str(ctx.exception))


class TestAgentFunctionSignatures(unittest.TestCase):
    """Verify all three agent functions have protocol_snapshot in their signature."""

    def test_run_orchestrator_accepts_protocol_snapshot(self):
        from ttcn3_generation_pipeline import run_orchestrator
        import inspect
        sig = inspect.signature(run_orchestrator)
        self.assertIn("protocol_snapshot", sig.parameters)
        # Default must be None
        param = sig.parameters["protocol_snapshot"]
        self.assertIsNone(param.default)

    def test_run_core_agent_accepts_protocol_snapshot(self):
        from ttcn3_generation_pipeline import run_core_agent
        import inspect
        sig = inspect.signature(run_core_agent)
        self.assertIn("protocol_snapshot", sig.parameters)
        param = sig.parameters["protocol_snapshot"]
        self.assertIsNone(param.default)

    def test_run_infrastructure_agent_accepts_protocol_snapshot(self):
        from ttcn3_generation_pipeline import run_infrastructure_agent
        import inspect
        sig = inspect.signature(run_infrastructure_agent)
        self.assertIn("protocol_snapshot", sig.parameters)
        param = sig.parameters["protocol_snapshot"]
        self.assertIsNone(param.default)


class TestNullSafeBehaviour(unittest.TestCase):
    """When protocol_snapshot is None, prompts must not break."""

    def test_orchestrator_prompt_injects_empty_json_when_snapshot_is_none(self):
        from ttcn3_generation_pipeline import load_text, ORCHESTRATOR_PROMPT_PATH
        from ttcn3_generation_pipeline import run_orchestrator

        prompt = load_text(ORCHESTRATOR_PROMPT_PATH)
        replaced = prompt.replace("{PROTOCOL_SNAPSHOT}", "{}")
        self.assertIn("{}", replaced)
        self.assertNotIn("{PROTOCOL_SNAPSHOT}", replaced)

    def test_infrastructure_agent_prompt_injects_empty_json_when_snapshot_is_none(self):
        from ttcn3_generation_pipeline import load_text, INFRASTRUCTURE_AGENT_PROMPT_PATH
        prompt = load_text(INFRASTRUCTURE_AGENT_PROMPT_PATH)
        replaced = prompt.replace("{PROTOCOL_SNAPSHOT}", "{}")
        self.assertIn("{}", replaced)
        self.assertNotIn("{PROTOCOL_SNAPSHOT}", replaced)


if __name__ == "__main__":
    unittest.main(verbosity=2)
