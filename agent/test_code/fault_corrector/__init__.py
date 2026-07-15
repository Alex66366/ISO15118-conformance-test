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

Canonical error type taxonomy (single source of truth):
  timeout | port_mismatch | template_field_error | v2gtp_header_error |
  runs_on_mismatch | type_mismatch | import_missing |
  codec_serialization_error | codec_deserialization_error |
  verdict_not_set |
  precondition_not_met | pcap_only_error | ok

Pipeline-specific aliases (resolved by _ERROR_TYPE_ALIAS in repair_engine.py):
  port_undefined -> port_mismatch
  timer_error    -> timeout
  ttcn_syntax    -> template_field_error
  cpp_link       -> import_missing (manual_review)
  template_error -> codec_serialization_error
  verdict_none / none_verdict / missing_setverdict -> verdict_not_set
"""

# Canonical error type taxonomy — single source of truth.
# All modules (repair_engine, fault_classifier, pipeline CompileErrorClassifier)
# must derive their type lists from here.
CANONICAL_ERROR_TYPES = frozenset({
    "timeout", "port_mismatch", "template_field_error", "v2gtp_header_error",
    "runs_on_mismatch", "type_mismatch", "import_missing",
    "codec_serialization_error", "codec_deserialization_error",
    "verdict_not_set",
    "precondition_not_met", "pcap_only_error", "ok",
})

# Re-export path constants from repair_engine
from .repair_engine import KB_DIR, TROUBLESHOOTING_DB_PATH  # noqa: E402
# Re-export alias map (also available as _ERROR_TYPE_ALIAS in repair_engine)
from .repair_engine import PIPELINE_ALIAS_MAP  # noqa: E402

__version__ = "1.0.0"

__all__ = [
    "KB_DIR", "TROUBLESHOOTING_DB_PATH",
    "CANONICAL_ERROR_TYPES", "PIPELINE_ALIAS_MAP",
]