# Protocol Descriptors

This directory contains **protocol descriptor JSON files** that capture
everything the multi-agent pipeline needs to know about a particular
protocol — naming rules, message catalog, transport, protocol layers,
union-field mappings, and state-machine coverage.

## Why this exists

Before this directory, the same protocol-specific knowledge was hardcoded
in three places:

| Location | What it held |
|---|---|
| `ttcn3_generation_pipeline.py` | `_WRAPPER_PROTOCOL_PREFIXES`, `_STATE_MACHINE_COVERED`, hardcoded SDP hex/timeout, `if message_type == "SDP"` branches, naming-template string formats |
| `ttcn_static_renderer.py` | `_V2G_FIELD_MAP` (22 entries), `_V2GTPHEADER_BAD_HEX_DEFAULTS` (3 entries), BadCase fallback hex |
| `knowledge_base/ttcn3_generation_sop.json` | `naming_rules`, `protocol_constants` |

To add a new protocol you had to touch all three. To remove one protocol
you had to remember every reference. To audit "does this generator know
about protocol X?" you had to grep three files.

This directory replaces all of that with a single, declarative
`{protocol_id}.json` per protocol. Adding a new protocol is a 1-file
change.

## Directory layout

```
protocols/
├── README.md                  # this file
├── iso15118_2.json            # ISO 15118-2 (V2G application layer)
├── iso15118_20.json           # (future) ISO 15118-20
├── ocpp2_0_1.json             # (future) OCPP 2.0.1
└── .review_queue/             # LLM-proposed additions awaiting human review
    ├── README.md              # explains the review workflow
    └── .gitkeep
```

## Schema

Each `*.json` has the following top-level fields.

### Required

| Field | Type | Purpose |
|---|---|---|
| `protocol_id` | string | Unique identifier (lowercase, no spaces). Used as filename. |
| `version` | string | Spec version (e.g. `"2014"`). |
| `suts` | string[] | System-under-test roles (e.g. `["SECC", "EVCC"]`). |
| `categories` | string[] | Test categories (e.g. `["CMN", "AC", "DC"]`). |
| `naming` | object | Naming-pattern templates (see below). |
| `messages` | object[] | Catalog of every message this protocol defines. |

### Optional but recommended

| Field | Type | Purpose |
|---|---|---|
| `protocol_name` | string | Human-readable name (for LLM prompts and logs). |
| `spec_documents` | object[] | References to the spec PDFs/sections the LLM should consult. |
| `wrapper_prefixes` | string[] | Prefixes that, when stripped from a message name, yield the canonical name (e.g. `["V2GTP"]` so `V2GTPSessionSetup` → `SessionSetup`). |
| `tc_id_standard_pattern` | string (regex) | Regex the TC_Id must match to be considered "standard form" for this protocol. |
| `transport` | object | Transport-layer configuration (TCP/TLS, UDP multicast, etc.). |
| `protocol_layers` | object[] | Sub-protocol layers (V2G_Application, V2GTP, SDP, …). Each has a payload type hex. |
| `v2gtp_header_constants` | object | Constants for any V2GTP-style header (protocol version, inverse version). |
| `source` | object | Provenance: where this descriptor was extracted from (filenames, dates, notes). Used for traceability during migration. |

### `naming` object

| Key | Pattern (using `{...}` placeholders) | Example |
|---|---|---|
| `module` | `TC_{SUT}_{Category}_VTB_{MessageType}_{SeqNo}` | `TC_SECC_CMN_VTB_SessionSetup_001` |
| `testcase` | (same as module) | |
| `test_behavior_function` | `f_{SUT}_{Category}_TB_VTB_{MessageType}_{SeqNo}` | `f_SECC_CMN_TB_VTB_SessionSetup_001` |
| `precondition_function` | `f_{SUT}_{Category}_PR_{Name}_{SeqNo}` | `f_SECC_CMN_PR_InitConfiguration_001` |
| `postcondition_function` | `f_{SUT}_{Category}_PO_{Name}_{SeqNo}` | `f_SECC_CMN_PO_InitialState_001` |
| `send_template` | `md_CMN_{MessageType}Req_{SeqNo}` | `md_CMN_SessionSetupReq_001` |
| `receive_template` | `mdw_{SUT}_{MessageType}Res_{SeqNo}` | `mdw_SECC_SessionSetupRes_001` |
| `timer` | `tc_V2G_{SUT}_Msg_Timer` | `tc_V2G_SECC_Msg_Timer` |
| `timer_param` | `par_V2G_{SUT}_Msg_Timeout_{MessageType}{Req\|Res}` | `par_V2G_EVCC_Msg_Timeout_SessionSetupReq` |
| `altstep` | `a_{SUT}_{Name}` | `a_SECC_ReceptionTimeout` |
| `component_var` | `vc_{Name}` | `vc_HAL61851` |

### `messages[]` object

| Field | Type | Required | Purpose |
|---|---|---|---|
| `name` | string | ✓ | Canonical message name (e.g. `SessionSetup`). The TC_Id `_VTB_` segment is matched against this (and its aliases) case-insensitively. |
| `category` | string | ✓ | `"CMN"` / `"AC"` / `"DC"` (whatever this protocol uses). |
| `protocol_layer` | string | ✓ | One of `protocol_layers[].id`. |
| `transport` | string | ✓ | One of `transport.ports` keys (e.g. `"main"`, `"sdp_udp_multicast"`). |
| `uses_raw_octetstring` | bool | for SDP-like msgs | `true` if the message is sent as raw octetstring (e.g. SDP, raw V2GTP headers for BadCases), not as a TTCN-3 union value. |
| `raw_request_hex` | string | when `uses_raw_octetstring` | The default hex payload for the request (e.g. SDP `"01FE9000000000021000"`). |
| `raw_request_description` | string | optional | Human-readable description of what the hex means. Injected into LLM prompts. |
| `response_timeout_seconds` | float | when `uses_raw_octetstring` | How long to wait for a response (e.g. `5.0` for SDP). |
| `covered_by_precondition` | string \| null | ✓ | If non-null, the pre-condition function that **already** exchanges this message (so the TB can short-circuit to `setverdict(pass)`). Use `{SUT}` placeholder. |
| `request_type` | string \| null | when not raw | PascalCase request type name (e.g. `SessionSetupReq`). Must exist in the protocol's union type. |
| `response_type` | string \| null | when not raw | PascalCase response type name (e.g. `SessionSetupRes`). |
| `union_field` | string \| null | when not raw | The TTCN-3 union field name for this message's request side (e.g. `sessionSetupReq`). Must be the camelCase field defined in the protocol's union type. |
| `wrapper_prefix` | string | optional | If set, this message's name carries a wrapper prefix that must be stripped before resolution. The canonical name is `wrapper_prefix` removed. |
| `canonical_name` | string | when wrapper_prefixed | The bare message name after wrapper stripping (e.g. `SessionSetup` for `V2GTPSessionSetup`). |
| `alias_names` | string[] | optional | Alternative names that should resolve to this same message (e.g. `MeteringReceipt` is an alias for `MeterReceipt`). |
| `default_precondition_chain` | string[] | ✓ | The pre-condition functions to invoke before this message's TB. Use `{SUT}` placeholder. |
| `bad_case_hex_defaults` | object | optional | Map of `TC_Id` → `bad_hex_value` for BadCases that need specific malformed bytes (V2GTPHeader, etc.). |
| `union_field_note` | string | optional | Free-text annotation explaining a non-obvious `union_field` (e.g. why `v2g` is used for V2GTP-layer). |
| `note` | string | optional | Free-text annotation, injected into LLM prompts when this message is referenced. |

## Cross-cutting design decisions

### Wrapper-prefixed messages are first-class

`V2GTPSessionSetup` is a real protocol message in `iso15118_2.json`, with
its own full metadata, rather than a special case in the helper code. The
only concession is the `wrapper_prefix` + `canonical_name` pair that
tells the registry to strip the prefix before looking up templates.

### Aliases are explicit

`MeteringReceipt` → `MeterReceipt` and `PaymentServiceSelection` →
`ServicePaymentSelection` are declared in `alias_names[]`. The registry
resolves both identifiers to the same canonical message, so old and new
test cases both work.

### SDP has its own slot

SDP is structurally different (UDP multicast, raw octetstring, no union
type). Rather than scattering `if message_type == "SDP"` branches
through the code, SDP is a normal entry in `messages[]` with
`uses_raw_octetstring: true`. The renderer reads those flags.

### `source` provides traceability

Each descriptor records which hardcoded locations it was extracted from.
This makes the migration auditable and gives the team confidence to
delete the old hardcoded values once descriptors are stable.

## Review queue workflow

When the LLM proposes a new message mapping it didn't see in the
descriptor, it writes a JSON file to `protocols/.review_queue/` rather
than mutating the main descriptor. A human then reviews the proposal and
either:
1. Merges it into the main descriptor (move + delete the queue file), or
2. Rejects it (delete the queue file with reason in commit message).

See `protocols/.review_queue/README.md` for the detailed workflow.

## Adding a new protocol

1. Copy `protocols/iso15118_2.json` to `protocols/{your_protocol_id}.json`.
2. Update `protocol_id`, `version`, `protocol_name`.
3. Adjust `suts`, `categories`, `naming`, `transport` to match your protocol.
4. Replace the `messages[]` array with your protocol's messages.
5. (Optional) Set `source` to record where you got the data from.
6. Run the validation script in `tests/test_protocol_registry.py` (P5 deliverable).

**No Python code change required.** The pipeline will pick up the new
descriptor automatically once `ProtocolRegistry` is in place (P1).
