"""
protocol_registry.py
====================

Loads protocol descriptors from `protocols/{protocol_id}.json` and exposes
a single API for resolving protocol-specific identifiers.

Memory model (strict lazy loading)
----------------------------------
The registry maintains two memory tiers:

  L1  ProtocolManifest
      - Created at startup for every .json found in protocols/.
      - Contains ONLY the routing metadata needed to decide which protocol
        a TC_Id belongs to: protocol_id, compiled tc_id_pattern regex,
        suts/categories lists, and a file-size/mtime snapshot.
      - <1 KB per protocol.  ALL protocols have one resident from startup.

  L2  Full descriptor dict
      - Loaded from disk the FIRST TIME any operation needs the full
        descriptor content (resolve, format_naming, snapshot, …).
      - Cached indefinitely once loaded.
      - Contains messages[], naming templates, protocol_layers, and
        (future) large spec-text blocks.
      - Only the *active* protocol needs L2 at any moment; multiple
        protocols can accumulate in L2 if the pipeline switches back and
        forth.

Public API
----------

    >>> from protocol_registry import ProtocolRegistry
    >>> reg = ProtocolRegistry(Path("protocols"))
    >>> reg.available_protocols()          # L1 scan only — no full JSON loaded
    ['iso15118_2']
    >>> reg.manifest_size_bytes()         # ~1 KB for 1 protocol
    870
    >>> reg.load("iso15118_2")            # L2 loaded here
    >>> reg.is_loaded("iso15118_2")
    True
    >>> reg.loaded_size_bytes()           # full JSON in memory
    48291
    >>> resolved = reg.resolve("V2GTPSessionSetup", sut="SECC")
    >>> resolved.canonical            # "V2GTPSessionSetup"
    >>> resolved.union_field_req     # "sessionSetupReq"
    >>> resolved.port_name            # "pt_V2G_TCP_TLS_ALM_SECC_Port"
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Pattern, Tuple


# ---------------------------------------------------------------------------
# L1: Protocol Manifest (lightweight routing metadata)
# ---------------------------------------------------------------------------

@dataclass
class ProtocolManifest:
    """Lightweight per-protocol metadata used for routing decisions.

    One is created at registry-construction time for every .json found
    in the protocols directory.  Contains only the fields needed for
    "which protocol does this TC_Id belong to" decisions.

    The full descriptor content (messages[], naming, spec text) lives in
    L2 and is loaded lazily via `ProtocolRegistry._ensure_full()`.
    """
    protocol_id: str
    descriptor_path: Path
    tc_id_pattern: Optional[Pattern[str]]   # compiled from tc_id_standard_pattern
    version: str
    suts: List[str]
    categories: List[str]
    message_count: int
    file_mtime_ns: int
    file_size_bytes: int

    def approx_memory_bytes(self) -> int:
        """Conservative estimate of this manifest's in-memory footprint."""
        n = len(self.protocol_id) + len(self.version)
        n += sum(len(s) for s in self.suts)
        n += sum(len(c) for c in self.categories)
        n += 800  # compiled regex object overhead
        return n


# ---------------------------------------------------------------------------
# Resolved value object
# ---------------------------------------------------------------------------

@dataclass
class ResolvedMessage:
    """The result of resolving a raw message type string to its protocol-
    specific identifiers.  All fields are computed from the descriptor;
    callers should not construct this directly."""

    raw: str
    canonical: str
    protocol_id: str
    sut: str
    category: str
    protocol_layer: str
    transport: str
    port_name: str
    request_type: Optional[str] = None
    response_type: Optional[str] = None
    union_field_req: Optional[str] = None
    union_field_res: Optional[str] = None
    uses_raw_octetstring: bool = False
    raw_request_hex: Optional[str] = None
    raw_request_description: Optional[str] = None
    response_timeout_seconds: Optional[float] = None
    covered_by_precondition: Optional[str] = None
    self_precondition: Optional[str] = None
    default_precondition_chain: List[str] = None
    send_template_name: Optional[str] = None
    receive_template_name: Optional[str] = None
    send_key: Optional[str] = None
    receive_key: Optional[str] = None
    note: Optional[str] = None

    def __post_init__(self):
        if self.default_precondition_chain is None:
            self.default_precondition_chain = []

    def to_plan_dict(self) -> Dict[str, Any]:
        """Dump as a flat dict for inclusion in the generation_plan JSON."""
        return {
            "normalized_message_type": self.canonical,
            "request_message_type": self.request_type,
            "response_message_type": self.response_type,
            "union_field_req": self.union_field_req,
            "union_field_res": self.union_field_res,
            "protocol_layer": self.protocol_layer,
            "port_name": self.port_name,
            "send_template_name": self.send_template_name,
            "receive_template_name": self.receive_template_name,
            "send_key": self.send_key,
            "receive_key": self.receive_key,
            "uses_raw_octetstring": self.uses_raw_octetstring,
            "sdp_send": self.uses_raw_octetstring,
            "sdp_request_hex": self.raw_request_hex,
            "raw_request_hex": self.raw_request_hex,
            "raw_request_description": self.raw_request_description,
            "sdp_response_timeout_seconds": self.response_timeout_seconds,
            "response_timeout_seconds": self.response_timeout_seconds,
            "covered_by_precondition": self.covered_by_precondition is not None,
            "covered_by_precondition_fn": self.covered_by_precondition,
            "self_precondition": self.self_precondition,
            "default_precondition_chain": self.default_precondition_chain,
        }


# ---------------------------------------------------------------------------
# Protocol Registry
# ---------------------------------------------------------------------------

class ProtocolRegistry:
    """Resolves protocol-specific identifiers against a lazy-loaded descriptor store.

    Memory model: see module docstring above.
    """

    def __init__(self, protocols_dir: Path, default_log_fn=None):
        self._protocols_dir = Path(protocols_dir)
        self._log = default_log_fn or (lambda *a, **k: None)

        # L1: lightweight manifests — ALL protocols have one from startup
        self._manifests: Dict[str, ProtocolManifest] = {}
        # L2: full descriptor dict — loaded lazily, cached indefinitely
        self._full: Dict[str, Dict[str, Any]] = {}
        # L2 name index — built lazily when a protocol first becomes active
        self._name_index: Dict[str, Dict[str, Tuple[str, str]]] = {}
        # Fallback heuristic (kept per the P0 decision)
        self._fallback_camel_cache: Dict[str, str] = {}
        self._active: Optional[str] = None

        self._scan_protocols()  # L1 scan only — no full JSON loaded

    # ------------------------------------------------------------------
    # L1: scan (fast, memory-light)
    # ------------------------------------------------------------------

    def _scan_protocols(self) -> None:
        """Scan protocols/ and build one L1 ProtocolManifest per .json file.

        This is the ONLY method that runs at construction time.
        Subdirectories (e.g. .review_queue/) are skipped.
        """
        if not self._protocols_dir.is_dir():
            self._log(f"protocols dir does not exist: {self._protocols_dir}", "WARN")
            return
        for fp in sorted(self._protocols_dir.glob("*.json")):
            if fp.name.startswith("."):
                continue
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except Exception as e:
                self._log(f"[L1 skip] {fp.name}: {e}", "WARN")
                continue
            pid = raw.get("protocol_id")
            if not pid:
                self._log(f"[L1 skip] {fp.name}: missing protocol_id", "WARN")
                continue
            # Compile tc_id pattern for fast routing — this is the ONLY
            # per-protocol regex compiled at startup.
            pattern_str = raw.get("tc_id_standard_pattern")
            compiled: Optional[Pattern[str]] = None
            if pattern_str:
                try:
                    compiled = re.compile(pattern_str)
                except re.error as e:
                    self._log(f"[L1] {pid}: bad tc_id_pattern regex: {e}", "WARN")
            st = fp.stat()
            self._manifests[pid] = ProtocolManifest(
                protocol_id=pid,
                descriptor_path=fp,
                tc_id_pattern=compiled,
                version=raw.get("version", "unknown"),
                suts=raw.get("suts", []),
                categories=raw.get("categories", []),
                message_count=len(raw.get("messages", [])),
                file_mtime_ns=st.st_mtime_ns,
                file_size_bytes=st.st_size,
            )
        self._log(
            f"[L1] Scanned {len(self._manifests)} protocol manifests "
            f"(~{self.manifest_size_bytes():,} bytes)", "INFO"
        )

    # ------------------------------------------------------------------
    # L2: lazy full load
    # ------------------------------------------------------------------

    def _ensure_full(self, protocol_id: str) -> Dict[str, Any]:
        """Load (or return cached) the full descriptor JSON for protocol_id.

        This is the SOLE entry point for .json I/O.  All other methods
        that need full content call this first.
        """
        if protocol_id in self._full:
            return self._full[protocol_id]
        if protocol_id not in self._manifests:
            raise KeyError(f"No manifest for protocol_id={protocol_id!r}")
        fp = self._manifests[protocol_id].descriptor_path
        with open(fp, "r", encoding="utf-8") as f:
            full = json.load(f)
        self._full[protocol_id] = full
        self._log(
            f"[L2] Loaded {protocol_id} "
            f"({self._manifests[protocol_id].message_count} messages, "
            f"{self._manifests[protocol_id].file_size_bytes:,} bytes on disk)", "INFO"
        )
        return full

    def _get_full(self, protocol_id: Optional[str] = None) -> Dict[str, Any]:
        """Load full descriptor for `protocol_id`, or the active one if None."""
        pid = protocol_id or self._active
        if pid is None:
            raise RuntimeError("No active protocol. Call .load(pid) first.")
        return self._ensure_full(pid)

    def _ensure_name_index(self, protocol_id: str) -> Dict[str, Tuple[str, str]]:
        """Build (or return cached) the per-protocol name→(pid, canonical) index.

        Built lazily on first resolve for the active protocol.
        """
        if protocol_id in self._name_index:
            return self._name_index[protocol_id]
        full = self._ensure_full(protocol_id)
        idx: Dict[str, Tuple[str, str]] = {}
        for msg in full.get("messages", []):
            name = msg["name"]
            idx[name] = (protocol_id, name)
            for alias in msg.get("alias_names", []):
                idx[alias] = (protocol_id, name)
            # Wrapper-prefixed forms (e.g. V2GTPSessionSetup) are their own
            # distinct messages and are indexed under their own name so that
            # "V2GTPSessionSetup" resolves to its own descriptor
            # (protocol_layer=V2GTP) rather than SessionSetup's.
        self._name_index[protocol_id] = idx
        return idx

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def is_loaded(self, protocol_id: str) -> bool:
        """True when the full L2 descriptor for protocol_id is resident."""
        return protocol_id in self._full

    def loaded_protocols(self) -> List[str]:
        """Protocol IDs whose L2 descriptors are currently in memory."""
        return sorted(self._full.keys())

    def manifest_size_bytes(self) -> int:
        """Approximate total memory of all L1 manifests."""
        return sum(m.approx_memory_bytes() for m in self._manifests.values())

    def loaded_size_bytes(self) -> int:
        """Approximate total memory of resident L2 descriptors."""
        total = 0
        for full in self._full.values():
            total += len(json.dumps(full, ensure_ascii=False).encode("utf-8"))
        return total

    # ------------------------------------------------------------------
    # Discovery / activation
    # ------------------------------------------------------------------

    def available_protocols(self) -> List[str]:
        """All protocol_ids known at L1 scan time (no L2 I/O)."""
        return sorted(self._manifests.keys())

    def load(self, protocol_id: str) -> None:
        """Set the active protocol. All subsequent `resolve()` calls use it.

        Raises KeyError if protocol_id is not in the L1 manifest.
        """
        if protocol_id not in self._manifests:
            raise KeyError(
                f"protocol_id={protocol_id!r} not in manifest. "
                f"Available: {self.available_protocols()}"
            )
        self._active = protocol_id
        self._log(f"Active protocol: {protocol_id}", "INFO")

    def active_protocol(self) -> Optional[str]:
        return self._active

    def get_descriptor(self, protocol_id: Optional[str] = None) -> Dict[str, Any]:
        """Return the full descriptor (triggers L2 load if not resident)."""
        return self._get_full(protocol_id)

    def detect_protocol(self, tc_id: str) -> Optional[str]:
        """Return the protocol_id whose L1 tc_id_pattern matches this TC_Id.

        Consults L1 manifests only — no L2 I/O occurs here.
        """
        for pid, m in self._manifests.items():
            if m.tc_id_pattern and m.tc_id_pattern.match(tc_id):
                return pid
        return None

    def auto_load_for_tc(self, tc_id: str) -> Optional[str]:
        """Detect the protocol for a TC_Id and activate it. Returns the
        detected protocol_id, or None if no protocol claims it."""
        pid = self.detect_protocol(tc_id)
        if pid is not None:
            self.load(pid)
        return pid

    # ------------------------------------------------------------------
    # Message lookup
    # ------------------------------------------------------------------

    def _lookup_message(self, raw: str) -> Tuple[str, Dict[str, Any]]:
        """Find the canonical message descriptor for a raw input string.

        Lookup order (active protocol's L2 only):
          1. Exact match in the name index.
          2. Case-insensitive match.
          3. Wrapper-prefix stripping.
        """
        if not raw:
            raise KeyError("empty message type")
        pid = self._active
        if pid is None:
            raise RuntimeError("No active protocol.")
        idx = self._ensure_name_index(pid)
        # 1. Exact
        if raw in idx:
            _, canon = idx[raw]
            msg = self._find_message_in(pid, canon)
            if msg is not None:
                return pid, msg
        # 2. Case-insensitive
        raw_lower = raw.lower()
        for k, (p, canon) in idx.items():
            if k.lower() == raw_lower:
                msg = self._find_message_in(p, canon)
                if msg is not None:
                    return p, msg
        # 3. Wrapper-prefix stripping
        full = self._get_full(pid)
        for prefix in full.get("wrapper_prefixes", []):
            if raw.startswith(prefix) and len(raw) > len(prefix):
                tail = raw[len(prefix):]
                if tail and tail[0].isupper():
                    if tail in idx:
                        _, canon = idx[tail]
                        msg = self._find_message_in(pid, canon)
                        if msg is not None:
                            return pid, msg
        raise KeyError(
            f"message_type={raw!r} not found in active protocol {pid!r}. "
            f"Known: {sorted(set(idx.keys()))[:20]}"
        )

    def _find_message_in(self, protocol_id: str, canonical_name: str) -> Optional[Dict[str, Any]]:
        full = self._get_full(protocol_id)
        for m in full.get("messages", []):
            if m["name"] == canonical_name:
                return m
            if m.get("canonical_name") == canonical_name:
                return m
            if canonical_name in m.get("alias_names", []):
                return m
        return None

    def _strip_wrapper(self, raw: str) -> str:
        if not self._active or self._active not in self._full:
            return raw
        for prefix in self._full[self._active].get("wrapper_prefixes", []):
            if raw.startswith(prefix) and len(raw) > len(prefix):
                tail = raw[len(prefix):]
                if tail and tail[0].isupper():
                    return tail
        return raw

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, raw_message_type: str, sut: str = "SECC",
                seq_no: str = "001") -> ResolvedMessage:
        """Resolve a raw message_type string to a ResolvedMessage."""
        if not self._active:
            raise RuntimeError(
                "No active protocol. Call .load(pid) or "
                ".auto_load_for_tc(tc_id) first."
            )
        try:
            pid, msg = self._lookup_message(raw_message_type)
        except KeyError:
            stripped = self._strip_wrapper(raw_message_type)
            if stripped != raw_message_type:
                pid, msg = self._lookup_message(stripped)
            else:
                raise
        return self._build_resolved(msg, pid, raw_message_type, sut, seq_no)

    def _build_resolved(self, msg: Dict[str, Any], protocol_id: str,
                        raw: str, sut: str, seq_no: str) -> ResolvedMessage:
        desc = self._get_full(protocol_id)
        category = msg.get("category", "CMN")
        # Port selection
        port_key = msg.get("port_key")
        if not port_key:
            if msg.get("uses_raw_octetstring") and msg.get("protocol_layer", "").upper() == "SDP":
                port_key = "sdp_udp_multicast"
            else:
                port_key = "main"
        port_tmpl = desc.get("transport", {}).get("ports", {}).get(port_key)
        port_name = port_tmpl.replace("{SUT}", sut) if port_tmpl else f"pt_Unknown_{sut}_Port"
        # Precondition chain
        chain = [fn.replace("{SUT}", sut) for fn in msg.get("default_precondition_chain", [])]
        covered_raw = msg.get("covered_by_precondition")
        covered = covered_raw.replace("{SUT}", sut) if covered_raw else None
        # Self-precondition (the function module this message owns).
        # When set, the Plan Builder appends it to the chain so this case
        # actually generates the .ttcn file for its own precondition — making
        # the asset available for downstream cases to reuse.
        self_pre_raw = msg.get("self_precondition")
        self_pre = self_pre_raw.replace("{SUT}", sut) if self_pre_raw else None
        # Template names — use canonical (stripped) name so templates match
        effective_name = msg.get("canonical_name") or msg["name"]
        send_t = recv_t = None
        if msg.get("request_type") and not msg.get("uses_raw_octetstring"):
            st = desc.get("naming", {}).get("send_template", "md_CMN_{MessageType}Req_{SeqNo}")
            send_t = st.replace("{MessageType}", effective_name).replace("{SeqNo}", seq_no)
            rt = desc.get("naming", {}).get("receive_template", "mdw_{SUT}_{MessageType}Res_{SeqNo}")
            recv_t = rt.replace("{SUT}", sut).replace("{MessageType}", effective_name).replace("{SeqNo}", seq_no)
        # Union fields
        uf_req = msg.get("union_field")
        uf_res = None
        if uf_req:
            if uf_req.endswith("Req"):
                uf_res = uf_req[:-3] + "Res"
            else:
                uf_res = uf_req  # e.g. "v2g" for V2GTP-layer messages
        note = msg.get("note") or msg.get("union_field_note")
        return ResolvedMessage(
            raw=raw, canonical=msg["name"], protocol_id=protocol_id, sut=sut,
            category=category, protocol_layer=msg.get("protocol_layer", "V2G_Application"),
            transport=port_key, port_name=port_name, request_type=msg.get("request_type"),
            response_type=msg.get("response_type"), union_field_req=uf_req, union_field_res=uf_res,
            uses_raw_octetstring=msg.get("uses_raw_octetstring", False),
            raw_request_hex=msg.get("raw_request_hex"),
            raw_request_description=msg.get("raw_request_description"),
            response_timeout_seconds=msg.get("response_timeout_seconds"),
            covered_by_precondition=covered, self_precondition=self_pre,
            default_precondition_chain=chain,
            send_template_name=send_t, receive_template_name=recv_t,
            send_key=uf_req, receive_key=uf_res, note=note,
        )

    # ------------------------------------------------------------------
    # Naming
    # ------------------------------------------------------------------

    def format_naming(self, role: str, **kwargs) -> str:
        """Render a naming template with placeholders substituted."""
        if not self._active:
            raise RuntimeError("No active protocol.")
        desc = self._get_full(self._active)
        tmpl = desc.get("naming", {}).get(role)
        if not tmpl:
            raise KeyError(f"naming role {role!r} not defined in active protocol")
        result = tmpl
        for k, v in kwargs.items():
            result = result.replace("{" + k + "}", str(v))
        return result

    # ------------------------------------------------------------------
    # Bad-case hex
    # ------------------------------------------------------------------

    def get_bad_hex_default(self, tc_id: str, message_type: str) -> Optional[str]:
        """Look up the BadCase hex default for a TC from the active protocol."""
        if not self._active:
            return None
        full = self._get_full(self._active)
        for msg in full.get("messages", []):
            if msg["name"] != message_type:
                continue
            defaults = msg.get("bad_case_hex_defaults", {})
            if tc_id in defaults:
                return defaults[tc_id]
            for sut in full.get("suts", []):
                expanded = tc_id.replace(sut, "{SUT}", 1) if sut in tc_id else None
                if expanded and expanded in defaults:
                    return defaults[expanded]
        return None

    # ------------------------------------------------------------------
    # Snapshot for LLM prompts
    # ------------------------------------------------------------------

    def snapshot(self, protocol_id: Optional[str] = None,
                include_full_messages: bool = False) -> Dict[str, Any]:
        """Return a JSON-serializable snapshot of the active (or specified)
        protocol's descriptor for LLM prompt injection.

        Triggers L2 load if not already resident.
        """
        desc = self._get_full(protocol_id)
        snap: Dict[str, Any] = {
            "protocol_id": desc["protocol_id"],
            "protocol_name": desc.get("protocol_name", desc["protocol_id"]),
            "version": desc.get("version", "unknown"),
            "suts": desc.get("suts", []),
            "categories": desc.get("categories", []),
            "wrapper_prefixes": desc.get("wrapper_prefixes", []),
            "naming": desc.get("naming", {}),
            "transport": desc.get("transport", {}),
            "protocol_layers": desc.get("protocol_layers", []),
            "v2gtp_header_constants": desc.get("v2gtp_header_constants", {}),
            "message_names": [m["name"] for m in desc.get("messages", [])],
            "message_aliases": {
                m["name"]: m.get("alias_names", []) for m in desc.get("messages", [])
            },
        }
        if include_full_messages:
            snap["messages"] = desc.get("messages", [])
        return snap

    # ------------------------------------------------------------------
    # Review queue
    # ------------------------------------------------------------------

    def extend_with_llm_discoveries(
        self,
        llm_proposals: Dict[str, Any],
        proposed_by: str = "unknown_agent",
        proposed_during_tc: str = "",
    ) -> Path:
        """Write an LLM-proposed extension to the .review_queue/ directory.

        The proposal is NOT applied to the active descriptor; a human reviews
        it before merging.  See protocols/.review_queue/README.md.
        """
        if not self._active:
            raise RuntimeError("No active protocol to extend.")
        review_dir = self._protocols_dir / ".review_queue"
        review_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        base = f"{self._active}_{ts}.proposal.json"
        path = review_dir / base
        i = 1
        while path.exists():
            path = review_dir / f"{self._active}_{ts}_{i}.proposal.json"
            i += 1
        envelope = {
            "target_protocol": self._active,
            "proposed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "proposed_by": proposed_by,
            "proposed_during_tc": proposed_during_tc,
            "merge_path": llm_proposals,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(envelope, f, indent=2, ensure_ascii=False)
        self._log(f"Wrote review-queue proposal to {path}", "INFO")
        return path

    # ------------------------------------------------------------------
    # Fallback heuristic (kept per P0 decision)
    # ------------------------------------------------------------------

    def _naive_camel(self, pascal: str) -> str:
        """Naive lowerCamelCase conversion. Last-resort fallback with WARN."""
        if not pascal:
            return pascal
        for suffix in ("ReqType", "ResType", "Type"):
            if pascal.endswith(suffix):
                pascal = pascal[: -len(suffix)]
        parts, cur = [], ""
        for ch in pascal:
            if ch.isupper() and cur:
                parts.append(cur.lower()); cur = ch.lower()
            else:
                cur += ch.lower()
        if cur:
            parts.append(cur)
        return "".join(parts)

    def fallback_union_field(self, raw_message_type: str) -> str:
        """Best-effort union field guess with a warning to add to the JSON."""
        key = raw_message_type.lower().replace("_", "").replace(" ", "")
        if key in self._fallback_camel_cache:
            return self._fallback_camel_cache[key]
        result = self._naive_camel(self._strip_wrapper(raw_message_type)) + "Req"
        self._log(
            f"WARNING: no protocol entry for message_type={raw_message_type!r}; "
            f"using naive-camelCase fallback -> {result!r}. "
            f"Add this message to {self._active}.json to silence this warning.",
            "WARN",
        )
        self._fallback_camel_cache[key] = result
        return result


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_default_registry: Optional[ProtocolRegistry] = None


def get_default_registry(protocols_dir: Optional[Path] = None) -> ProtocolRegistry:
    """Process-wide singleton.  First call sets the directory; later calls
    return the same instance."""
    global _default_registry
    if _default_registry is None:
        if protocols_dir is None:
            protocols_dir = Path(__file__).parent / "protocols"
        _default_registry = ProtocolRegistry(protocols_dir)
    return _default_registry


def reset_default_registry() -> None:
    """Reset the singleton (used in tests to force a fresh registry)."""
    global _default_registry
    _default_registry = None
