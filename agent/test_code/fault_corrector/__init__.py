"""Fault Corrector - Runtime fault analysis module (part of the unified Fault Corrector agent).

Provides dual-track root cause analysis for TTCN-3 V2G test failures by correlating:
- TITAN TestExecution.log (application-layer perspective)
- PCAP network captures (network-layer perspective)

Used by `run_fault_corrector(phase="runtime")` in the main pipeline.

Architecture:
  sniffer.py       -> PCAP capture during test execution
  titan_log_parser -> Parse TITAN log events
  pcap_parser.py   -> Parse V2GTP packets from PCAP
  correlator.py    -> Time-align both tracks
  fault_classifier -> Classify failure type
  repair_engine    -> Generate fixes (auto + manual)
"""

__version__ = "1.0.0"
