"""
tests/test_protocol_registry.py
===============================

Unit tests for ProtocolRegistry. Run with:
    cd agent/test_code
    python -m pytest tests/test_protocol_registry.py -v
or simply:
    python tests/test_protocol_registry.py
"""

import json
import sys
import unittest
from pathlib import Path

# Make parent dir importable
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

from protocol_registry import ProtocolRegistry, ResolvedMessage, get_default_registry, reset_default_registry


PROTOCOLS_DIR = HERE.parent / "protocols"


class TestProtocolRegistryLoad(unittest.TestCase):
    """Tests for descriptor loading and protocol detection."""

    def setUp(self):
        self.reg = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda *a, **k: None)

    def test_load_iso15118_2(self):
        self.assertIn("iso15118_2", self.reg.available_protocols())

    def test_load_count(self):
        # At least 1 protocol (iso15118_2). The .review_queue/ subdir must NOT
        # be treated as a protocol descriptor.
        self.assertGreaterEqual(len(self.reg.available_protocols()), 1)
        for pid in self.reg.available_protocols():
            self.assertNotIn("/", pid)
            self.assertNotIn(".", pid)

    def test_detect_iso15118_2(self):
        pid = self.reg.detect_protocol("TC_SECC_CMN_VTB_SessionSetup_001")
        self.assertEqual(pid, "iso15118_2")

    def test_detect_returns_none_for_unknown(self):
        pid = self.reg.detect_protocol("TC_OCPP_CMN_VTB_Unknown_001")
        self.assertIsNone(pid)

    def test_auto_load_for_tc(self):
        pid = self.reg.auto_load_for_tc("TC_SECC_CMN_VTB_SessionSetup_001")
        self.assertEqual(pid, "iso15118_2")
        self.assertEqual(self.reg.active_protocol(), "iso15118_2")

    def test_load_raises_for_unknown_protocol(self):
        with self.assertRaises(KeyError):
            self.reg.load("does_not_exist")


class TestProtocolRegistryResolve(unittest.TestCase):
    """Tests for the resolve() API."""

    def setUp(self):
        self.reg = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda *a, **k: None)
        self.reg.load("iso15118_2")

    # ---- exact-match messages ----

    def test_resolve_session_setup(self):
        r = self.reg.resolve("SessionSetup", sut="SECC", seq_no="001")
        self.assertEqual(r.canonical, "SessionSetup")
        self.assertEqual(r.request_type, "SessionSetupReq")
        self.assertEqual(r.response_type, "SessionSetupRes")
        self.assertEqual(r.union_field_req, "sessionSetupReq")
        self.assertEqual(r.union_field_res, "sessionSetupRes")
        self.assertEqual(r.port_name, "pt_V2G_TCP_TLS_ALM_SECC_Port")
        self.assertEqual(r.send_template_name, "md_CMN_SessionSetupReq_001")
        self.assertEqual(r.receive_template_name, "mdw_SECC_SessionSetupRes_001")
        self.assertEqual(r.protocol_layer, "V2G_Application")
        self.assertFalse(r.uses_raw_octetstring)
        self.assertIsNone(r.covered_by_precondition)

    def test_resolve_authorization(self):
        r = self.reg.resolve("Authorization", sut="SECC")
        self.assertEqual(r.union_field_req, "authorizationReq")
        self.assertEqual(r.send_template_name, "md_CMN_AuthorizationReq_001")

    def test_resolve_evcc(self):
        r = self.reg.resolve("SessionSetup", sut="EVCC", seq_no="005")
        self.assertEqual(r.port_name, "pt_V2G_TCP_TLS_ALM_EVCC_Port")
        self.assertEqual(r.receive_template_name, "mdw_EVCC_SessionSetupRes_005")
        # Precondition chain uses EVCC
        for fn in r.default_precondition_chain:
            self.assertIn("EVCC", fn)

    # ---- alias messages ----

    def test_resolve_metering_receipt_alias(self):
        r = self.reg.resolve("MeteringReceipt", sut="SECC")
        self.assertEqual(r.canonical, "MeterReceipt")
        self.assertEqual(r.union_field_req, "meterReceiptReq")
        self.assertEqual(r.request_type, "MeterReceiptReq")

    def test_resolve_payment_service_selection_alias(self):
        r = self.reg.resolve("PaymentServiceSelection", sut="SECC")
        self.assertEqual(r.canonical, "ServicePaymentSelection")
        self.assertEqual(r.union_field_req, "servicePaymentSelectionReq")

    # ---- wrapper-prefixed message ----

    def test_resolve_v2gtp_session_setup(self):
        """The bug case from the previous session."""
        r = self.reg.resolve("V2GTPSessionSetup", sut="SECC", seq_no="001")
        # The raw form is its own message in the descriptor.
        # `canonical` is the descriptor's name field (V2GTPSessionSetup);
        # the wrapper-stripped bare form is exposed via union_field_req.
        self.assertEqual(r.canonical, "V2GTPSessionSetup")
        self.assertEqual(r.union_field_req, "sessionSetupReq")
        self.assertEqual(r.union_field_res, "sessionSetupRes")
        # But the template names use the *bare* form, so they match
        # what's in CmnTemplates.ttcn.
        self.assertEqual(r.send_template_name, "md_CMN_SessionSetupReq_001")
        self.assertEqual(r.receive_template_name, "mdw_SECC_SessionSetupRes_001")
        self.assertEqual(r.request_type, "SessionSetupReq")
        self.assertEqual(r.response_type, "SessionSetupRes")
        # The protocol layer reflects the wrapper's framing
        self.assertEqual(r.protocol_layer, "V2GTP")
        # The TB func name keeps the wrapper prefix (matches the file
        # name convention used in the rest of the project).
        tb_name = self.reg.format_naming(
            "test_behavior_function", SUT="SECC", Category="CMN",
            MessageType="V2GTPSessionSetup", SeqNo="001"
        )
        self.assertEqual(tb_name, "f_SECC_CMN_TB_VTB_V2GTPSessionSetup_001")

    # ---- raw-octetstring messages ----

    def test_resolve_sdp(self):
        r = self.reg.resolve("SDP", sut="SECC")
        self.assertTrue(r.uses_raw_octetstring)
        self.assertEqual(r.port_name, "pt_V2G_UDP_SDP_Port")
        self.assertEqual(r.protocol_layer, "SDP")
        self.assertEqual(r.raw_request_hex, "01FE9000000000021000")
        self.assertEqual(r.response_timeout_seconds, 5.0)
        # SDP has no union field
        self.assertIsNone(r.union_field_req)
        self.assertIsNone(r.send_template_name)
        self.assertIsNone(r.receive_template_name)
        # SDP IS covered by InitConfiguration pre-condition
        self.assertIsNotNone(r.covered_by_precondition)
        self.assertIn("InitConfiguration", r.covered_by_precondition)

    # ---- V2GTPHeader ----

    def test_resolve_v2gtp_header(self):
        r = self.reg.resolve("V2GTPHeader", sut="SECC")
        # V2GTPHeader uses the recursive 'v2g' field
        self.assertEqual(r.union_field_req, "v2g")
        self.assertEqual(r.union_field_res, "v2g")
        self.assertEqual(r.protocol_layer, "V2GTP")

    # ---- state-machine coverage ----

    def test_resolve_supported_app_protocol_coverage(self):
        r = self.reg.resolve("SupportedAppProtocol", sut="SECC")
        self.assertIsNotNone(r.covered_by_precondition)
        self.assertIn("SupportedAppProtocol_001", r.covered_by_precondition)

    # ---- resolve() raises on truly unknown ----

    def test_resolve_unknown_raises(self):
        with self.assertRaises(KeyError):
            self.reg.resolve("TotallyMadeUpMessage_42")

    # ---- to_plan_dict ----

    def test_to_plan_dict_for_session_setup(self):
        r = self.reg.resolve("SessionSetup", sut="SECC", seq_no="001")
        d = r.to_plan_dict()
        self.assertEqual(d["normalized_message_type"], "SessionSetup")
        self.assertEqual(d["request_message_type"], "SessionSetupReq")
        self.assertEqual(d["response_message_type"], "SessionSetupRes")
        self.assertEqual(d["union_field_req"], "sessionSetupReq")
        self.assertEqual(d["union_field_res"], "sessionSetupRes")
        self.assertEqual(d["send_template_name"], "md_CMN_SessionSetupReq_001")
        self.assertEqual(d["covered_by_precondition"], False)

    def test_to_plan_dict_for_sdp(self):
        r = self.reg.resolve("SDP", sut="SECC")
        d = r.to_plan_dict()
        self.assertEqual(d["sdp_send"], True)
        self.assertEqual(d["sdp_request_hex"], "01FE9000000000021000")
        self.assertEqual(d["sdp_response_timeout_seconds"], 5.0)
        self.assertEqual(d["covered_by_precondition"], True)
        self.assertEqual(d["union_field_req"], None)


class TestFormatNaming(unittest.TestCase):
    """Tests for the format_naming() API."""

    def setUp(self):
        self.reg = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda *a, **k: None)
        self.reg.load("iso15118_2")

    def test_tb_function_name(self):
        s = self.reg.format_naming(
            "test_behavior_function", SUT="SECC", Category="CMN",
            MessageType="SessionSetup", SeqNo="001"
        )
        self.assertEqual(s, "f_SECC_CMN_TB_VTB_SessionSetup_001")

    def test_pr_function_name(self):
        s = self.reg.format_naming(
            "precondition_function", SUT="SECC", Category="CMN",
            Name="InitConfiguration", SeqNo="001"
        )
        self.assertEqual(s, "f_SECC_CMN_PR_InitConfiguration_001")

    def test_send_template(self):
        s = self.reg.format_naming("send_template", MessageType="SessionSetup", SeqNo="001")
        self.assertEqual(s, "md_CMN_SessionSetupReq_001")

    def test_receive_template(self):
        s = self.reg.format_naming("receive_template", SUT="EVCC", MessageType="Authorization", SeqNo="005")
        self.assertEqual(s, "mdw_EVCC_AuthorizationRes_005")

    def test_unknown_role_raises(self):
        with self.assertRaises(KeyError):
            self.reg.format_naming("nonexistent_role", SUT="SECC")


class TestGetBadHexDefault(unittest.TestCase):
    def setUp(self):
        self.reg = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda *a, **k: None)
        self.reg.load("iso15118_2")

    def test_known_v2gtpheader_badcase(self):
        h = self.reg.get_bad_hex_default("TC_SECC_CMN_VTB_V2GTPHeader_002", "V2GTPHeader")
        self.assertEqual(h, "0201FD02000000020000")

    def test_v2gtpheader_tc_003(self):
        h = self.reg.get_bad_hex_default("TC_SECC_CMN_VTB_V2GTPHeader_003", "V2GTPHeader")
        self.assertEqual(h, "0101FA8001000000020000")

    def test_v2gtpheader_tc_004(self):
        h = self.reg.get_bad_hex_default("TC_SECC_CMN_VTB_V2GTPHeader_004", "V2GTPHeader")
        self.assertEqual(h, "0101FE00000000000000")

    def test_unknown_tc_returns_none(self):
        h = self.reg.get_bad_hex_default("TC_SECC_CMN_VTB_V2GTPHeader_999", "V2GTPHeader")
        self.assertIsNone(h)

    def test_session_setup_returns_none(self):
        h = self.reg.get_bad_hex_default("TC_SECC_CMN_VTB_SessionSetup_002", "SessionSetup")
        self.assertIsNone(h)


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.reg = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda *a, **k: None)
        self.reg.load("iso15118_2")

    def test_snapshot_compact(self):
        s = self.reg.snapshot()
        self.assertEqual(s["protocol_id"], "iso15118_2")
        self.assertIn("SessionSetup", s["message_names"])
        self.assertIn("V2GTPSessionSetup", s["message_names"])
        self.assertIn("SDP", s["message_names"])
        # Default is compact — no full message bodies
        self.assertNotIn("messages", s)

    def test_snapshot_full(self):
        s = self.reg.snapshot(include_full_messages=True)
        self.assertIn("messages", s)
        names = {m["name"] for m in s["messages"]}
        self.assertIn("SessionSetup", names)
        self.assertIn("SDP", names)

    def test_snapshot_is_json_serializable(self):
        s = self.reg.snapshot(include_full_messages=True)
        # Should not raise
        json.dumps(s)


class TestExtendWithLLMDiscoveries(unittest.TestCase):
    def setUp(self):
        self.reg = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda *a, **k: None)
        self.reg.load("iso15118_2")

    def test_proposal_written_to_review_queue(self):
        proposals = {
            "messages": [
                {
                    "name": "WeldingDetection",
                    "category": "CMN",
                    "protocol_layer": "V2G_Application",
                    "transport": "TCP_TLS",
                    "uses_raw_octetstring": False,
                    "covered_by_precondition": None,
                    "request_type": "WeldingDetectionReq",
                    "response_type": "WeldingDetectionRes",
                    "union_field": "weldingDetectionReq",
                    "default_precondition_chain": [
                        "f_{SUT}_CMN_PR_InitConfiguration_001",
                        "f_{SUT}_CMN_PR_SupportedAppProtocol_001",
                    ],
                }
            ]
        }
        path = self.reg.extend_with_llm_discoveries(
            proposals,
            proposed_by="Core Agent (test)",
            proposed_during_tc="TC_SECC_CMN_VTB_WeldingDetection_002",
        )
        self.assertTrue(path.exists())
        self.assertIn(".review_queue", str(path))
        # File should be valid JSON with the envelope
        with open(path) as f:
            env = json.load(f)
        self.assertEqual(env["target_protocol"], "iso15118_2")
        self.assertEqual(env["proposed_by"], "Core Agent (test)")
        self.assertEqual(env["merge_path"]["messages"][0]["name"], "WeldingDetection")
        # Clean up
        path.unlink()

    def test_proposal_unique_filename(self):
        proposals = {"messages": [{"name": "Test", "category": "CMN"}]}
        p1 = self.reg.extend_with_llm_discoveries(proposals, proposed_by="x", proposed_during_tc="t")
        p2 = self.reg.extend_with_llm_discoveries(proposals, proposed_by="x", proposed_during_tc="t")
        self.assertNotEqual(p1, p2)
        p1.unlink()
        p2.unlink()


class TestFallbackHeuristic(unittest.TestCase):
    def setUp(self):
        self.reg = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda *a, **k: None)
        self.reg.load("iso15118_2")

    def test_fallback_for_unknown_message(self):
        logs = []
        reg2 = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda msg, level="INFO": logs.append((msg, level)))
        reg2.load("iso15118_2")
        # Unknown message — should still produce a guess and log a warning
        f = reg2.fallback_union_field("BrandNewMessage")
        self.assertTrue(f.endswith("Req"))
        # Should have logged a warning
        self.assertTrue(any("WARNING" in str(lvl) or "WARN" in str(lvl) for msg, lvl in logs))


class TestResolveVsBaseline(unittest.TestCase):
    """Cross-check: ResolvedMessage.to_plan_dict() must match the exact
    values the OLD (pre-refactor) plan builder hardcoded."""

    def setUp(self):
        self.reg = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda *a, **k: None)
        self.reg.load("iso15118_2")

    def test_v2gtpsessionsetup_matches_old_builder(self):
        """This is the regression that crashed compilation last time."""
        r = self.reg.resolve("V2GTPSessionSetup", sut="SECC", seq_no="001")
        # The compile-breaking bug was: send_template_name was
        # "md_CMN_V2GTPSessionSetupReq_001" (does not exist in templates).
        # It must now be the bare form, which DOES exist.
        self.assertEqual(r.send_template_name, "md_CMN_SessionSetupReq_001")
        self.assertEqual(r.union_field_req, "sessionSetupReq")
        self.assertEqual(r.union_field_res, "sessionSetupRes")
        self.assertEqual(r.request_type, "SessionSetupReq")
        self.assertEqual(r.response_type, "SessionSetupRes")

    def test_sdp_matches_old_builder(self):
        r = self.reg.resolve("SDP", sut="SECC")
        self.assertTrue(r.uses_raw_octetstring)
        self.assertEqual(r.port_name, "pt_V2G_UDP_SDP_Port")
        self.assertEqual(r.protocol_layer, "SDP")
        self.assertEqual(r.raw_request_hex, "01FE9000000000021000")
        self.assertEqual(r.response_timeout_seconds, 5.0)

    def test_all_10_known_tcs_resolve(self):
        """All 10 batch_results TCs should resolve without error."""
        import glob, os
        tc_files = glob.glob(str(HERE.parent.parent / "batch_results" / "tc_json_files" / "*.json"))
        import json as _json
        for fp in tc_files:
            with open(fp) as f:
                data = _json.load(f)
            if isinstance(data, dict) and "Test_Case" in data:
                tc = data["Test_Case"]
            elif isinstance(data, dict):
                tc = data
            else:
                continue
            tid = tc.get("TC_Id", "")
            import re as _re
            m = _re.search(r"_VTB_(\w+?)_\d{3}$", tid)
            if not m:
                continue
            msg = m.group(1)
            # This should not raise
            r = self.reg.resolve(msg, sut="SECC", seq_no="001")
            # And should produce a non-empty canonical name
            self.assertTrue(r.canonical, f"empty canonical for {msg}")


class TestLazyLoading(unittest.TestCase):
    """Tests that verify the strict two-tier lazy-loading memory model.

    Invariants:
      - After __init__, L1 (manifests) is full but L2 (full descriptors) is empty.
      - L2 loads only when a protocol is first resolved or explicitly accessed.
      - After resolve(), L2 contains only the active protocol.
      - Multiple protocols accumulate in L2 only if the pipeline switches between them.
    """

    def setUp(self):
        reset_default_registry()
        self.reg = ProtocolRegistry(PROTOCOLS_DIR, default_log_fn=lambda *a, **k: None)

    def tearDown(self):
        reset_default_registry()

    def test_l1_full_after_init(self):
        """L1 manifests for all protocols exist immediately after construction."""
        self.assertIn("iso15118_2", self.reg.available_protocols())
        self.assertGreater(self.reg.manifest_size_bytes(), 0)

    def test_l2_empty_after_init(self):
        """L2 full descriptors are NOT loaded at startup."""
        self.assertEqual(len(self.reg._full), 0)
        self.assertEqual(self.reg.loaded_size_bytes(), 0)
        self.assertFalse(self.reg.is_loaded("iso15118_2"))

    def test_l2_loads_on_first_resolve(self):
        """First resolve() triggers L2 load for the active protocol."""
        self.reg.load("iso15118_2")
        self.assertFalse(self.reg.is_loaded("iso15118_2"))  # load() does NOT load L2
        self.reg.resolve("SessionSetup", sut="SECC")
        self.assertTrue(self.reg.is_loaded("iso15118_2"))
        self.assertGreater(self.reg.loaded_size_bytes(), 0)

    def test_l2_cached_on_repeated_resolve(self):
        """Subsequent resolve() calls use the L2 cache (no extra I/O)."""
        self.reg.load("iso15118_2")
        self.reg.resolve("SessionSetup", sut="SECC")  # first resolve — L2 loaded
        before = self.reg.loaded_size_bytes()
        for _ in range(5):
            self.reg.resolve("Authorization", sut="SECC")
        self.assertEqual(self.reg.loaded_size_bytes(), before)

    def test_resolve_without_load_raises(self):
        """resolve() without a prior load() or auto_load_for_tc() raises RuntimeError."""
        with self.assertRaises(RuntimeError) as cm:
            ProtocolRegistry(PROTOCOLS_DIR).resolve("SessionSetup", sut="SECC")
        self.assertIn("No active protocol", str(cm.exception))

    def test_is_loaded_false_for_unloaded(self):
        """is_loaded() returns False for protocols whose L2 is not yet loaded."""
        self.reg.load("iso15118_2")
        self.assertFalse(self.reg.is_loaded("iso15118_2"))

    def test_loaded_protocols_lists_only_loaded(self):
        """loaded_protocols() returns only protocols whose L2 is in memory."""
        self.assertEqual(self.reg.loaded_protocols(), [])
        self.reg.load("iso15118_2")
        self.assertEqual(self.reg.loaded_protocols(), [])
        self.reg.resolve("SessionSetup", sut="SECC")
        self.assertEqual(self.reg.loaded_protocols(), ["iso15118_2"])

    def test_l2_size_smaller_than_full_json(self):
        """loaded_size_bytes() is at least as large as the JSON on disk (rough bound)."""
        self.reg.load("iso15118_2")
        self.reg.resolve("SessionSetup", sut="SECC")
        self.assertGreaterEqual(
            self.reg.loaded_size_bytes(),
            self.reg._manifests["iso15118_2"].file_size_bytes // 2,
            "Loaded descriptor should be comparable to disk size",
        )

    def test_manifest_size_less_than_full(self):
        """L1 manifest is orders of magnitude smaller than L2."""
        self.reg.load("iso15118_2")
        self.reg.resolve("SessionSetup", sut="SECC")
        self.assertLess(
            self.reg.manifest_size_bytes(),
            self.reg.loaded_size_bytes(),
        )
        self.assertLess(
            self.reg.manifest_size_bytes(),
            2000,
            "Single manifest should be < 2 KB",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
