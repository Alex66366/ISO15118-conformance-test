#!/usr/bin/env python3
"""
RepairEngine - Generate TTCN-3 code repairs based on fault classification.

Hybrid strategy:
  - auto_fix: Deterministic repairs (port names, timer params, template fields)
    are applied automatically and output fixed code directly.
  - manual_review: Semantic repairs (precondition chain, protocol logic)
    are output as structured suggestions for human confirmation.

Supports self-repair loop: auto_fix suggestions with non-empty `suggested_code`
can be automatically applied back to the source files.

Self-learning: When a fault is resolved but no matching entry exists in the
knowledge base (titan_troubleshooting_db.json), the engine records the error
pattern and fix for future reference.

NOTE: titan_troubleshooting.md is DEPRECATED. All troubleshooting data
is managed in titan_troubleshooting_db.json (structured JSON format).
"""

import json
import re
from dataclasses import dataclass, field, asdict
from datetime import date
from difflib import unified_diff
from pathlib import Path
from typing import Any, Dict, List, Optional


from .fault_classifier import FaultResult

# Default path to the troubleshooting knowledge base.
# NOTE: titan_troubleshooting.md is DEPRECATED and no longer maintained.
# All troubleshooting data now lives in titan_troubleshooting_db.json (structured JSON).
KB_DIR = Path(__file__).parent.parent / "knowledge_base"
TROUBLESHOOTING_DB_PATH = KB_DIR / "titan_troubleshooting_db.json"

# Pipeline-specific aliases -> canonical names (used by build_compile_error_repairs).
# This map is the single source of truth; __init__.py re-exports it as PIPELINE_ALIAS_MAP.
PIPELINE_ALIAS_MAP = {
    "port_undefined": "port_mismatch",
    "timer_error":    "timeout",
    "ttcn_syntax":    "template_field_error",
    "cpp_link":       "import_missing",
}
_ERROR_TYPE_ALIAS = PIPELINE_ALIAS_MAP  # backward-compatible alias

# Canonical fault types: timeout | port_mismatch | template_field_error |
# v2gtp_header_error | runs_on_mismatch | type_mismatch | import_missing
AUTO_FIX_RULES = {
    "timeout": {
        "strategy": "auto_fix",
        "location_type": "timer",
        "suggestion": (
            "Increase timer timeout value in alt block or verify SUT is reachable. "
            "Also check that timer is started before the send operation."
        ),
    },
    "port_mismatch": {
        "strategy": "auto_fix",
        "location_type": "port_mapping",
        "suggestion": (
            "Correct the port name mapping in the testcase. "
            "SECC tests should use pt_V2G_TCP_TLS_ALM_SECC_Port. "
            "EVCC tests should use pt_V2G_TCP_TLS_ALM_EVCC_Port."
        ),
    },
    "template_field_error": {
        "strategy": "auto_fix",
        "location_type": "template",
        "suggestion": (
            "Correct the template field values. "
            "Verify payloadType, messageType, and other fields match ISO 15118-2 spec."
        ),
    },
    "v2gtp_header_error": {
        "strategy": "auto_fix",
        "location_type": "v2gtp_template",
        "suggestion": (
            "Fix V2GTP header template: protocolVersion='01'H, "
            "invProtocolVersion='FE'H, payloadType='8001'H."
        ),
    },
    "runs_on_mismatch": {
        "strategy": "auto_fix",
        "location_type": "function_signature",
        "suggestion": (
            "Fix runs on clause mismatch: ensure all function/altstep declarations "
            "have the same runs on component type as the testcase."
        ),
    },
    "type_mismatch": {
        "strategy": "auto_fix",
        "location_type": "template",
        "suggestion": (
            "Fix template parameter type mismatch: ensure template parameter types "
            "match the values being passed."
        ),
    },
    "import_missing": {
        "strategy": "auto_fix",
        "location_type": "module_import",
        "suggestion": (
            "Add missing import statement at the top of the module. "
            "Check which module defines the missing type/symbol and add "
            "import from <module_name> all;"
        ),
    },
    # --- Pipeline error-type aliases (same canonical types) ---
    "ttcn_syntax": {
        "strategy": "auto_fix",
        "location_type": "template",
        "suggestion": (
            "Check TTCN-3 syntax: matching parentheses, semicolons, keyword spelling. "
            "Refer to TITAN ReferenceGuide Chapter 3."
        ),
    },
}

MANUAL_REVIEW_RULES = {
    "precondition_not_met": {
        "strategy": "manual_review",
        "location_type": "precondition_chain",
        "suggestion": (
            "Review and correct the pre/post condition chain. "
            "Check that InitConfiguration is called before other preconditions, "
            "and that postconditions are called unconditionally after Phase C. "
            "Verify timer values and SUT IP/port configuration."
        ),
    },
    "pcap_only_error": {
        "strategy": "manual_review",
        "location_type": "none",
        "suggestion": (
            "SUT implementation issue detected. "
            "Do NOT modify TTCN-3 test code."
        ),
    },
    # --- Pipeline error-type aliases ---
    "cpp_link": {
        "strategy": "manual_review",
        "location_type": "cpp_adapter",
        "suggestion": (
            "C++ linker error: check that cpp_adapters/ .cc files implement all "
            "required methods and there are no duplicate symbol definitions. "
            "This is a manual review item."
        ),
    },
}


@dataclass
class RepairSuggestion:
    """A single repair suggestion for the TTCN-3 code."""
    repair_type: str          # "auto_fix" or "manual_review"
    fault_type: str
    location: str            # e.g. "testcase", "template", "precondition_function"
    current_code: str        # The problematic code snippet
    suggested_code: str      # The suggested fix (empty for manual_review)
    reasoning: str           # Explanation of why this fix is needed
    confidence: float        # 0.0-1.0 confidence in this fix
    patch_diff: str = ""     # git-style unified diff for auto-application


class RepairEngine:
    """Generate TTCN-3 code repairs based on fault classification results."""

    def generate_repairs(self, fault: FaultResult,
                        ttcn3_code: str,
                        generation_plan: Dict) -> List[RepairSuggestion]:
        """Generate repair suggestions for the failing TTCN-3 test case.

        Args:
            fault: Classified fault from FaultClassifier.
            ttcn3_code: The current TTCN-3 source code.
            generation_plan: The generation plan JSON from Orchestrator.

        Returns:
            List of RepairSuggestion objects.
        """
        suggestions = []

        if fault.fault_type in AUTO_FIX_RULES:
            rule = AUTO_FIX_RULES[fault.fault_type]
            suggestion = self._build_auto_fix(fault, rule, ttcn3_code, generation_plan)
            if suggestion:
                suggestions.append(suggestion)

        elif fault.fault_type in MANUAL_REVIEW_RULES:
            rule = MANUAL_REVIEW_RULES[fault.fault_type]
            suggestion = self._build_manual_review(fault, rule, generation_plan)
            suggestions.append(suggestion)

        return suggestions

    def _build_auto_fix(self, fault: FaultResult,
                        rule: Dict,
                        ttcn3_code: str,
                        generation_plan: Dict) -> RepairSuggestion:
        """Build an auto-fix repair suggestion with concrete suggested_code."""
        location = rule["location_type"]
        sut = generation_plan.get("sut_type", "SECC")
        port_name = f"pt_V2G_TCP_TLS_ALM_{sut}_Port"
        timer_name = generation_plan.get(
            "timer_param", "tc_V2G_EVCC_Msg_Timer"
        )

        current = ""
        suggested = ""
        reasoning = ""

        if fault.fault_type == "timeout":
            current = self._extract_timer_usage(ttcn3_code, timer_name)
            reasoning = (
                f"Timer '{timer_name}' expired. "
                f"Increase timeout value in titan_runtime.cfg or alt block parameter. "
                f"The current timer value may be too short for the SUT response time."
            )
            # suggested_code stays empty for timeout since it requires config change

        elif fault.fault_type == "port_mismatch":
            current = self._extract_port_mapping(ttcn3_code)
            # Try to determine the wrong port from evidence
            evidence_str = str(fault.evidence)
            wrong_port = self._guess_wrong_port(evidence_str, sut)
            suggested = self._fix_port_mapping(current, wrong_port, port_name)
            reasoning = (
                f"Port mismatch: expected {port_name}, but network traffic goes to "
                f"a different port. Correct the port name in map/unmap and send/receive calls."
            )

        elif fault.fault_type == "v2gtp_header_error":
            current = self._extract_v2gtp_header(ttcn3_code)
            suggested = self._fix_v2gtp_header(current)
            reasoning = (
                "V2GTP header must use protocolVersion='01'H, "
                "invProtocolVersion='FE'H, payloadType='8001'H."
            )

        elif fault.fault_type == "runs_on_mismatch":
            current = self._extract_runs_on_clauses(ttcn3_code)
            correct_component = f"{sut}Tester"
            suggested = self._fix_runs_on_clauses(current, correct_component)
            reasoning = (
                f"runs on clause mismatch: ensure all function and altstep declarations "
                f"use runs on {correct_component} consistently."
            )

        elif fault.fault_type == "type_mismatch":
            current = self._extract_template_definitions(ttcn3_code)
            reasoning = (
                "Template type mismatch: check that template parameter types "
                "match the values being passed."
            )

        elif fault.fault_type == "import_missing":
            current = self._extract_module_header(ttcn3_code)
            reasoning = (
                "Missing import statement: add the appropriate import from clause "
                "at the top of the module."
            )

        else:
            current = "(see fault evidence)"
            suggested = ""
            reasoning = rule["suggestion"]

        # Generate unified diff if we have both current and suggested
        patch_diff = ""
        if current and suggested and current != suggested:
            patch_diff = self._make_diff(current, suggested, location)

        return RepairSuggestion(
            repair_type="auto_fix",
            fault_type=fault.fault_type,
            location=location,
            current_code=current,
            suggested_code=suggested,
            reasoning=reasoning,
            confidence=fault.confidence,
            patch_diff=patch_diff,
        )

    def _build_manual_review(self, fault: FaultResult,
                            rule: Dict,
                            generation_plan: Dict) -> RepairSuggestion:
        """Build a manual review suggestion."""
        sut = generation_plan.get("sut_type", "SECC")

        pre_chain = generation_plan.get("precondition_chain", [
            "f_SECC_CMN_PR_InitConfiguration_001",
            "f_SECC_CMN_PR_SupportedAppProtocol_001",
        ])
        post_chain = generation_plan.get("postcondition_chain", [
            "f_SECC_CMN_PO_InitialState_001",
            "f_SECC_CMN_PO_ShutdownConfiguration_001",
        ])

        if fault.fault_type == "precondition_not_met":
            reasoning = (
                f"Precondition chain may be incorrect. "
                f"Expected chain: {', '.join(pre_chain)}. "
                f"Ensure InitConfiguration is called first with correct parameters: "
                f"(out HAL_61851_Listener, in SystemSECC). "
                f"Ensure SupportedAppProtocol returns verdicttype and is stored in preConVerdict."
            )
        elif fault.fault_type == "pcap_only_error":
            reasoning = (
                "Anomaly detected in network traffic (PCAP) but TTCN-3 log is clean. "
                "This indicates a SUT implementation issue, not a test script bug. "
                "Do NOT modify the TTCN-3 test code."
            )
        else:
            reasoning = rule["suggestion"]

        return RepairSuggestion(
            repair_type="manual_review",
            fault_type=fault.fault_type,
            location="precondition_chain" if "precondition" in fault.fault_type else "testcase",
            current_code="(see fault evidence)",
            suggested_code="",
            reasoning=reasoning,
            confidence=fault.confidence,
            patch_diff="",
        )

    def _extract_timer_usage(self, code: str, timer_name: str) -> str:
        """Extract timer-related code snippets."""
        lines = []
        for line in code.split("\n"):
            if timer_name in line or "timeout" in line.lower():
                lines.append(line.strip())
        return "\n".join(lines[:10]) if lines else f"// Timer {timer_name} usage not found"

    def _extract_port_mapping(self, code: str) -> str:
        """Extract port mapping code snippets (map/unmap/send/receive with port names)."""
        lines = []
        for line in code.split("\n"):
            if any(kw in line for kw in ["map(", "unmap(", "_Port", "pt_"]):
                lines.append(line.strip())
        return "\n".join(lines[:15]) if lines else "// Port mapping not found"

    def _fix_port_mapping(self, current: str, wrong_port: str, correct_port: str) -> str:
        """Replace wrong port name with correct port name."""
        if wrong_port and wrong_port != correct_port:
            return current.replace(wrong_port, correct_port)
        return current

    def _guess_wrong_port(self, evidence: str, sut: str) -> str:
        """Guess the wrong port from evidence string."""
        if "EVCC" in evidence and sut == "SECC":
            return "pt_V2G_TCP_TLS_ALM_EVCC_Port"
        if "SECC" in evidence and sut == "EVCC":
            return "pt_V2G_TCP_TLS_ALM_SECC_Port"
        return ""

    def _extract_v2gtp_header(self, code: str) -> str:
        """Extract V2GTP header template usage."""
        lines = []
        in_v2gtp_block = False
        for line in code.split("\n"):
            if "V2GTP" in line or "Header" in line or "md_CMN_V2GTP" in line:
                in_v2gtp_block = True
            if in_v2gtp_block:
                lines.append(line.strip())
                if line.strip().endswith(")") and len(lines) > 3:
                    break
        return "\n".join(lines[:15]) if lines else "// V2GTP header usage not found"

    def _fix_v2gtp_header(self, current: str) -> str:
        """Fix V2GTP header with correct protocol constants."""
        fixed = current
        # Fix protocolVersion if wrong
        fixed = self._replace_hex_value(fixed, "protocolVersion", "'01'H")
        # Fix invProtocolVersion if wrong
        fixed = self._replace_hex_value(fixed, "invProtocolVersion", "'FE'H")
        return fixed

    def _replace_hex_value(self, text: str, field_name: str, correct_value: str) -> str:
        """Replace a hex value in a text if it appears to be wrong."""
        pattern = rf"({field_name}\s*:=\s*)'[0-9A-Fa-f]{{2}}'H"
        return re.sub(pattern, rf"\g<1>{correct_value}", text)

    def _extract_runs_on_clauses(self, code: str) -> str:
        """Extract runs on declarations."""
        lines = []
        for line in code.split("\n"):
            if "runs on" in line:
                lines.append(line.strip())
        return "\n".join(lines[:20]) if lines else "// runs on clauses not found"

    def _fix_runs_on_clauses(self, current: str, correct_component: str) -> str:
        """Replace all runs on references with the correct component."""
        import re
        pattern = r"(runs on\s+)(SECCTester|EVCCTester|Tester|Component)"
        return re.sub(pattern, rf"\g<1>{correct_component}", current)

    def _extract_template_definitions(self, code: str) -> str:
        """Extract template definitions."""
        lines = []
        in_template = False
        for line in code.split("\n"):
            if "template " in line:
                in_template = True
            if in_template:
                lines.append(line.strip())
                if line.strip() == "}" and len(lines) > 5:
                    in_template = False
                    break
        return "\n".join(lines[:30]) if lines else "// Template definitions not found"

    def _extract_module_header(self, code: str) -> str:
        """Extract module header with imports."""
        lines = []
        brace_count = 0
        for line in code.split("\n"):
            lines.append(line)
            if "import from" in line:
                continue
            if "{" in line:
                brace_count += line.count("{")
            if "}" in line:
                brace_count -= line.count("}")
            if brace_count > 0:
                continue
            if brace_count == 0 and lines:
                break
        return "\n".join(lines[:50]) if lines else "// Module header not found"

    def _make_diff(self, old: str, new: str, location: str) -> str:
        """Generate a unified diff string for the patch."""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = unified_diff(
            old_lines, new_lines,
            fromfile=f"a/{location}",
            tofile=f"b/{location}",
            lineterm="",
        )
        return "".join(diff)


def build_compile_error_repairs(
    classified_errors: List[Dict],
    ttcn3_code: str,
    generation_plan: Dict,
) -> List[Dict]:
    """Build repair suggestions from classified compile errors.

    Accepts both raw parse errors (from parse_compile_errors) and
    classified errors (from CompileErrorClassifier.classify).

    Raw parse format:  {"type": ..., "file": ..., "line": ..., "message": ...}
    Classified format: {"error_type": ..., "raw_message": ..., "fix_suggestion": ..., ...}

    Args:
        classified_errors: Output from parse_compile_errors() or CompileErrorClassifier.classify().
        ttcn3_code: Current TTCN-3 source code.
        generation_plan: Generation plan JSON.

    Returns:
        List of repair dicts with repair_type, suggested_code, current_code, etc.
    """
    engine = RepairEngine()

    repairs = []
    for err in classified_errors:
        # Normalize field names: accept both raw parse format and classified format.
        # Raw parse:  type / message / file / line
        # Classified: error_type / raw_message / fix_suggestion / ...
        err_type = (
            err.get("error_type")
            or err.get("type", "unknown")
        )
        # Normalize pipeline error-type names to canonical names (e.g. "port_undefined" -> "port_mismatch").
        canonical_type = _ERROR_TYPE_ALIAS.get(err_type, err_type)
        raw_msg = (
            err.get("raw_message")
            or err.get("message", "")
        )
        fix_sug = (
            err.get("fix_suggestion")
            or err.get("description", "")
        )

        fault = FaultResult(
            fault_type=canonical_type,
            confidence=err.get("confidence", 0.5),
            evidence={
                "raw_message": raw_msg,
                "original_error_type": err_type,  # preserve original name for display/logging
            },
            affected_component=err.get("location", ""),
            suggested_fix_category="auto_fix",
            description=fix_sug,
        )
        suggestions = engine.generate_repairs(fault, ttcn3_code, generation_plan)
        for s in suggestions:
            repairs.append({
                "repair_type": s.repair_type,
                "fault_type": s.fault_type,
                "location": s.location,
                "current_code": s.current_code,
                "suggested_code": s.suggested_code,
                "reasoning": s.reasoning,
                "confidence": s.confidence,
                "patch_diff": s.patch_diff,
            })
    return repairs


# ============================================================
# Self-Learning: Knowledge Base Management
# ============================================================

def load_troubleshooting_db(path: Path = TROUBLESHOOTING_DB_PATH) -> Dict:
    """Load the troubleshooting knowledge base from JSON."""
    if not path.exists():
        return {"_schema_version": "1.0", "entries": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_troubleshooting_db(db: Dict, path: Path = TROUBLESHOOTING_DB_PATH) -> None:
    """Save the troubleshooting knowledge base to JSON."""
    db["_last_updated"] = str(date.today())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def find_matching_entry(error_type: str, raw_message: str, db: Dict) -> Optional[Dict]:
    """Find a matching entry in the knowledge base by error type or pattern match."""
    for entry in db.get("entries", []):
        if entry.get("error_type") == error_type:
            return entry
        for pattern in entry.get("patterns", []):
            try:
                if re.search(pattern, raw_message, re.IGNORECASE):
                    return entry
            except re.error:
                pass
    return None


def record_new_entry(
    error_type: str,
    raw_message: str,
    suggested_fix: str,
    affected_file: str,
    category: str = "ttcn_compile",
    severity: str = "warning",
    db: Dict = None,
    db_path: Path = TROUBLESHOOTING_DB_PATH,
) -> Dict:
    """Record a new error pattern and its fix into the knowledge base.

    Call this when a fault is resolved but no matching entry exists in the DB.

    Args:
        error_type: Category label for the error (e.g. "port_undefined").
        raw_message: The original error message from the compiler.
        suggested_fix: The fix that resolved the error.
        affected_file: Which file the error originated from.
        category: "ttcn_compile", "cpp_link", "ttcn_runtime", etc.
        severity: "critical" or "warning".
        db: Optional pre-loaded DB dict (avoids re-reading from disk).
        db_path: Path to the JSON file.

    Returns:
        The newly created entry dict.
    """
    if db is None:
        db = load_troubleshooting_db(db_path)

    today_str = str(date.today())

    # Check if we already have an entry for this error_type
    existing = next(
        (e for e in db.get("entries", []) if e.get("error_type") == error_type),
        None
    )

    if existing:
        # Update occurrence count and last_seen
        existing["occurrences"] = existing.get("occurrences", 0) + 1
        existing["last_seen"] = today_str
        if suggested_fix and not existing.get("fix_en"):
            existing["fix_en"] = suggested_fix
        save_troubleshooting_db(db, db_path)
        return existing

    # Create new entry
    new_entry: Dict[str, Any] = {
        "id": error_type,
        "error_type": error_type,
        "severity": severity,
        "patterns": [_extract_pattern(raw_message)],
        "category": category,
        "affected_file": affected_file,
        "fix": suggested_fix,
        "fix_en": suggested_fix,
        "occurrences": 1,
        "first_seen": today_str,
        "last_seen": today_str,
        "verified": False,
    }

    db.setdefault("entries", []).append(new_entry)
    save_troubleshooting_db(db, db_path)
    return new_entry


def _extract_pattern(message: str) -> str:
    """Extract a regex-like pattern from an error message.

    Replaces specific values with wildcards to create a reusable pattern.
    """
    # Replace quoted strings
    pattern = re.sub(r"'[^']*'", "'(.+?)'", message)
    # Replace numbers
    pattern = re.sub(r"\b\d+\b", "\\d+", pattern)
    # Truncate if too long
    if len(pattern) > 120:
        pattern = pattern[:120] + "..."
    return pattern


def learn_from_repair(
    repair: Dict,
    raw_message: str,
    db_path: Path = TROUBLESHOOTING_DB_PATH,
) -> bool:
    """Record a successful repair into the knowledge base.

    Call this when a RepairSuggestion is applied and the build succeeds.
    This allows the system to remember the fix for future occurrences.

    Args:
        repair: A repair dict with keys: fault_type, suggested_code, reasoning.
        raw_message: The original error message.
        db_path: Path to the JSON file.

    Returns:
        True if a new entry was added, False if it already existed.
    """
    db = load_troubleshooting_db(db_path)
    error_type = repair.get("fault_type", "unknown")

    existing = find_matching_entry(error_type, raw_message, db)
    if existing:
        # Update occurrence count
        existing["occurrences"] = existing.get("occurrences", 0) + 1
        existing["last_seen"] = str(date.today())
        save_troubleshooting_db(db, db_path)
        return False

    # Create new entry
    record_new_entry(
        error_type=error_type,
        raw_message=raw_message,
        suggested_fix=repair.get("suggested_code", "") or repair.get("reasoning", ""),
        affected_file=repair.get("location", "unknown"),
        db=db,
        db_path=db_path,
    )
    return True


def query_kb(
    error_type: Optional[str] = None,
    raw_message: Optional[str] = None,
    db_path: Path = TROUBLESHOOTING_DB_PATH,
) -> Optional[Dict]:
    """Query the knowledge base for a matching entry.

    Args:
        error_type: Exact error type to search for.
        raw_message: Error message text to match patterns against.
        db_path: Path to the JSON file.

    Returns:
        Matching entry dict, or None if not found.
    """
    db = load_troubleshooting_db(db_path)
    return find_matching_entry(error_type or "", raw_message or "", db)

