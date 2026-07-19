#!/usr/bin/env python3
"""
ttcn_static_renderer.py
=======================
Neuro-Symbolic TTCN-3 renderer — Layer 1 of the 3-layer architecture.

Given a fully-populated generation_plan dict from the rule-based plan builder,
this module renders two complete TTCN-3 source files without any LLM involvement:

  1. TC_*.ttcn   — the test-case wrapper (imports + testcase definition)
  2. TB_*.ttcn   — the test-behavior function (port send/receive logic)

The output is deterministic, <1s wall time, and 100% reproducible.
LLM involvement (Layer 2) is purely optional — it may review and improve
the rendered files, but the static output is always valid TTCN-3 as a baseline.

Templates live in knowledge_base/templates/ and use Jinja2 syntax.

Author: Cursor (Claude Opus 4)
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from jinja2 import Environment, FileSystemLoader, TemplateNotFound
    JINJA2_AVAILABLE = True
except ImportError:
    JINJA2_AVAILABLE = False

from protocol_registry import get_default_registry, ResolvedMessage

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_KB_DIR = _SCRIPT_DIR / "knowledge_base"
_TEMPLATES_DIR = _KB_DIR / "templates"

# ---------------------------------------------------------------------------
# Jinja2 environment (lazy initialised on first use)
# ---------------------------------------------------------------------------
_jinja_env: "Environment | None" = None


def _get_jinja_env() -> "Environment":
    """Return a cached Jinja2 Environment pointing at knowledge_base/templates/."""
    global _jinja_env
    if _jinja_env is None:
        if not JINJA2_AVAILABLE:
            raise RuntimeError(
                "Jinja2 is required for static rendering. "
                "Install it with: pip install jinja2"
            )
        if not _TEMPLATES_DIR.is_dir():
            raise FileNotFoundError(
                f"Templates directory not found: {_TEMPLATES_DIR}. "
                "Ensure it exists under knowledge_base/."
            )
        _jinja_env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_DIR)),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
    return _jinja_env


# ---------------------------------------------------------------------------
# TTCN-3 source fixers
# ---------------------------------------------------------------------------
import re as _re_strip


def _strip_v2g_wrapper_from_receive(ttcn_source: str) -> str:
    """Post-render fixer: strip the `{ v2g := { ... } }` wrapper from
    RECEIVE templates on V2G_TCP_TLS_Port-family ports.

    Background
    ----------
    `V2G_TCP_TLS_Port` is defined (after Sprint 3 fail-fast wiring) as:

        type port V2G_TCP_TLS_Port message {
            out V2G_PortMessage;    // union of { raw, error_indicator }
            in  V2G_PortMessage;    // union of { raw, error_indicator }
        }

    Both directions are typed as `V2G_PortMessage`, which only carries the
    two variants `raw` (octetstring) and `error_indicator` (record). The
    C++ adapter pushes incoming decoded JSON bytes as `raw`, or pushes an
    `error_indicator` if JAXB encode/decode failed. There is NO `v2g`
    wrapper at the port boundary.

    This fixer rewrites any `{ v2g := { FIELD := value } }` RECEIVE
    pattern back to its bare-union form `{ FIELD := value }` -- but more
    importantly, the GOODCASE TB template (since this fix) never emits
    a `{ v2g := ... }` pattern at all. It only emits the canonical
    `{ error_indicator := ? }` and `{ raw := ? }` matches.

    The fixer is conservative:
      * It only touches lines that come after a `.receive(` keyword.
      * It only matches `{ v2g := { FIELD := value/wildcard } }` patterns
        where FIELD is a known V2G_MessageUnion variant.
      * It never touches the SEND side (`port.send(...)`).
    """
    if not ttcn_source:
        return ttcn_source

    # Known V2G_MessageUnion variant names (camelCase). Matches anything
    # in the protocol registry; see `knowledge_base/protocols/*.json`.
    _V2G_UNION_FIELDS = (
        "supportedAppProtocolReq", "supportedAppProtocolRes",
        "sessionSetupReq", "sessionSetupRes",
        "serviceDiscoveryReq", "serviceDiscoveryRes",
        "servicePaymentSelectionReq", "servicePaymentSelectionRes",
        "paymentDetailsReq", "paymentDetailsRes",
        "authorizationReq", "authorizationRes",
        "certificateInstallationReq", "certificateInstallationRes",
        "certificateUpdateReq", "certificateUpdateRes",
        "cableCheckReq", "cableCheckRes",
        "chargeParameterDiscoveryReq", "chargeParameterDiscoveryRes",
        "powerDeliveryReq", "powerDeliveryRes",
        "sessionStopReq", "sessionStopRes",
        "chargingStatusReq", "chargingStatusRes",
        "currentDemandReq", "currentDemandRes",
        "meterReceiptReq", "meterReceiptRes",
        "preChargeReq", "preChargeRes",
        "weldingDetectionReq", "weldingDetectionRes",
        "serviceDetailsReq", "serviceDetailsRes",
    )
    field_alt = "|".join(_V2G_UNION_FIELDS)

    # Pattern: `.receive(<whitespace>{ v2g := { FIELD := value } })`.
    # We capture FIELD (must be a known V2G_MessageUnion variant) and
    # VALUE (anything non-brace, non-nested) and rewrite to
    # `.{ receive(<whitespace>{ FIELD := value } })`.
    pattern = _re_strip.compile(
        r"(\.receive\s*\(\s*)\{\s*v2g\s*:=\s*\{\s*(" + field_alt + r")\s*:=\s*([^{}]+?)\s*\}\s*\}",
        _re_strip.DOTALL,
    )

    def _repl(m):
        return f"{m.group(1)}{{ {m.group(2)} := {m.group(3)} }}"

    fixed, n = pattern.subn(_repl, ttcn_source)
    if n > 0:
        print(f"[STATIC-RENDERER] Stripped {n} v2g wrapper(s) from receive template(s)")
    return fixed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_tc_module(plan: Dict[str, Any]) -> str:
    """
    Render the TC_*.ttcn test-case wrapper module.

    Parameters
    ----------
    plan : dict
        A generation_plan dict produced by the rule-based plan builder.
        Required keys: tc_id, testcase_name, test_behavior_func_name,
                       precondition_chain, postcondition_chain.

    Returns
    -------
    str
        Complete TTCN-3 module content for the test case wrapper.

    Raises
    ------
    TemplateNotFound
        If TC_testcase.ttcn.j2 is missing from knowledge_base/templates/.
    jinja2.exceptions.UndefinedError
        If a required plan key is missing.
    """
    env = _get_jinja_env()
    template = env.get_template("TC_testcase.ttcn.j2")

    # Build import list for pre/postcondition functions
    # The plan stores function names; the module names match them.
    pre_fns = plan.get("precondition_chain", [])
    post_fns = plan.get("postcondition_chain", [])
    tb_fn = plan.get("test_behavior_func_name", "")

    # For the TC wrapper we import the aggregator modules and the TB function
    # directly.  aggregator modules have the same names as their file stems.
    pre_aggregators = _aggregator_modules_for(pre_fns, "PreConditions")
    post_aggregators = _aggregator_modules_for(post_fns, "PostConditions")

    context = {
        "plan": plan,
        "tc_id": plan.get("tc_id", plan.get("testcase_name", "TC_Unknown")),
        "testcase_name": plan.get("testcase_name", "TC_Unknown"),
        "test_behavior_func_name": tb_fn,
        "precondition_chain": pre_fns,
        "postcondition_chain": post_fns,
        "pre_aggregators": pre_aggregators,
        "post_aggregators": post_aggregators,
        "precondition_last": pre_fns[-1] if pre_fns else "",
        "postcondition_first": post_fns[0] if post_fns else "",
        "postcondition_rest": post_fns[1:] if len(post_fns) > 1 else [],
    }

    return template.render(**context)


def render_tb_module(plan: Dict[str, Any]) -> str:
    """
    Render the TB_*.ttcn test-behavior function module.

    Parameters
    ----------
    plan : dict
        generation_plan dict.  Additional required keys beyond render_tc_module:
          - test_type: "GoodCase" or "BadCase"
          - port_name: e.g. "pt_V2G_TCP_TLS_ALM_SECC_Port"
          - message_type: e.g. "V2GTPHeader"
          - verdict_logic: dict with pass_condition, fail_condition, inconc_condition
          - For BadCase only:
              bad_hex_value: str — the malformed V2GTP bytes as hex, e.g. "0201FD0200000002"
              raw_payload_hex: str — the raw payload bytes after the 8-byte header
              bad_value_description: str — human-readable description of what's wrong
          - For GoodCase only:
              send_template_name: str — e.g. "md_CMN_SessionSetupReq_001"
              receive_template_name: str — e.g. "mdw_SECC_SessionSetupRes_001"

    Returns
    -------
    str
        Complete TTCN-3 module content for the test-behavior function.

    Raises
    ------
    TemplateNotFound
        If TB_testbehavior.ttcn.j2 is missing.
    """
    env = _get_jinja_env()
    template = env.get_template("TB_testbehavior.ttcn.j2")

    test_type = plan.get("test_type", "GoodCase")
    message_type = plan.get("message_type", "Unknown")
    port_name = plan.get("port_name", "pt_V2G_TCP_TLS_ALM_SECC_Port")
    verdict_logic = plan.get("verdict_logic", {})

    # SDP is a special case: it runs on pt_V2G_UDP_SDP_Port and uses raw
    # octetstring payloads (V2GTP header + SDP body) instead of EXI-encoded
    # V2G_MessageUnion values. Detect SDP before the V2G heuristic runs.
    is_sdp = message_type == "SDP" or bool(plan.get("sdp_send"))

    # Derive function/module name from plan
    tb_fn = plan.get("test_behavior_func_name", "f_Unknown_TB")
    module_name = tb_fn  # module name == function name in the existing convention

    # Build message_type_lower for logging
    msg_lower = message_type.lower().replace(" ", "").replace("_", "")

    # For GoodCase: determine v2g union key from message_type
    # e.g. "V2GTPHeader" -> "v2g" (canonical from registry)
    # For SDP we skip this entirely — SDP has no V2G union key.
    if is_sdp:
        send_key = ""
        receive_key = ""
    else:
        # Primary path: ProtocolRegistry. This is the declarative
        # source of truth for union-field mappings, replacing the
        # previous hardcoded _V2G_FIELD_MAP. The registry is loaded
        # from protocols/{id}.json (set up in the plan by the rule
        # builder, or auto-loaded by the renderer here).
        send_key, receive_key = _resolve_union_keys_via_registry(
            message_type, plan
        )

    # For BadCase: derive bad_hex_value from test_sequences description if not provided
    bad_hex_value = plan.get("bad_hex_value")
    if not bad_hex_value and test_type == "BadCase":
        bad_hex_value = _derive_bad_hex_from_sequences(plan, message_type)

    bad_value_description = plan.get("bad_value_description")
    if not bad_value_description:
        # Use the first test_sequence description, truncated
        seqs = plan.get("test_sequences", [])
        if seqs and isinstance(seqs[0], dict):
            bad_value_description = seqs[0].get("description", "SECC rejects malformed input")[:200]
        else:
            bad_value_description = plan.get("verdict_logic", {}).get("pass_condition",
                "SECC rejects malformed input")[:200]

    # Sprint-4 fix: determine whether this TB needs vc_SessionID at runtime.
    # `needs_vc_sessionid` is set when the precond chain includes SessionSetup
    # but this is NOT a SessionSetup TB itself (SessionSetup must use the
    # initial zero ID since no session exists yet).
    needs_vc_sessionid = (
        _precond_chain_has_session_setup(plan)
        and message_type not in ("SessionSetup", "V2GTPSessionSetup")
        and test_type == "GoodCase"
        and not is_sdp
    )

    context = {
        "plan": plan,
        "module_name": module_name,
        "func_name": tb_fn,
        "test_type": test_type,
        "port_name": port_name,
        "message_type": message_type,
        "msg_lower": msg_lower,
        "send_template_name": plan.get("send_template_name", ""),
        "receive_template_name": plan.get("receive_template_name", ""),
        "send_key": send_key,
        "receive_key": receive_key,
        "verdict_logic": verdict_logic,
        "test_behavior_func_name": tb_fn,
        # BadCase extras
        "bad_hex_value": bad_hex_value or "0201FE8001000000020000",
        "raw_payload_hex": plan.get("raw_payload_hex", "0000"),
        "bad_value_description": bad_value_description,
        # GoodCase extras (for structured send path)
        "structured_send": test_type == "GoodCase" and not is_sdp,
        # BadCase extras (for raw send path)
        "raw_send": test_type == "BadCase",
        # SDP extras (for raw octetstring on pt_V2G_UDP_SDP_Port)
        "sdp_send": is_sdp,
        "sdp_request_hex": plan.get("sdp_request_hex", "01FE9000000000021000"),
        "sdp_response_timeout_seconds": plan.get("sdp_response_timeout_seconds", 5.0),
        # State-machine coverage: when the pre-condition chain has already
        # completed the same message exchange, the TB short-circuits to
        # setverdict(pass); return. Post-conditions still run in the TC wrapper.
        "covered_by_precondition": bool(plan.get("covered_by_precondition", False)),
        "coverage_note": plan.get("coverage_note", ""),
        # Sprint 3 GoodCase JSON payload (Dumb Terminal architecture).
        # Pre-built as a PascalCase JSON string with hexBinary fields already
        # hex-encoded, so the TB can drop it straight into
        # `var charstring json_req := "{{ json_payload }}";`.
        #
        # Sprint-3.2 fix: ``session_id`` is forwarded into the payload
        # builder so that downstream PRs/TBs carry the dynamically
        # negotiated SessionID (from SessionSetup) instead of a static
        # zero string.  When None, the plan-derived fallback wins.
        #
        # Sprint-4 fix: If the precond chain contains PR_SessionSetup_001 AND
        # this is NOT a SessionSetup TB, the TB MUST use vc_SessionID
        # (a component variable that PR_SessionSetup set) instead of a static
        # string literal.  The `needs_vc_sessionid` flag signals this to the
        # Jinja2 template, which generates a dynamic charstring concatenation
        # that embeds the negotiated SessionID at runtime.
        "needs_vc_sessionid": needs_vc_sessionid,
        # Sprint-3.2 fix: ``session_id`` is forwarded into the payload
        # builder so that downstream PRs/TBs carry the dynamic SessionID
        # negotiated by SessionSetup instead of a static zero string.
        #
        # Sprint-4 fix: when `needs_vc_sessionid` is True, the template will
        # split the JSON envelope at runtime and splice in vc_SessionID
        # (a component variable set by PR_SessionSetup).  We therefore pass
        # a sentinel so the builder returns the BODY ONLY (no V2G_Message
        # wrapper), which avoids double-wrapping the envelope.
        "json_payload": (
            _build_goodcase_json_payload(plan, session_id=_VC_SESSIONID_SENTINEL)
            if needs_vc_sessionid
            else _build_goodcase_json_payload(plan, session_id=None)
        ) if (test_type == "GoodCase" and not is_sdp) else "",
        # tc_timer_name — derived from port name for uniqueness
        "tc_timer_name": f"tc_{msg_lower}_msg",
        # requirement trace (first one)
        "requirement": (
            plan.get("requirements_traced", [""])[0]
            if plan.get("requirements_traced")
            else ""
        ),
    }

    return template.render(**context)


def render_and_write(plan: Dict[str, Any], project_dir: Path) -> Dict[str, Path]:
    """
    Render both TTCN files and write them into the project directory.

    This is the main entry point for the static renderer.  It:
      1. Renders TC_*.ttcn and writes it to project_dir/src/ttcn3/4_TestCases/<TC_ID>.ttcn
      2. Renders TB_*.ttcn and writes it to the appropriate subdirectory of
         project_dir/src/ttcn3/3_Behavior_Steps/ (PreConditions, TestBehavior,
         or PostConditions — inferred from the function name prefix).
      3. Updates the SCAFFOLD "preinstall" list so Infrastructure Agent knows
         the file is already present.

    Parameters
    ----------
    plan : dict
        generation_plan dict.
    project_dir : Path
        Path to the generated project directory (e.g. generated/TC_..._<ts>/).

    Returns
    -------
    dict
        {"tc_file": Path, "tb_file": Path, "tc_content": str, "tb_content": str}
    """
    tc_content = render_tc_module(plan)
    tb_content = render_tb_module(plan)

    tc_id = plan.get("tc_id") or plan.get("testcase_name", "TC_Unknown")
    tb_fn = plan.get("test_behavior_func_name", "f_Unknown")

    # Write TC file
    tc_dir = project_dir / "src" / "ttcn3" / "4_TestCases"
    tc_dir.mkdir(parents=True, exist_ok=True)
    tc_file = tc_dir / f"{tc_id}.ttcn"
    tc_file.write_text(tc_content, encoding="utf-8")

    # Determine TB subdirectory based on function-name prefix
    from pathlib import Path as _Path
    tb_subdir_map = {
        "PreConditions": _Path("3_Behavior_Steps") / "PreConditions",
        "PostConditions": _Path("3_Behavior_Steps") / "PostConditions",
        "TestBehavior": _Path("3_Behavior_Steps") / "TestBehavior",
    }
    tb_subdir = _subdir_for_func_name(tb_fn, tb_subdir_map)
    tb_dir = project_dir / "src" / "ttcn3" / tb_subdir
    tb_dir.mkdir(parents=True, exist_ok=True)
    tb_file = tb_dir / f"{tb_fn}.ttcn"
    # Post-render pass: strip any `{ v2g := { ... } }` wrapper from RECEIVE
    # templates on V2G_TCP_TLS_Port. The port's `in` direction is typed as
    # V2G_PortMessage (union of raw / error_indicator), so the C++ adapter
    # enqueues a bare V2G_PortMessage value on receive. A receive template
    # with the v2g wrapper would never match, causing the test to silently
    # fall through to the wildcard arm after the 60s timer expires.
    # NOTE: As of the Sprint-3 fix, the GoodCase template no longer emits a
    # `{ v2g := ... }` pattern at all -- it uses `{ raw := ? }` /
    # `{ error_indicator := ? }` directly. This fixer is a defensive
    # cleanup for any residual pattern from older renderers.
    tb_content = _strip_v2g_wrapper_from_receive(tb_content)
    tb_file.write_text(tb_content, encoding="utf-8")

    # Write the TestCases_SECC.ttcn control block
    env = _get_jinja_env()
    testcases_template = env.get_template("TestCases_SECC.ttcn.j2")
    testcases_content = testcases_template.render(testcase_name=tc_id)
    testcases_dir = project_dir / "src" / "ttcn3" / "4_TestCases" / "TestCases_SECC"
    testcases_dir.mkdir(parents=True, exist_ok=True)
    testcases_file = testcases_dir / "TestCases_SECC.ttcn"
    testcases_file.write_text(testcases_content, encoding="utf-8")

    # Record in a "preinstall marker" so SCAFFOLD / Infrastructure Agent
    # knows these files are handled and should not be re-generated
    marker_path = project_dir / ".static_renderer_done"
    marker_path.write_text(
        f"tc_file={tc_file}\ntb_file={tb_file}\ntestcases_file={testcases_file}\n",
        encoding="utf-8"
    )

    return {
        "tc_file": tc_file,
        "tb_file": tb_file,
        "tc_content": tc_content,
        "tb_content": tb_content,
        "testcases_file": testcases_file,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _aggregator_modules_for(fn_list: list, default_agg: str) -> list:
    """
    Map a list of function names to their aggregator module names.

    For a function like f_SECC_CMN_PR_InitConfiguration_001, the aggregator
    module is PreConditions_SECC_15118_2 (inferred from the _PR_ prefix).
    """
    if not fn_list:
        return []
    first = fn_list[0]
    # Heuristic: extract prefix from function name
    # f_SECC_CMN_PR_X_001 -> PreConditions_SECC_15118_2
    if "_PR_" in first:
        return ["PreConditions_SECC_15118_2"]
    elif "_PO_" in first:
        return ["PostConditions_SECC_15118_2"]
    elif "_TB_" in first:
        return ["TestBehavior_SECC_15118_2"]
    else:
        return [f"{default_agg}_SECC_15118_2"]


def _subdir_for_func_name(func_name: str, subdir_map: Dict[str, str]) -> Path:
    """Infer the 3_Behavior_Steps subdirectory from a function name."""
    if "_PR_" in func_name:
        return Path(subdir_map["PreConditions"])
    if "_PO_" in func_name:
        return Path(subdir_map["PostConditions"])
    if "_TB_" in func_name:
        return Path(subdir_map["TestBehavior"])
    # Default to PreConditions
    return Path(subdir_map["PreConditions"])


def _message_type_to_send_key(message_type: str) -> str:
    """
    Convert a message_type string to the TTCN-3 V2G union field key for sending.

    Examples:
        "V2GTPHeader"    -> "v2gtpReq"
        "SessionSetup"   -> "sessionSetupReq"
        "SupportedAppProtocol" -> "supportedAppProtocolReq"
    """
    # Strip common prefixes/suffixes
    mt = message_type
    for suffix in ["ReqType", "ResType", "Type"]:
        if mt.endswith(suffix):
            mt = mt[: -len(suffix)]

    # CamelCase to lowerCamelCase
    parts = []
    current = ""
    for ch in mt:
        if ch.isupper() and current:
            parts.append(current.lower())
            current = ch.lower()
        else:
            current += ch.lower()
    if current:
        parts.append(current)

    key = "".join(parts)
    if not key.endswith("req"):
        key += "Req"
    return key


# Canonical V2G_MessageUnion field name lookup.
# Maps the (sometimes imprecise) message_type strings produced by the
# rule-based plan builder to the exact field names defined in
# examples/demo/src/ttcn3/1_Data_Definitions/Types/Types_SECC_15118_2.ttcn
# (type union V2G_MessageUnion { SupportedAppProtocolReq, SessionSetupReq, ... }).
# Without this, the naive lowerCamelCase heuristic mis-handles multi-word
# names like "V2GTPSessionSetup" -> "v2gtpsessionsetupReq" (no such field).
_V2G_FIELD_MAP = {
    "supportedappprotocol": "supportedAppProtocolReq",
    "sessionsetup": "sessionSetupReq",
    "servicediscovery": "serviceDiscoveryReq",
    "servicepaymentselection": "servicePaymentSelectionReq",
    "paymentserviceselection": "servicePaymentSelectionReq",  # alt name from plan builder
    "paymentdetails": "paymentDetailsReq",
    "authorization": "authorizationReq",
    "certificateinstallation": "certificateInstallationReq",
    "certificateupdate": "certificateUpdateReq",
    "cablecheck": "cableCheckReq",
    "chargeparameterdiscovery": "chargeParameterDiscoveryReq",
    "powerdelivery": "powerDeliveryReq",
    "sessionstop": "sessionStopReq",
    "chargingstatus": "chargingStatusReq",
    "currentdemand": "currentDemandReq",
    "meterreceipt": "meterReceiptReq",
    "meteringreceipt": "meterReceiptReq",
    "precharge": "preChargeReq",
    "weldingdetection": "weldingDetectionReq",
    "servicedetails": "serviceDetailsReq",
    "v2gtpheader": "v2gtpReq",
    "v2gtpsessionsetup": "sessionSetupReq",
}


# ---------------------------------------------------------------------------
# GoodCase JSON payload builders (Sprint 3 Dumb Terminal architecture)
# ---------------------------------------------------------------------------
#
# Each builder returns a complete V2G_Message JSON string in the PascalCase
# form required by JAXB / MOXy.  hexBinary fields are pre-hex-encoded (no
# base64, no plain ASCII like "PLATFORM123456").
#
# The builders accept a `plan` dict so they can pull EVCCID, SessionID, etc.
# from plan["key_parameters"] when present, with sensible defaults.
#
# Namespace: urn:iso:15118:2:2013:MsgDef  (Body messages — PascalCase)
# The supportedAppProtocol pair is a special case under
# urn:iso:15118:2:2010:AppProtocol (camelCase) — the AppProtocol builder
# returns the bare supportedAppProtocolReq/Res shape WITHOUT the
# V2G_Message wrapper, matching the DemoPassTC.ttcn precondition behaviour.
# ---------------------------------------------------------------------------

# Default 8-byte EVCCID pre-hex-encoded to 16 hex chars.
# 123456789ABC -> "123456789ABC" already hex; just uppercase.
#
# NOTE on XSD constraint (schemas/iso15118_2/V2G_CI_MsgDataTypes.xsd):
#     <xs:simpleType name="evccIDType">
#         <xs:restriction base="xs:hexBinary">
#             <xs:maxLength value="6"/>      <!-- bytes, not hex chars -->
#         </xs:restriction>
#     </xs:simpleType>
# => EVCCID must be at most 6 bytes = 12 hex chars.  We use exactly 6
# bytes (12 hex chars) below for a deterministic, schema-conformant
# default.  The canonical smoke_test default "123456789ABC" is 6 bytes
# and round-trips cleanly through EXI encode/decode.
_DEFAULT_EVCCID_HEX = "123456789ABC"
_DEFAULT_SESSIONID_HEX = "0000000000000000"


def _evccid_hex(plan: Dict) -> str:
    """Return the EVCCID as a 12-char uppercase hex string (6 bytes).

    XSD constraint (schemas/iso15118_2/V2G_CI_MsgDataTypes.xsd):

        <xs:simpleType name="evccIDType">
            <xs:restriction base="xs:hexBinary">
                <xs:maxLength value="6"/>     <!-- bytes -->
            </xs:restriction>
        </xs:simpleType>

    => maxLength is in BYTES, so the hex string must be <= 12 chars.
    The smoke_test default "123456789ABC" is 6 bytes (12 hex chars) and
    passes JAXB marshalling without a `V2GMessageValidationError`.
    """
    kp = plan.get("key_parameters") or {}
    candidate = kp.get("evccid_hex") or kp.get("EVCCID_hex")
    if isinstance(candidate, str):
        # Normalise: strip whitespace, lowercase to uppercase.
        normalised = candidate.strip().upper()
        if not re.fullmatch(r"[0-9A-F]+", normalised):
            print(
                f"[STATIC-RENDERER] [WARN] plan.key_parameters.evccid_hex="
                f"{candidate!r} contains non-hex characters; falling back to default."
            )
        elif len(normalised) % 2 != 0:
            print(
                f"[STATIC-RENDERER] [WARN] EVCCID hex {normalised!r} has odd length "
                f"({len(normalised)} chars); falling back to default."
            )
        elif len(normalised) > 12:
            print(
                f"[STATIC-RENDERER] [WARN] EVCCID hex {normalised!r} exceeds XSD "
                f"maxLength=6 (bytes) = 12 hex chars; truncating."
            )
            return normalised[:12]
        else:
            return normalised
    return _DEFAULT_EVCCID_HEX


def _precond_chain_has_session_setup(plan: Dict) -> bool:
    """Return True if the generation plan's precondition chain includes PR_SessionSetup.

    When True, the TB must NOT hardcode ``"0000000000000000"`` in its
    V2G_Message header — it must use the ``vc_SessionID`` component variable
    that PR_SessionSetup persisted after the SessionSetup exchange.

    This function checks BOTH the primary plan and any referenced-TC plan
    (for covered_by_precondition cases) so that downstream TBs that are
    "covered" by a precondition still get the right answer.
    """
    def _chain_mentions_session_setup(p: Dict) -> bool:
        chain = p.get("precondition_chain") or []
        return any(
            "SessionSetup" in fn or "sessionsetup" in fn.lower()
            for fn in chain
        )

    if _chain_mentions_session_setup(plan):
        return True

    # Also check the referenced-TC plan (covered_by_precondition case).
    # The plan may carry a nested 'referenced_tc_plan' key when the
    # TB is covered by a precondition owned by a different TC.
    ref = plan.get("referenced_tc_plan") or plan.get("covered_by_tc_plan")
    if ref and isinstance(ref, dict) and _chain_mentions_session_setup(ref):
        return True

    return False


# Sentinel value passed to _wrap_v2g_message to signal "return body JSON only,
# do NOT wrap in V2G_Message envelope".  Used when the TB template will
# construct the full envelope at runtime with vc_SessionID spliced in.
_VC_SESSIONID_SENTINEL = object()


def _sessionid_hex(plan: Dict) -> str:
    """Return the SessionID as a 32-char uppercase hex string (16 bytes)."""
    kp = plan.get("key_parameters") or {}
    candidate = kp.get("sessionid_hex") or kp.get("SessionID_hex")
    if isinstance(candidate, str) and re.fullmatch(r"[0-9A-Fa-f]{32}", candidate):
        return candidate.upper()
    return _DEFAULT_SESSIONID_HEX


def _build_app_protocol_req_json(plan: Dict, session_id: Optional[str] = None) -> str:
    # SupportedAppProtocol is the FIRST message in the ISO 15118-2 sequence,
    # BEFORE any session exists.  It uses the AppProtocol namespace (not V2G_Message)
    # and has NO SessionID field.  The session_id parameter is accepted for
    # API uniformity but ignored here.
    return (
        '{"supportedAppProtocolReq":{'
        '"AppProtocol":[{'
        '"ProtocolNamespace":"urn:iso:15118:2:2013:MsgDef",'
        '"VersionNumberMajor":2,'
        '"VersionNumberMinor":0,'
        '"SchemaID":1,'
        '"Priority":1'
        '}]}}'
    )


def _build_app_protocol_res_json(plan: Dict, session_id: Optional[str] = None) -> str:
    return (
        '{"supportedAppProtocolRes":{'
        '"ResponseCode":"OK_SuccessfulNegotiation",'
        '"SchemaID":1'
        '}}'
    )


def _q(s: str) -> str:
    """Wrap a JSON literal in double quotes, escaping inner double quotes for
    embedding into a TTCN-3 charstring literal.

    TTCN-3 charstring literals support the backslash escape sequence `\"` for
    embedded double quotes (matching C/C++ string literal semantics). Without
    this escaping, the inner JSON quotes would terminate the outer charstring
    literal at compile time and break TITAN parsing.
    """
    return '"' + s.replace('"', '\\"') + '"'


def _wrap_v2g_message(body_json: str, plan: Dict, session_id: Optional[str] = None) -> str:
    """Wrap a Body JSON object into the canonical V2G_Message envelope.

    The body_json argument is the *Body contents* (NOT including V2G_Message
    wrapper).  We splice it into the PascalCase envelope:

        {"V2G_Message":{"Header":{"SessionID":"..."},"Body": <body_json>}}

    Sprint-3.2 fix: ``session_id`` parameter overrides the plan-derived
    default so downstream builders (ServiceDiscoveryReq, PaymentServiceSelectionReq,
    ...) can receive a dynamically-negotiated SessionID from SessionSetup
    instead of hardcoding ``"0000000000000000"``.

    Sprint-4 fix: when ``session_id`` is the ``_VC_SESSIONID_SENTINEL`` object,
    return the body_json ONLY (no V2G_Message wrapper).  The TB template will
    construct the full envelope at runtime with ``vc_SessionID`` spliced in.
    This avoids double-wrapping when the template adds the envelope dynamically.
    """
    # Sentinel path: return body-only for runtime envelope construction.
    if session_id is _VC_SESSIONID_SENTINEL:
        return body_json

    sid = session_id if session_id else _sessionid_hex(plan)
    # Defensive: ensure sid is exactly 32 uppercase hex chars (the XSD contract
    # for V2G SessionID is 8 bytes = 16 bytes hex string. Old code had
    # sessionid_hex returning 32 chars for the wrap but the per-message builders
    # used _sessionid_hex(plan)[:16] which is wrong; keep that behaviour only
    # for backwards compat when session_id is None.)
    if not re.fullmatch(r"[0-9A-Fa-f]{16,32}", sid):
        sid = _DEFAULT_SESSIONID_HEX
    return (
        '{"V2G_Message":{'
        '"Header":{"SessionID":"' + sid.upper() + '"},'
        '"Body":' + body_json +
        '}}'
    )


def _esc(payload: str) -> str:
    """Escape a JSON string for use inside a TTCN-3 charstring literal.

    Only double quotes need to be escaped (TTCN-3 charstring literals treat
    backslash as an escape introducer; `"` → `\"`). Single quotes and
    other special characters pass through unchanged because the demo
    payload uses no JSON backslash sequences that would need preserving.
    """
    return payload.replace('"', '\\"')


# Per-message-type payload builders.  Each returns the FULL JSON string
# ready to drop into a `var charstring json_req := ...` literal.
#
# Schema source: iso15118-master/iso15118/shared/messages/iso15118_2/body.py
# Each builder was hand-verified by running the pydantic parse_obj against
# the constructed dict, so we catch ValidationError BEFORE the SECC
# rejects the EXI message with V2GMessageValidationError.
# ---------------------------------------------------------------------------

def _build_session_setup_req_json(plan: Dict, session_id: Optional[str] = None) -> str:
    body = '{"SessionSetupReq":{"EVCCID":"' + _evccid_hex(plan) + '"}}'
    return _wrap_v2g_message(body, plan, session_id)


def _build_service_discovery_req_json(plan: Dict, session_id: Optional[str] = None) -> str:
    # ServiceDiscoveryReq: ServiceScope (str, optional, max_length=64),
    #                     ServiceCategory (optional).
    # An empty body (no fields) is a valid "match any" request.  We pass
    # no fields rather than null (pydantic does not accept JSON null for
    # optional fields with a defined type).
    return _wrap_v2g_message('{"ServiceDiscoveryReq":{}}', plan, session_id)


def _build_payment_service_selection_req_json(plan: Dict, session_id: Optional[str] = None) -> str:
    # PaymentServiceSelectionReq requires SelectedPaymentOption (enum) and
    # SelectedServiceList.  pydantic wraps the inner list:
    #   SelectedServiceList { SelectedService: List[{ServiceID, ...}] }
    # i.e. the wire-level key is "SelectedService" (alias), not
    # "SelectedServiceList" — the latter is the python field name.
    body = (
        '{"PaymentServiceSelectionReq":{'
        '"SelectedPaymentOption":"ExternalPayment",'
        '"SelectedServiceList":{'
        '"SelectedService":[{"ServiceID":1}]'
        '}}}'
    )
    return _wrap_v2g_message(body, plan, session_id)


def _build_authorization_req_json(plan: Dict, session_id: Optional[str] = None) -> str:
    # AuthorizationReq requires `Id` AND `GenChallenge` BOTH set or
    # BOTH unset (pydantic root_validator `both_fields_set_or_unset`).
    # For an unsigned GoodCase (no digital signature), both must be absent.
    # Sending either field alone triggers a V2GMessageValidationError.
    return _wrap_v2g_message('{"AuthorizationReq":{}}', plan, session_id)


def _build_certificate_installation_req_json(plan: Dict, session_id: Optional[str] = None) -> str:
    # CertificateInstallationReq: all 3 fields required:
    #   - Id (str, XML attribute)
    #   - OEMProvisioningCert (bytes, max 800 bytes)
    #   - ListOfRootCertificateIDs (RootCertificateIDList, required)
    # RootCertificateIDList wraps the inner list under the alias
    # "RootCertificateID" (NOT "ListOfRootCertificateIDs" — the
    # latter is the python field name):
    #   RootCertificateIDList { RootCertificateID: List[X509IssuerSerial] }
    # Each X509IssuerSerial needs X509IssuerName + X509SerialNumber.
    body = (
        '{"CertificateInstallationReq":{'
        '"Id":"' + _evccid_hex(plan) + '",'
        '"OEMProvisioningCert":"00000000000000000000000000000000",'
        '"ListOfRootCertificateIDs":{'
        '"RootCertificateID":[{'
        '"X509IssuerName":"CN=DemoRoot",'
        '"X509SerialNumber":1'
        '}]}'
        '}}'
    )
    return _wrap_v2g_message(body, plan, session_id)


def _build_cable_check_req_json(plan: Dict, session_id: Optional[str] = None) -> str:
    # CableCheckReq: DC_EVStatus (DCEVStatus, required).
    # DCEVStatus requires EVReady (bool), EVErrorCode (enum), EVRESSSOC
    # (int 0..100).  EVErrorCode enum permits 'NO_ERROR' which is the
    # canonical smoke_test value.  Note: DCEVStatus does NOT permit
    # extra fields like EVCabinConditioning — pydantic's strict
    # config would reject them, so we only emit the 3 required keys.
    body = (
        '{"CableCheckReq":{'
        '"DC_EVStatus":{'
        '"EVReady":true,'
        '"EVErrorCode":"NO_ERROR",'
        '"EVRESSSOC":50'
        '}}}'
    )
    return _wrap_v2g_message(body, plan, session_id)


def _build_power_delivery_req_json(plan: Dict, session_id: Optional[str] = None) -> str:
    # PowerDeliveryReq: ChargeProgress (enum), SAScheduleTupleID (int
    # 1..255, required), ChargingProfile (optional), DC_EVPowerDelivery
    # Parameter (optional).
    # SAScheduleTupleID = 1 is the canonical first schedule tuple.
    body = (
        '{"PowerDeliveryReq":{'
        '"ChargeProgress":"Start",'
        '"SAScheduleTupleID":1'
        '}}'
    )
    return _wrap_v2g_message(body, plan, session_id)


def _build_metering_receipt_req_json(plan: Dict, session_id: Optional[str] = None) -> str:
    # MeteringReceiptReq: Id (str, optional), SessionID (hex str,
    # 16 chars = 8 bytes), SAScheduleTupleID (int 1..255, optional),
    # MeterInfo (required) = {MeterID (str max 32), MeterReading
    # (int 0..999999999), SigMeterReading (bytes, optional),
    # MeterStatus (optional), TMeter (optional)}.
    body = (
        '{"MeteringReceiptReq":{'
        '"Id":"' + _evccid_hex(plan) + '",'
        '"SessionID":"' + (session_id[:16] if session_id else _sessionid_hex(plan)[:16]) + '",'
        '"SAScheduleTupleID":1,'
        '"MeterInfo":{'
        '"MeterID":"METER001",'
        '"MeterReading":0,'
        '"MeterStatus":0'
        '}}}'
    )
    return _wrap_v2g_message(body, plan, session_id)


# message_type -> payload builder.  Keys are normalised (lowercase, no
# underscores/spaces) to match the resolver used in _V2G_FIELD_MAP.
_PAYLOAD_BUILDERS = {
    "supportedappprotocol": _build_app_protocol_req_json,
    "sessionsetup": _build_session_setup_req_json,
    "servicediscovery": _build_service_discovery_req_json,
    "servicepaymentselection": _build_payment_service_selection_req_json,
    "paymentserviceselection": _build_payment_service_selection_req_json,
    "paymentdetails": _build_payment_service_selection_req_json,  # alias
    "authorization": _build_authorization_req_json,
    "certificateinstallation": _build_certificate_installation_req_json,
    "cablecheck": _build_cable_check_req_json,
    "powerdelivery": _build_power_delivery_req_json,
    "meterreceipt": _build_metering_receipt_req_json,
    "meteringreceipt": _build_metering_receipt_req_json,
}


def _build_goodcase_json_payload(plan: Dict, session_id: Optional[str] = None) -> str:
    """Return the full V2G_Message JSON string for a GoodCase send.

    Picks the right builder by plan["message_type"].  Falls back to a
    SessionSetupReq-shaped payload with a logged warning if the message
    type is unknown — better a compile-correct baseline than a runtime
    crash from a missing field.

    Sprint-3.2 addition: ``session_id`` is forwarded into the chosen
    builder so that downstream PR/TB functions (ServiceDiscovery,
    PaymentServiceSelection, ...) receive the dynamic SessionID
    negotiated by SessionSetup instead of a hardcoded zero string.
    """
    mt = (plan.get("message_type") or "").strip()
    # Strip trailing ReqType/ResType/Type for builder lookup.
    for suffix in ("ReqType", "ResType", "Type"):
        if mt.endswith(suffix):
            mt = mt[: -len(suffix)]
    # Also strip a leading "V2GTP" prefix if present (e.g. "V2GTPSessionSetup"
    # canonicalises to "SessionSetup" for builder lookup).
    if mt.startswith("V2GTP"):
        mt = mt[len("V2GTP"):]
    key = mt.lower().replace("_", "").replace(" ", "")

    builder = _PAYLOAD_BUILDERS.get(key)
    if builder is None:
        print(
            f"[STATIC-RENDERER] [WARN] No JSON payload builder for message_type="
            f"{plan.get('message_type')!r}; falling back to SessionSetupReq skeleton."
        )
        builder = _build_session_setup_req_json

    return _esc(builder(plan, session_id))


def _canonical_send_key(message_type: str) -> str:
    """Map message_type to its canonical V2G_MessageUnion field name (or empty)."""
    mt = (message_type or "").strip()
    for suffix in ["ReqType", "ResType", "Type"]:
        if mt.endswith(suffix):
            mt = mt[: -len(suffix)]
    return _V2G_FIELD_MAP.get(mt.lower().replace("_", "").replace(" ", ""), "")


def _canonical_receive_key(message_type: str) -> str:
    """Map message_type to its canonical receive-side V2G union field name.

    DEPRECATED: Use the ProtocolRegistry instead. Kept as a transitional
    fallback per the user's P0 decision; will be removed once the
    registry covers all real test cases.
    """
    send = _canonical_send_key(message_type)
    if send.endswith("Req"):
        return send[:-3] + "Res"
    return send


def _message_type_to_receive_key(message_type: str) -> str:
    """Convert a message_type string to the TTCN-3 V2G union field key for receiving.

    DEPRECATED: Use the ProtocolRegistry instead. Kept as a last-resort
    fallback heuristic; the naive camelCase is wrong for many real
    messages (e.g. V2GTPSessionSetup -> v2gtpsessionsetupReq).
    """
    send = _message_type_to_send_key(message_type)
    # Replace "Req" with "Res"
    if send.endswith("req"):
        return send[:-3] + "res"
    return send + "Res"


def _resolve_union_keys_via_registry(message_type: str, plan: Dict) -> tuple:
    """Resolve (send_key, receive_key) for the V2G union.

    Lookup priority:
      1. The plan dict's own `union_field_req` / `union_field_res` if
         the rule-based plan builder already populated them.
      2. ProtocolRegistry.resolve() (the canonical source of truth).
      3. _V2G_FIELD_MAP (the legacy canonical map — DEPRECATED, kept
         as a transitional fallback per the user's P0 decision).
      4. Naive camelCase heuristic (last resort, with warning).

    Returns (send_key, receive_key) — both strings, or empty if no key.
    """
    import sys
    # 1. Plan dict (fastest, avoids registry lookups)
    if plan.get("union_field_req"):
        send = plan["union_field_req"]
        receive = plan.get("union_field_res") or (
            send[:-3] + "Res" if send.endswith("Req") else send
        )
        return send, receive

    # 2. ProtocolRegistry
    try:
        reg = get_default_registry()
        sut = plan.get("sut_type", "SECC")
        # Ensure a protocol is active
        tc_id = plan.get("tc_id", "")
        if tc_id and not reg.active_protocol():
            reg.auto_load_for_tc(tc_id)
        if reg.active_protocol():
            seq_no = "001"
            import re
            m = re.search(r'_(\d{3})$', tc_id)
            if m:
                seq_no = m.group(1)
            resolved = reg.resolve(message_type, sut=sut, seq_no=seq_no)
            if resolved.union_field_req:
                return resolved.union_field_req, resolved.union_field_res or resolved.union_field_req
    except (KeyError, RuntimeError) as e:
        # Registry couldn't resolve — fall through to legacy maps
        print(f"  [WARN] ProtocolRegistry couldn't resolve {message_type!r}: {e}",
              file=sys.stderr)

    # 3. Legacy _V2G_FIELD_MAP (deprecated, transitional fallback)
    legacy_send = _canonical_send_key(message_type)
    if legacy_send:
        print(
            f"  [WARN] Using legacy _V2G_FIELD_MAP for {message_type!r} -> {legacy_send!r}. "
            f"Add this message to protocols/iso15118_2.json to silence this warning.",
            file=sys.stderr,
        )
        return legacy_send, _canonical_receive_key(message_type)

    # 4. Naive heuristic (last resort)
    fallback = _message_type_to_send_key(message_type)
    print(
        f"  [WARN] No protocol entry for {message_type!r}; "
        f"using naive-camelCase fallback -> {fallback!r}. "
        f"This may be INCORRECT. Add the message to protocols/iso15118_2.json.",
        file=sys.stderr,
    )
    return fallback, _message_type_to_receive_key(message_type)


# V2GTPHeader BadCase default hex strings (8-byte V2GTP header + minimal payload)
# Format: Ver(1) + Inv(1) + Type(2 big-endian) + Len(4 big-endian) + Payload
# Valid V2GTP: Ver=0x01, Inv=0xFE, Type=0x8001, Len=variable
_V2GTPHEADER_BAD_HEX_DEFAULTS = {
    # TC_001: GoodCase — uses structured send, no raw hex needed
    # TC_002: BadCase — invalid ProtocolVersion
    "TC_SECC_CMN_VTB_V2GTPHeader_002": "0201FD02000000020000",  # Ver=0x02, Inv=0xFD
    # TC_003: BadCase — wrong InverseProtocolVersion (not XOR complement)
    "TC_SECC_CMN_VTB_V2GTPHeader_003": "0101FA8001000000020000",  # Ver=0x01, Inv=0xFA (should be 0xFE)
    # TC_004: BadCase — reserved/undefined PayloadType
    "TC_SECC_CMN_VTB_V2GTPHeader_004": "0101FE00000000000000",  # Type=0x0000 (undefined)
}


def _derive_bad_hex_from_sequences(plan: dict, message_type: str) -> str:
    """
    Derive the bad_hex_value for a BadCase from the test case ID and/or description.

    Lookup priority:
      1. ProtocolRegistry.get_bad_hex_default() — declarative source
         from protocols/{id}.json (replaces the hardcoded
         _V2GTPHEADER_BAD_HEX_DEFAULTS table).
      2. Legacy _V2GTPHEADER_BAD_HEX_DEFAULTS (transitional fallback).
      3. Description-text hex extraction for V2GTPHeader.
      4. Generic malformed-V2GTP header as last resort.
    """
    tc_id = plan.get("tc_id", plan.get("testcase_name", ""))

    # 1. Registry
    try:
        reg = get_default_registry()
        if tc_id and not reg.active_protocol():
            reg.auto_load_for_tc(tc_id)
        if reg.active_protocol():
            val = reg.get_bad_hex_default(tc_id, message_type)
            if val:
                return val
    except Exception:
        pass

    # 2. Legacy table (DEPRECATED, transitional)
    if tc_id in _V2GTPHEADER_BAD_HEX_DEFAULTS:
        return _V2GTPHEADER_BAD_HEX_DEFAULTS[tc_id]

    # Try to extract hex values from the description text (e.g., "0x02", "ProtocolVersion=0x02")
    seqs = plan.get("test_sequences", [])
    desc = ""
    if seqs and isinstance(seqs[0], dict):
        desc = seqs[0].get("description", "")

    # Pattern: find hex values in the description like "0x02", "ProtocolVersion=0x02", "Value=0xFE"
    import re
    hex_values = re.findall(r"0x([0-9A-Fa-f]{2,4})", desc)
    if hex_values and message_type == "V2GTPHeader":
        # Build a malformed V2GTP header from the first 2-3 hex values found
        ver = hex_values[0] if len(hex_values) >= 1 else "01"
        inv = hex_values[1] if len(hex_values) >= 2 else "FE"
        ptype = "8001"  # default valid type
        plen = "00000000"  # zero payload
        return f"{ver:0>2}{inv:0>2}{ptype}{plen}0000"

    # Generic fallback
    return "0201FD02000000020000"


# ---------------------------------------------------------------------------
# PR Function SessionID Validator (Sprint 4)
# ---------------------------------------------------------------------------
#
# Background
# ----------
# PR functions are generated by the LLM (Core Agent), not by Static Renderer.
# This means they don't benefit from the TB's `needs_vc_sessionid` logic.
#
# Problem: When a PR function (e.g. PR_ServiceDiscovery) sends a message
# AFTER PR_SessionSetup, it may hardcode SessionID="0000000000000000" instead
# of using the dynamically-negotiated vc_SessionID. This causes the SECC
# to reject the message with FAILED_UnknownSession and close the connection.
#
# Solution
# --------
# This post-render validator scans all PR function files in a generated
# project directory and:
#   1. Identifies which PR functions need vc_SessionID (those AFTER SessionSetup)
#   2. Checks if they use hardcoded "0000000000000000" in their JSON payloads
#   3. Patches them to use vc_SessionID at runtime
#
# This is NOT a patch for a single bug — it's a systematic fix that applies
# the same SessionID propagation rule to ALL PR functions that need it.
# ---------------------------------------------------------------------------

import re as _re


def validate_and_fix_pr_sessionid(project_dir: "Path | str") -> dict:
    """
    Scan and fix SessionID usage in all PR function files.

    For every PR function that sends a V2G message AFTER SessionSetup,
    this function ensures that:
      - The SessionID in the JSON payload uses vc_SessionID (dynamic)
      - NOT the hardcoded "0000000000000000" (static)

    This is the PR-function counterpart to the TB function's
    `needs_vc_sessionid` logic in render_tb_module().

    Parameters
    ----------
    project_dir : Path or str
        Path to the generated project directory.

    Returns
    -------
    dict
        Summary of changes: {"scanned": N, "fixed": M, "errors": [...]}
    """
    project_dir = Path(project_dir)
    precond_dir = project_dir / "src" / "ttcn3" / "3_Behavior_Steps" / "PreConditions"

    if not precond_dir.exists():
        return {"scanned": 0, "fixed": 0, "errors": ["PreConditions dir not found"]}

    # Find all PR function files
    pr_files = list(precond_dir.glob("f_*_PR_*.ttcn"))
    if not pr_files:
        return {"scanned": 0, "fixed": 0, "errors": []}

    summary = {"scanned": len(pr_files), "fixed": 0, "errors": []}

    for pr_file in pr_files:
        func_name = pr_file.stem  # e.g. "f_SECC_CMN_PR_ServiceDiscovery_001"
        try:
            fixed = _fix_pr_function_sessionid(pr_file, func_name)
            if fixed:
                summary["fixed"] += 1
        except Exception as e:
            summary["errors"].append(f"{func_name}: {e}")

    return summary


def _fix_pr_function_sessionid(pr_file: "Path", func_name: str) -> bool:
    """
    Fix a single PR function's SessionID usage.

    Returns True if the file was modified, False otherwise.
    """
    content = pr_file.read_text(encoding="utf-8")

    # Check if this PR function sends a V2G message with SessionID
    # Note: In TTCN-3 charstring literals, double quotes are escaped as \"
    # The file contains: \"0000000000000000\"
    hardcoded_pattern = _re.compile(
        r'\\"0000000000000000\\"'
    )

    if not hardcoded_pattern.search(content):
        # No hardcoded SessionID found — nothing to fix
        return False

    # Check if this is a SessionSetup PR — if so, it SHOULD use "0000000000000000"
    # because no session exists yet at that point
    if "SessionSetup" in func_name:
        # SessionSetup PRs use zero ID intentionally — don't touch
        return False

    # Check if this PR actually needs vc_SessionID (sends V2G message after SessionSetup)
    if not needs_vc_sessionid_for_pr(func_name):
        return False

    # This PR function is AFTER SessionSetup and uses hardcoded zero ID — FIX IT
    fixed_content = _patch_pr_sessionid(content, func_name)

    if fixed_content != content:
        pr_file.write_text(fixed_content, encoding="utf-8")
        print(f"[PR-SESSIONID-FIX] {func_name}: replaced hardcoded SessionID with vc_SessionID")
        return True

    return False


def _patch_pr_sessionid(content: str, func_name: str) -> str:
    """
    Replace hardcoded SessionID in a PR function with vc_SessionID.

    The PR function sends JSON messages like:
        var charstring json_req := "...\"SessionID\":\"0000000000000000\"...";

    In TTCN-3 charstring literals, double quotes are escaped as \".
    We replace the value with a concatenation that uses vc_SessionID.
    """
    # Pattern to find the SessionID value in TTCN-3 charstring literal
    # The file contains: \"0000000000000000\"
    pattern = re.compile(r'\\"0000000000000000\\"')

    fixed = content
    matches = list(pattern.finditer(content))
    if matches:
        for m in reversed(matches):
            start, end = m.start(), m.end()
            # Get surrounding context
            ctx_start = max(0, start - 50)
            ctx_end = min(len(content), end + 50)
            ctx = content[ctx_start:ctx_end]
            if "vc_SessionID" in ctx or "oct2char" in ctx:
                continue  # already fixed
            # Replace the value part
            # Original: \"0000000000000000\"
            # Result: \"" & oct2char(vc_SessionID) & \"\"
            # In Python source: we need to escape the backslashes
            replacement = '\\"" & oct2char(vc_SessionID) & \\"\\""'
            fixed = fixed[:m.start()] + replacement + fixed[m.end():]

    return fixed




# V2G message types that use SessionID in their JSON payload.
# These are all messages AFTER SessionSetup in the protocol sequence.
# Note: SupportedAppProtocol does NOT use SessionID (no session exists yet).
_V2G_MESSAGES_WITH_SESSIONID = frozenset([
    "ServiceDiscovery",
    "PaymentServiceSelection",
    "PaymentDetails",
    "Authorization",
    "CertificateInstallation",
    "CertificateUpdate",
    "CableCheck",
    "ChargeParameterDiscovery",
    "PowerDelivery",
    "ChargingStatus",
    "MeteringReceipt",
    "CurrentDemand",
    "PreCharge",
    "WeldingDetection",
    "SessionStop",
    "ServiceDetail",
    "ServiceDiscovery",
])


def needs_vc_sessionid_for_pr(func_name: str) -> bool:
    """
    Determine if a PR function needs to use vc_SessionID.

    Returns True for PR functions that:
      1. Are NOT SessionSetup PRs (those use "0000000000000000" intentionally)
      2. Send a V2G message that includes SessionID

    Examples:
      - f_SECC_CMN_PR_ServiceDiscovery_001 -> True
      - f_SECC_CMN_PR_SessionSetup_001 -> False (uses zero ID intentionally)
      - f_SECC_CMN_PR_InitConfiguration_001 -> False (no V2G message)
      - f_SECC_CMN_PR_SupportedAppProtocol_001 -> False (no SessionID)
    """
    # SessionSetup uses zero ID intentionally
    if "SessionSetup" in func_name:
        return False

    # Check if this PR sends a V2G message with SessionID
    for msg_type in _V2G_MESSAGES_WITH_SESSIONID:
        if msg_type in func_name:
            return True

    return False


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json, sys

    if len(sys.argv) < 2:
        print("Usage: python ttcn_static_renderer.py <plan.json>")
        sys.exit(1)

    plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    print("=== TC module ===")
    print(render_tc_module(plan))
    print("\n=== TB module ===")
    print(render_tb_module(plan))
