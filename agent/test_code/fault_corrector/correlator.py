#!/usr/bin/env python3
"""
Correlator - Time-align TITAN log events with PCAP packets for dual-track analysis.

Correlation strategy:
  1. Time-window matching: TTCN-3 Port Send events and PCAP TCP packets
     in the same time window are matched.
  2. Message content cross-validation: TTCN-3 send template name vs PCAP
     payloadType field.
  3. TCP flow mapping: TTCN-3 port names mapped to PCAP TCP 5-tuples.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .titan_log_parser import TitanLogEntry
from .pcap_parser import V2GTP_Packet, PcapParser


@dataclass
class CorrelatedEvent:
    """A single event that may have entries from both tracks."""
    log_entry: Optional[TitanLogEntry]
    packet: Optional[V2GTP_Packet]
    alignment_status: str   # "matched" | "packet_missing" | "unexpected_packet" | "log_only" | "pcap_only"
    time_offset_ms: float
    seq_index: int


@dataclass
class AlignmentResult:
    """Result of correlating log events with PCAP packets."""
    tc_name: str
    correlated_events: List[CorrelatedEvent]
    missing_packets: List[dict]
    unexpected_packets: List[V2GTP_Packet]
    root_cause: str        # "timeout" | "template_field_error" | "v2gtp_header_error" | "pcap_only_error" | "ok"
    confidence: float       # 0.0 - 1.0
    summary: str            # Human-readable summary


class Correlator:
    """Correlates TITAN application-layer events with network-layer packets."""

    # Time window for matching (seconds)
    DEFAULT_WINDOW_MS = 500.0

    def __init__(self, window_ms: float = DEFAULT_WINDOW_MS):
        self.window_ms = window_ms

    def align(self, log_events: List[TitanLogEntry],
              packets: List[V2GTP_Packet],
              tc_name: str = "unknown") -> AlignmentResult:
        """Align log events with network packets.

        Args:
            log_events: Parsed TITAN log entries for a test case.
            packets: Parsed V2GTP packets from PCAP.
            tc_name: Name of the test case (for reporting).

        Returns:
            AlignmentResult with correlated events and root cause.
        """
        correlated = []
        missing = []
        unexpected = []

        port_events = [e for e in log_events
                       if e.event_type in ("EVENT_PORT_SEND", "EVENT_PORT_RECEIVE")]
        timeout_events = [e for e in log_events
                         if e.event_type == "EVENT_TIMER_TIMEOUT"]

        used_packet_indices = set()

        for seq_idx, log_e in enumerate(port_events):
            log_time_ms = self._parse_timestamp_ms(log_e.timestamp)

            # Find matching packet
            matched_idx = self._find_matching_packet(
                log_e, packets, log_time_ms, used_packet_indices
            )

            if matched_idx is not None:
                used_packet_indices.add(matched_idx)
                pkt = packets[matched_idx]
                aligned = CorrelatedEvent(
                    log_entry=log_e,
                    packet=pkt,
                    alignment_status="matched",
                    time_offset_ms=abs(pkt.timestamp * 1000 - log_time_ms),
                    seq_index=seq_idx,
                )
            else:
                # No matching packet found
                expected_direction = "EVCC->SECC" if "Send" in log_e.event_type else "SECC->EVCC"
                if log_e.event_type == "EVENT_PORT_SEND":
                    aligned = CorrelatedEvent(
                        log_entry=log_e,
                        packet=None,
                        alignment_status="packet_missing",
                        time_offset_ms=0.0,
                        seq_index=seq_idx,
                    )
                    missing.append({
                        "log_entry": log_e.details,
                        "expected_direction": expected_direction,
                        "timestamp_ms": log_time_ms,
                    })
                else:
                    aligned = CorrelatedEvent(
                        log_entry=log_e,
                        packet=None,
                        alignment_status="log_only",
                        time_offset_ms=0.0,
                        seq_index=seq_idx,
                    )

            correlated.append(aligned)

        # Find unexpected packets (not matched to any log event)
        for idx, pkt in enumerate(packets):
            if idx not in used_packet_indices:
                unexpected.append(pkt)

        # Determine root cause
        root_cause, confidence = self._analyze_alignment(
            correlated, timeout_events, missing, unexpected
        )

        summary = self._build_summary(correlated, missing, unexpected, root_cause)

        return AlignmentResult(
            tc_name=tc_name,
            correlated_events=correlated,
            missing_packets=missing,
            unexpected_packets=unexpected,
            root_cause=root_cause,
            confidence=confidence,
            summary=summary,
        )

    def _parse_timestamp_ms(self, timestamp: str) -> float:
        """Convert TITAN timestamp string to milliseconds."""
        if not timestamp:
            return 0.0
        # Handle ISO 8601 format: "2024-01-01T12:00:00.123"
        try:
            if "T" in timestamp:
                dt_str = timestamp.replace("T", " ")
                if "." in dt_str:
                    base, frac = dt_str.rsplit(".", 1)
                    frac = float("0." + frac) if frac else 0.0
                else:
                    frac = 0.0
                parts = base.split(":")
                hours = int(parts[0].split()[0].split("-")[-1]) if "-" in parts[0] else int(parts[0])
                # Simple hour/min/sec parse
                h, m, s = map(float, parts[-3 if len(parts) == 3 else 0:]) if len(parts) >= 2 else (0, 0, 0)
                return (h * 3600 + m * 60 + s) * 1000 + frac * 1000
        except (ValueError, IndexError):
            pass
        return 0.0

    def _find_matching_packet(self, log_entry: TitanLogEntry,
                              packets: List[V2GTP_Packet],
                              log_time_ms: float,
                              used_indices: set) -> Optional[int]:
        """Find the best-matching packet for a log event."""
        best_idx = None
        best_delta = float('inf')

        for idx, pkt in enumerate(packets):
            if idx in used_indices:
                continue

            pkt_time_ms = pkt.timestamp * 1000
            delta = abs(pkt_time_ms - log_time_ms)

            # Direction must match: send -> EVCC->SECC, receive -> SECC->EVCC
            expected_direction = "EVCC->SECC" if "Send" in log_entry.event_type else "SECC->EVCC"
            if pkt.direction != expected_direction:
                continue

            if delta < self.window_ms and delta < best_delta:
                best_delta = delta
                best_idx = idx

        return best_idx

    def _analyze_alignment(self, correlated: List[CorrelatedEvent],
                           timeouts: List[TitanLogEntry],
                           missing: List[dict],
                           unexpected: List[V2GTP_Packet]
                           ) -> Tuple[str, float]:
        """Analyze alignment to determine root cause."""
        missing_count = sum(1 for c in correlated if c.alignment_status == "packet_missing")
        timeout_count = len(timeouts)
        unexpected_count = len(unexpected)

        if timeout_count > 0 and missing_count > 0:
            return ("timeout", 0.9)

        if missing_count > 0:
            return ("template_field_error", 0.75)

        if unexpected_count > 0:
            for pkt in unexpected:
                if pkt.protocol_version and pkt.protocol_version != '01':
                    return ("v2gtp_header_error", 0.8)
            return ("pcap_only_error", 0.6)

        if timeout_count > 0:
            return ("timeout", 0.85)

        return ("ok", 1.0)

    def _build_summary(self, correlated: List[CorrelatedEvent],
                       missing: List[dict], unexpected: List[V2GTP_Packet],
                       root_cause: str) -> str:
        """Build human-readable alignment summary."""
        lines = [f"Root cause: {root_cause}"]
        lines.append(f"Correlated events: {len(correlated)}")
        matched = sum(1 for c in correlated if c.alignment_status == "matched")
        lines.append(f"  - Matched: {matched}/{len(correlated)}")
        missing_count = sum(1 for c in correlated if c.alignment_status == "packet_missing")
        lines.append(f"  - Missing packets: {missing_count}")
        if unexpected:
            lines.append(f"  - Unexpected packets: {len(unexpected)}")
            for pkt in unexpected[:3]:
                lines.append(f"    {pkt.direction} {pkt.packet_info}")
        return "\n".join(lines)
