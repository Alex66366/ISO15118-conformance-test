#!/usr/bin/env python3
"""
TitanLogParser - Parse TITAN TestExecution.log for V2G test analysis.

TITAN log format (titan_runtime.cfg LogLevel=0):
  - Verdict events: "Verdict: pass/fail/inconc"
  - Function entry:  "Function {name}"
  - Port send:       "Port {port_name}: Send"
  - Port receive:    "Port {port_name}: Receive"
  - Timer start:     "Timer {timer_name}: started"
  - Timer timeout:   "Timer {timer_name}: timeout"
  - Error:           "ERROR:"
  - Testcase start:  "Testcase {tc_name}: "
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class TitanLogEntry:
    """A single parsed log entry from TITAN TestExecution.log."""
    timestamp: str
    component: str
    event_type: str  # EVENT_TIMER_START, EVENT_TIMER_TIMEOUT, EVENT_PORT_SEND, etc.
    details: str
    raw_line: str
    line_number: int


class TitanLogParser:
    """Parser for TITAN TestExecution.log files."""

    # Regex patterns for key log events
    _PAT_VERDICT = re.compile(r"Verdict:\s+(pass|fail|inconc|none)\b", re.IGNORECASE)
    _PAT_FUNCTION = re.compile(r"Function\s+(\w+)", re.IGNORECASE)
    _PAT_PORT_SEND = re.compile(r"Port\s+(\S+):\s*Send\b", re.IGNORECASE)
    _PAT_PORT_RECV = re.compile(r"Port\s+(\S+):\s*Receive\b", re.IGNORECASE)
    _PAT_TIMER_START = re.compile(r"Timer\s+(\w+):\s*started", re.IGNORECASE)
    _PAT_TIMER_TIMEOUT = re.compile(r"Timer\s+(\w+):\s*timeout", re.IGNORECASE)
    _PAT_ERROR = re.compile(r"\bERROR\b", re.IGNORECASE)
    _PAT_TESTCASE = re.compile(r"Testcase\s+(\S+):", re.IGNORECASE)
    _PAT_TIMESTAMP = re.compile(r"^\[?(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]?")

    _EVENT_MAP = {
        "verdict": "EVENT_VERDICT",
        "function": "EVENT_FUNCTION",
        "port_send": "EVENT_PORT_SEND",
        "port_receive": "EVENT_PORT_RECEIVE",
        "timer_start": "EVENT_TIMER_START",
        "timer_timeout": "EVENT_TIMER_TIMEOUT",
        "error": "EVENT_ERROR",
        "testcase": "EVENT_TESTCASE_START",
    }

    def parse_file(self, log_path: str) -> List[TitanLogEntry]:
        """Parse a TITAN TestExecution.log file.

        Args:
            log_path: Path to the log file.

        Returns:
            List of TitanLogEntry objects.
        """
        entries = []
        path = Path(log_path)
        if not path.exists():
            return entries

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, raw_line in enumerate(f, start=1):
                entry = self._parse_line(raw_line, line_no)
                if entry:
                    entries.append(entry)

        return entries

    def _parse_line(self, raw_line: str, line_no: int) -> Optional[TitanLogEntry]:
        """Parse a single log line into a TitanLogEntry."""
        ts_match = self._PAT_TIMESTAMP.search(raw_line)
        timestamp = ts_match.group(1) if ts_match else ""

        # Detect event type (check in order of specificity)
        m = self._PAT_TIMER_TIMEOUT.search(raw_line)
        if m:
            return TitanLogEntry(
                timestamp=timestamp, component="", event_type="EVENT_TIMER_TIMEOUT",
                details=m.group(1), raw_line=raw_line.strip(), line_number=line_no
            )

        m = self._PAT_TIMER_START.search(raw_line)
        if m:
            return TitanLogEntry(
                timestamp=timestamp, component="", event_type="EVENT_TIMER_START",
                details=m.group(1), raw_line=raw_line.strip(), line_number=line_no
            )

        m = self._PAT_PORT_RECV.search(raw_line)
        if m:
            return TitanLogEntry(
                timestamp=timestamp, component="", event_type="EVENT_PORT_RECEIVE",
                details=m.group(1), raw_line=raw_line.strip(), line_number=line_no
            )

        m = self._PAT_PORT_SEND.search(raw_line)
        if m:
            return TitanLogEntry(
                timestamp=timestamp, component="", event_type="EVENT_PORT_SEND",
                details=m.group(1), raw_line=raw_line.strip(), line_number=line_no
            )

        m = self._PAT_VERDICT.search(raw_line)
        if m:
            return TitanLogEntry(
                timestamp=timestamp, component="", event_type="EVENT_VERDICT",
                details=m.group(1).lower(), raw_line=raw_line.strip(), line_number=line_no
            )

        m = self._PAT_FUNCTION.search(raw_line)
        if m:
            return TitanLogEntry(
                timestamp=timestamp, component="", event_type="EVENT_FUNCTION",
                details=m.group(1), raw_line=raw_line.strip(), line_number=line_no
            )

        m = self._PAT_ERROR.search(raw_line)
        if m:
            return TitanLogEntry(
                timestamp=timestamp, component="", event_type="EVENT_ERROR",
                details=raw_line.strip(), raw_line=raw_line.strip(), line_number=line_no
            )

        m = self._PAT_TESTCASE.search(raw_line)
        if m:
            return TitanLogEntry(
                timestamp=timestamp, component="", event_type="EVENT_TESTCASE_START",
                details=m.group(1), raw_line=raw_line.strip(), line_number=line_no
            )

        return None

    def get_event_sequence(self, tc_name: str,
                           entries: Optional[List[TitanLogEntry]] = None,
                           log_path: Optional[str] = None) -> List[TitanLogEntry]:
        """Extract event sequence for a specific test case.

        Args:
            tc_name: Name of the test case (e.g. "TC_SECC_CMN_VTB_...").
            entries: Pre-parsed entries (optional).
            log_path: Path to log file (used if entries not provided).

        Returns:
            List of log entries within the test case scope.
        """
        if entries is None:
            if not log_path:
                return []
            entries = self.parse_file(log_path)

        tc_started = False
        result = []
        for entry in entries:
            if entry.event_type == "EVENT_TESTCASE_START" and tc_name in entry.details:
                tc_started = True
                result = [entry]
            elif tc_started:
                result.append(entry)
                # Heuristic: next testcase start marks end of current
                if (entry.event_type == "EVENT_TESTCASE_START" and
                        entry.details != tc_name):
                    break

        return result

    def find_verdict_events(self, entries: List[TitanLogEntry]) -> List[TitanLogEntry]:
        """Filter verdict events from a sequence."""
        return [e for e in entries if e.event_type == "EVENT_VERDICT"]

    def find_timeout_events(self, entries: List[TitanLogEntry]) -> List[TitanLogEntry]:
        """Filter timer timeout events from a sequence."""
        return [e for e in entries if e.event_type == "EVENT_TIMER_TIMEOUT"]

    def find_port_events(self, entries: List[TitanLogEntry]) -> List[TitanLogEntry]:
        """Filter port send/receive events."""
        return [e for e in entries if e.event_type in ("EVENT_PORT_SEND", "EVENT_PORT_RECEIVE")]

    def get_testcase_names(self, log_path: str) -> List[str]:
        """Extract all test case names from a log file."""
        entries = self.parse_file(log_path)
        names = []
        for e in entries:
            if e.event_type == "EVENT_TESTCASE_START" and e.details not in names:
                names.append(e.details)
        return names
