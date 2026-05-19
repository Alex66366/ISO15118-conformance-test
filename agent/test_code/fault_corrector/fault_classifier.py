#!/usr/bin/env python3
"""
FaultClassifier - Classify V2G test failures based on alignment results.

Failure categories:
  timeout             - TTCN-3 timer timeout + no response in PCAP
  port_mismatch       - TTCN-3 send on port A + PCAP packet to different port
  template_field_error - TTCN-3 template + PCAP response payloadType mismatch
  precondition_not_met - TTCN-3 receive failure + PCAP shows SUT error code
  v2gtp_header_error  - TTCN-3 send + PCAP V2GTP header field incorrect
  pcap_only_error     - PCAP anomaly but TTCN-3 log is fine (SUT issue)
  ok                  - No failure detected
"""

from dataclasses import dataclass
from typing import List, Optional

from .correlator import AlignmentResult, CorrelatedEvent
from .titan_log_parser import TitanLogEntry
from .pcap_parser import V2GTP_Packet


@dataclass
class FaultResult:
    """Classification result of a test failure."""
    fault_type: str          # One of the failure categories above
    confidence: float         # 0.0 - 1.0
    evidence: dict            # Evidence from TTCN-3 log and PCAP
    affected_component: str   # "precondition" | "test_behavior" | "port_mapping"
    suggested_fix_category: str  # "auto_fix" | "manual_review"
    description: str          # Human-readable description


class FaultClassifier:
    """Classifies V2G test failures into actionable categories."""

    def classify(self, alignment: AlignmentResult,
                 log_events: List[TitanLogEntry]) -> FaultResult:
        """Classify a failure based on alignment and log data.

        Args:
            alignment: Result from Correlator.align()
            log_events: All log entries for the test case

        Returns:
            FaultResult with classification and evidence
        """
        root = alignment.root_cause
        correlated = alignment.correlated_events
        timeout_events = [e for e in log_events
                         if e.event_type == "EVENT_TIMER_TIMEOUT"]

        # === Check each failure type in order of specificity ===

        # 1. Check for timeout
        fault = self._check_timeout(alignment, timeout_events)
        if fault:
            return fault

        # 2. Check for V2GTP header errors
        fault = self._check_v2gtp_error(alignment)
        if fault:
            return fault

        # 3. Check for port mismatch
        fault = self._check_port_mismatch(alignment)
        if fault:
            return fault

        # 4. Check for missing responses (Correlator root_cause="template_field_error")
        if root == "template_field_error":
            missing_evts = [c for c in correlated if c.alignment_status == "packet_missing"]
            fault = self._check_missing_response(alignment, missing_evts)
            if fault:
                return fault

        # 5. Check for template field errors in matched packets (payloadType mismatch)
        fault = self._check_template_error(alignment)
        if fault:
            return fault

        # 6. Check for precondition failures
        fault = self._check_precondition_failure(alignment, log_events)
        if fault:
            return fault

        # 7. Check for PCAP-only errors (SUT bugs, not test script bugs)
        if alignment.unexpected_packets and not any(
                c.alignment_status == "packet_missing" for c in correlated):
            return self._classify_pcap_only_error(alignment)

        # 8. Also check pcap_only_error from correlator root_cause
        if root == "pcap_only_error":
            return self._classify_pcap_only_error(alignment)

        # 9. No error detected
        return FaultResult(
            fault_type="ok",
            confidence=1.0,
            evidence={},
            affected_component="none",
            suggested_fix_category="none",
            description="No failure detected - test may have passed or "
                        "inconclusive verdict was unrelated to SUT behavior.",
        )

    def _check_timeout(self, alignment: AlignmentResult,
                      timeout_events: List[TitanLogEntry]) -> Optional[FaultResult]:
        """Detect timeout-based failures.

        Called when alignment.root_cause == "timeout".  Handles all three
        timeout sub-cases from the Correlator:
          1. timeout_count > 0 AND missing_count > 0  -> ("timeout", 0.9)
          2. timeout_count > 0 AND missing_count == 0 -> ("timeout", 0.85)
          3. No timeout events but Correlator returned "timeout" (impossible path,
             kept for safety)
        """
        if alignment.root_cause != "timeout":
            return None

        if not timeout_events:
            return FaultResult(
                fault_type="timeout",
                confidence=0.85,
                evidence={
                    "ttcn3_side": "Correlator flagged timeout root cause but no timeout events found in log",
                    "pcap_side": "All packets were matched; timeout may have been set with zero duration",
                    "correlation": "Timeout root cause raised by correlator but no explicit timeout in logs",
                },
                affected_component="test_behavior",
                suggested_fix_category="auto_fix",
                description="Timeout flagged by correlator but no explicit timeout event found. Auto-fix: verify timer configuration and SUT response time.",
            )

        first_timeout = timeout_events[0]
        verdict_events = [e for e in alignment.correlated_events
                         if e.log_entry and e.log_entry.event_type == "EVENT_VERDICT"]

        return FaultResult(
            fault_type="timeout",
            confidence=0.9,
            evidence={
                "ttcn3_side": (
                    f"Timer '{first_timeout.details}' expired. "
                    f"No response received within expected time."
                ),
                "pcap_side": (
                    f"PCAP shows no response packet matching the expected "
                    f"message type within timeout window."
                ),
                "correlation": (
                    "TTCN-3 timer started but no corresponding network "
                    "response was captured in PCAP."
                ),
                "timer_name": first_timeout.details,
            },
            affected_component="test_behavior",
            suggested_fix_category="auto_fix",
            description=(
                f"Timer '{first_timeout.details}' expired without receiving "
                f"expected response. SUT may not have responded or response "
                f"was lost. Auto-fix: increase timeout value or verify SUT is reachable."
            ),
        )

    def _check_v2gtp_error(self, alignment: AlignmentResult) -> Optional[FaultResult]:
        """Detect V2GTP header field errors."""
        if alignment.root_cause != "v2gtp_header_error":
            return None

        for c in alignment.correlated_events:
            if c.packet and c.packet.protocol_version not in ("01", ""):
                return FaultResult(
                    fault_type="v2gtp_header_error",
                    confidence=0.8,
                    evidence={
                        "ttcn3_side": "TTCN-3 sends V2GTP message with expected header",
                        "pcap_side": (
                            f"PCAP V2GTP header has wrong protocolVersion="
                            f"{c.packet.protocol_version} (expected '01') or "
                            f"invVersion={c.packet.inv_version} (expected 'FE')"
                        ),
                        "correlation": "Header field mismatch between TTCN-3 expectation and PCAP capture",
                    },
                    affected_component="test_behavior",
                    suggested_fix_category="auto_fix",
                    description=(
                        f"V2GTP header field error detected. "
                        f"protocolVersion={c.packet.protocol_version}, "
                        f"invVersion={c.packet.inv_version}. "
                        f"Auto-fix: correct V2GTP header template fields."
                    ),
                )

        return None

    def _check_port_mismatch(self, alignment: AlignmentResult) -> Optional[FaultResult]:
        """Detect port mapping/connection errors."""
        # Look for mismatches between expected port and actual packet
        mismatches = []
        for c in alignment.correlated_events:
            if c.packet and c.log_entry:
                port_name = c.log_entry.details
                expected_port = self._extract_port_from_name(port_name)
                actual_port = c.packet.dst_port
                if expected_port and actual_port and expected_port != actual_port:
                    mismatches.append((port_name, expected_port, actual_port))

        if mismatches:
            port_name, exp, act = mismatches[0]
            return FaultResult(
                fault_type="port_mismatch",
                confidence=0.85,
                evidence={
                    "ttcn3_side": f"TTCN-3 port '{port_name}' used for communication",
                    "pcap_side": f"Actual network traffic on port {act}, expected port {exp}",
                    "correlation": f"Port mismatch: TTCN-3 uses '{port_name}' but network traffic goes to port {act}",
                },
                affected_component="port_mapping",
                suggested_fix_category="auto_fix",
                description=(
                    f"Port mismatch detected. TTCN-3 expects port {exp} but network "
                    f"traffic goes to port {act}. Auto-fix: correct port mapping "
                    f"in testcase or test port adapter configuration."
                ),
            )

        return None

    def _extract_port_from_name(self, port_name: str) -> Optional[int]:
        """Heuristic: extract port number from TTCN-3 port name."""
        # e.g., "pt_V2G_TCP_TLS_ALM_SECC_Port" -> 15118
        if "15118" in port_name:
            return 15118
        return None

    def _check_missing_response(self, alignment: AlignmentResult,
                                 missing_evts: List[CorrelatedEvent]) -> Optional[FaultResult]:
        """Detect missing response failures (packet_missing aligned events)."""
        if not missing_evts:
            return None

        first = missing_evts[0]
        log_e = first.log_entry
        direction = "EVCC->SECC" if log_e and "Send" in log_e.event_type else "SECC->EVCC"
        port_name = log_e.details if log_e else "unknown"

        return FaultResult(
            fault_type="template_field_error",
            confidence=0.75,
            evidence={
                "ttcn3_side": (
                    f"TTCN-3 {log_e.event_type if log_e else '?'} on port '{port_name}'. "
                    f"No response captured within time window."
                ),
                "pcap_side": (
                    f"No matching {direction} packet found in PCAP capture."
                ),
                "correlation": (
                    f"Expected {direction} response packet was missing. "
                    f"This may indicate a SUT implementation issue or incorrect template configuration."
                ),
                "missing_details": alignment.missing_packets[0] if alignment.missing_packets else {},
            },
            affected_component="test_behavior",
            suggested_fix_category="auto_fix",
            description=(
                f"Missing response: TTCN-3 expected a {direction} response packet but none "
                f"was captured. Possible causes: SUT did not respond, template field values "
                f"do not match SUT expectations, or incorrect port mapping. "
                f"Auto-fix: verify template field values and SUT reachability."
            ),
        )

    def _check_template_error(self, alignment: AlignmentResult) -> Optional[FaultResult]:
        """Detect template field value errors based on payloadType mismatch."""
        mismatches = []
        for c in alignment.correlated_events:
            if c.packet and c.packet.payload_type:
                # Known valid payload types
                valid_types = {"8001", "8002", "8003", "9000", "9001"}
                if c.packet.payload_type not in valid_types:
                    mismatches.append(c.packet.payload_type)

        if mismatches:
            return FaultResult(
                fault_type="template_field_error",
                confidence=0.75,
                evidence={
                    "ttcn3_side": "TTCN-3 sends message with expected template field values",
                    "pcap_side": f"PCAP shows unexpected payloadType: {mismatches[0]}",
                    "correlation": "Payload type in PCAP does not match expected template",
                },
                affected_component="test_behavior",
                suggested_fix_category="auto_fix",
                description=(
                    f"Template field error: unexpected payloadType "
                    f"'{mismatches[0]}' in PCAP. Auto-fix: correct template "
                    f"field values in TTCN-3 testcase."
                ),
            )

        return None

    def _check_precondition_failure(self, alignment: AlignmentResult,
                                    log_events: List[TitanLogEntry]) -> Optional[FaultResult]:
        """Detect precondition execution failures."""
        verdict_events = [e for e in log_events
                         if e.event_type == "EVENT_VERDICT"]
        func_events = [e for e in log_events
                      if e.event_type == "EVENT_FUNCTION"]

        # Check if any precondition function returned non-pass verdict
        for v in verdict_events:
            if v.details != "pass":
                # Find which function set this verdict
                relevant_funcs = [f for f in func_events
                                 if f.line_number < v.line_number][-3:]
                return FaultResult(
                    fault_type="precondition_not_met",
                    confidence=0.9,
                    evidence={
                        "ttcn3_side": (
                            f"Precondition function returned verdict '{v.details}'. "
                            f"Functions: {[f.details for f in relevant_funcs]}"
                        ),
                        "pcap_side": (
                            "PCAP may show SUT error response or no response "
                            "during precondition phase."
                        ),
                        "correlation": (
                            "Precondition chain did not complete successfully. "
                            "Test behavior should not have been executed."
                        ),
                    },
                    affected_component="precondition",
                    suggested_fix_category="manual_review",
                    description=(
                        f"Precondition failure detected: verdict='{v.details}'. "
                        f"Manual review: check pre/post condition chain correctness, "
                        f"verify SUT state, and review timeout values."
                    ),
                )

        return None

    def _classify_pcap_only_error(self, alignment: AlignmentResult) -> FaultResult:
        """Classify errors visible only in PCAP (SUT implementation bugs)."""
        unexpected = alignment.unexpected_packets[:3]
        pcap_info = "; ".join(f"{p.direction} {p.packet_info}" for p in unexpected)

        return FaultResult(
            fault_type="pcap_only_error",
            confidence=0.7,
            evidence={
                "ttcn3_side": "TTCN-3 log shows no application-layer errors",
                "pcap_side": f"Unexpected packets in PCAP: {pcap_info}",
                "correlation": "Application-layer (TTCN-3) and network-layer (PCAP) diverge",
            },
            affected_component="none",
            suggested_fix_category="none",
            description=(
                "PCAP shows anomalous traffic but TTCN-3 log is clean. "
                "This is likely a SUT implementation issue, not a test script bug. "
                "Do NOT modify the TTCN-3 test code."
            ),
        )
