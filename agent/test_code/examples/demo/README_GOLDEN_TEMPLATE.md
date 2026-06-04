# ISO 15118-2 V2G Test Suite -- Golden Template

**Version:** 1.0 (Golden Template)
**Purpose:** Minimal working reference implementation for the Multi-Agent Automated Generation Framework
**Standards:** ISO 15118-2 / DIN SPEC 70121

---

## Overview

This `demo/` directory is the **Golden Template** -- a minimal, working reference that demonstrates the complete V2G message exchange pipeline end-to-end. It is used as the blueprint by the Multi-Agent system to generate the remaining 20+ message sequences.

The demo implements a single test case: `TC_SECC_CMN_VTB_V2GTPSessionSetup_001`, which performs the SupportedAppProtocol exchange followed by a SessionSetup exchange with the SECC.

---

## The 4-Step SOP (Pure Static Data-Driven Pipeline)

Every V2G application message exchange follows this mechanical procedure:

| Step | Action | Details |
|------|--------|---------|
| **1** | Lookup JSON | Read `exi_payload_library.json`. Record `wire_hex`, `is_dynamic`, `feature_pattern` anchor bytes, and `dynamic_field_offsets`. |
| **2** | Locate Template | Find `md_CMN_{Msg}Req_001` (send) in `CmnTemplates.ttcn`. For receive, use lenient template `{ msgRes := ? }`. |
| **3** | Send Union | `pt_V2G_TCP_TLS_ALM_SECC_Port.send({ msgType := md_CMN_MsgReq_001 })` -- `StaticExiDecoder` encodes from JSON patterns. |
| **4** | Receive Verdict | `receive({ resType := ? })` -- `StaticExiDecoder` extracts dynamic fields and delivers `V2G_MessageUnion`. Stop timer. `setverdict(pass/fail)`. `return getverdict;` |

**SDP messages ONLY (UDP):** Use raw octetstrings on `pt_V2G_UDP_SDP_Port`.

### NEGATIVE CONSTRAINTS (MUST NOT)

- **MUST NOT** use `int2oct`, `'01'O`, `'FE'O`, `'8001'O` in TTCN-3 for V2GTP header construction
- **MUST NOT** send raw octetstrings to the TCP port for V2G application messages
- **MUST NOT** use openv2g encoder or X-Macro mapping -- `StaticExiDecoder` handles all encoding/decoding via static JSON patterns
- **MUST NOT** hardcode ResponseCode, SessionID, or EVSEID values in receive templates -- the C++ decoder extracts them from wire bytes dynamically

---

## Architecture

```
TTCN-3 Test Case
    |
    | send({ sessionSetupReq := md_CMN_SessionSetupReq_001 })
    v
V2G_TCP_TLS_Port (C++ Port Adapter)
    |
    | outgoing_send(OCTETSTRING) -- TITAN serialized the union
    v
StaticExiDecoder (C++ -- JSON-driven)
    |
    | encode("session_setup_req", session_id)
    v
exi_payload_library.json (pre-encoded wire bytes + feature_patterns)
    |
    | (if is_dynamic) patch SessionID placeholder
    v
wire_bytes (V2GTP header + EXI payload)
    |
    v
TCP Socket -> SECC

--- Receive path (static data-driven) ---

TCP Socket <- SECC
    |
    v
V2G_TCP_TLS_Port.receive_loop() -- raw TCP/TLS frame
    |
    v
extract_v2gtp_frame() -- strip TLS header, parse V2GTP header
    |
    v
StaticExiDecoder.decode_exi() -- anchor-based pattern matching
    |
    | STEP 1: Match feature_pattern anchor bytes from JSON
    | STEP 2: Slice dynamic fields (SessionID, ResponseCode, EVSEID) by offset
    | STEP 3: Map ResponseCode byte to TTCN-3 enum integer
    v
V2G_MessageUnion (SessionSetupRes with extracted fields)
    |
    v
incoming_message() -> TTCN-3 receive()
```

**SECC Port Behavior:** The SECC listens on a dynamic port (e.g., 62897) advertised via SDP. The TCP port adapter:
1. Uses `V2G__UDP__SDP__Port__last_secc_port_` (from SDP) for the initial TCP connect
2. Caches the actual connected port via `getpeername()` for reconnection
3. Adds a 75ms guard delay after SDP discovery before TCP handshake

---

## Directory Structure

```
demo/
├── exi_payload_library.json       # Pre-encoded V2G message wire bytes + metadata
├── build.sh                       # Build script (makefilegen + make)
├── config/
│   └── titan_runtime.cfg          # TITAN runtime configuration
└── src/
    └── ttcn3/
        ├── 1_Data_Definitions/
        │   ├── Types/
        │   │   ├── Types_SECC_15118_2.ttcn   # V2G_MessageUnion + message types
        │   │   └── TimerAndConstants.ttcn     # Timer constants, default values
        │   └── Templates/
        │       └── CmnTemplates.ttcn           # Send/receive templates (md_CMN_*, mdw_SECC_*)
        ├── 2_Architecture/
        │   └── ComponentsAndPorts/
        │       └── ComponentsAndPorts.ttcn     # Port types: V2G_MessageUnion for TCP
        ├── 3_Behavior_Steps/
        │   ├── PreConditions/
        │   │   ├── PreConditions_SECC_15118_2.ttcn
        │   │   ├── PreConditions_InitConfig.ttcn  # f_SECC_CMN_PR_InitConfiguration_001
        │   │   └── f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn
        │   ├── PostConditions/
        │   │   ├── PostConditions_SECC_15118_2.ttcn
        │   │   └── f_SECC_CMN_PO_InitialState_001.ttcn
        │   └── TestBehavior/
        │       └── f_SECC_CMN_TB_VTB_V2GTPSessionSetup_001.ttcn
        └── 4_TestCases/
            └── TestCases_SECC/
                └── TC_SECC_CMN_VTB_V2GTPSessionSetup_001.ttcn

demo/src/cpp_adapters/             # C++ port adapters
    ├── CD/exi_engine/
    │   ├── ExiCodecAdapter.hh
    │   └── ExiCodecAdapter.cc     # Singleton: encode/decode + SessionID patching
    ├── SA/ports/
    │   ├── V2G_TCP_TLS_Port.hh
    │   └── V2G_TCP_TLS_Port.cc   # Integrates ExiCodecAdapter
    └── SA/hardware/
```

---

## Key Components

### StaticExiDecoder (Pure Static, Data-Driven)

**Location:** `src/cpp_adapters/common/StaticExiDecoder.cc`

The `StaticExiDecoder` is a **zero-dependency** C++ EXI decoder that uses pre-computed static patterns from `exi_payload_library.json`. No external libraries.

**Interface:**

```cpp
class StaticExiDecoder {
    bool load_from_json(const std::string& path);
    bool decode(const uint8_t* wire_bytes, size_t wire_len, DecodeResult& result);
    bool decode_exi(const uint8_t* exi_payload, size_t exi_len, DecodeResult& result);
    struct DecodeResult {
        MsgType msg_type;      // e.g., MSG_SESSION_SETUP_RES
        int app_response_code; // SupportedAppProtocolRes: 0=OK, 1=Failed
        int schema_id;         // 1=ISO15118, 2=DINSPEC70121
        int v2g_response_code; // SessionSetupRes: 0=OK, 1=OK_NewSessionEstablished
        std::vector<uint8_t> session_id; // 8-byte session identifier
        std::string evseid;    // EVSE identifier (extracted as raw hex)
    };
};
```

**How it works (receive path):**
1. Parse V2GTP header (version, payload type, payload length)
2. Match EXI bytes against `feature_pattern` anchor bytes from JSON
3. Slice dynamic fields by fixed byte offsets (SessionID, ResponseCode, EVSEID)
4. Map ResponseCode byte to TTCN-3 enum integer via `V2GResponseCodeMap`
5. Construct and return `DecodeResult` with all extracted fields

**Pattern matching:**
- `find_pattern()`: checks for matching anchor bytes at `anchor_offset`
- Falls back to header-based detection for edge cases (e.g., 8-bit vs 10-bit EXI)
- `parse_session_setup_res()`: hardcoded byte offsets for known SECC responses

**Payload library resolution (in order):**
1. `EXI_PAYLOAD_LIBRARY` environment variable (absolute path)
2. Relative path from build directory
3. Absolute fallback path

### SessionID Patching

Four messages carry `SessionID` and must patch the placeholder `0000000000000000`:

| Message Key | `is_dynamic` | Dynamic Fields |
|-------------|-------------|----------------|
| `session_setup_req` | `true` | `SessionID` |
| `session_setup_res` | `true` | `SessionID` |
| `session_stop_req` | `true` | `SessionID` |
| `session_stop_res` | `true` | `SessionID` |

The patching logic searches for 8 consecutive zero bytes in the EXI payload portion (after the 8-byte V2GTP header) and replaces them with the established session ID bytes.

### V2G_MessageUnion

The `V2G_MessageUnion` (defined in `Types_SECC_15118_2.ttcn`) is a TTCN-3 union type that contains all V2G message body variants:

```ttcn3
type union V2G_MessageUnion {
    SupportedAppProtocolReq    supportedAppProtocolReq,
    SupportedAppProtocolRes   supportedAppProtocolRes,
    SessionSetupReq           sessionSetupReq,
    SessionSetupRes           sessionSetupRes,
    // ... all other V2G message types
}
```

When declared as the port message type, TITAN serializes union values to `OCTETSTRING` (per JSON encoding annotations) before calling `outgoing_send()`. The C++ port adapter intercepts this and substitutes the pre-encoded wire bytes.

### V2G_TCP_TLS_Port (C++)

**Location:** `src/cpp_adapters/SA/ports/V2G_TCP_TLS_Port.cc`

The TCP/TLS port adapter:
1. **Send:** Receives serialized union bytes from TITAN via `outgoing_send()`, calls `StaticExiDecoder` to encode, sends over TCP
2. **Receive:** Stores raw bytes, parses V2GTP header, calls `StaticExiDecoder.decode_exi()`, delivers `V2G_MessageUnion` via `incoming_message()`
3. **Port Discovery:** Reads dynamic port from `V2G__UDP__SDP__Port__last_secc_port_` (set by SDP response)
4. **Reconnection:** Caches actual peer port via `getpeername()` for graceful reconnection
5. **Guard Delay:** 75ms pause after SDP discovery before TCP handshake (per ISO 15118-3)

### V2G_UDP_SDP_Port

**Location:** `src/cpp_adapters/SA/ports/V2G_UDP_SDP_Port.cc`

The UDP SDP port handles raw octetstrings only. No EXI encoding. SDP request payload: `'01FE9000000000021000'O` (V2GTP header + SDP body).

---

## Precondition Chain

The demo demonstrates a two-phase precondition chain:

```
Phase A: InitConfiguration
  -> Port mapping (TCP + UDP)
  -> SDP Discovery via UDP multicast
  -> (TCP connection to SECC established)

Phase B: SupportedAppProtocol
  -> Send SupportedAppProtocolReq via structured union
  -> Receive SupportedAppProtocolRes
  -> SessionID initialized to 0000000000000000
```

After Phase B, the SessionSetup exchange (Phase C -- the actual test behavior) can proceed.

---

## How to Build

```bash
cd agent/test_code/examples/demo
bash build.sh
```

The build script:
1. Sets up TITAN environment (detects `ttcn3_runtime` path)
2. Copies C++ adapter files to `build/generated_cpp/`
3. Runs `makefilegen` to generate the Makefile
4. Runs `make` to compile TTCN-3 + link C++ adapters
5. Produces `build/bin/TestExecutable`

---

## Adding a New Message Type

To extend this demo for a new message type (e.g., ServiceDiscovery):

1. **Add pre-encoded payload** to `exi_payload_library.json`:
   ```json
   "service_discovery_req": {
     "wire_hex": "01FE8001...",
     "is_dynamic": false,
     "dynamic_fields": [],
     "json_payload": { ... }
   }
   ```

2. **Add send template** to `CmnTemplates.ttcn`:
   ```ttcn3
   template ServiceDiscoveryReq md_CMN_ServiceDiscoveryReq_001 := {
     sessionId := PX_SECC_CMN_Default_SessionID,
     // ... fields
   }
   ```

3. **Implement precondition** in `f_SECC_CMN_PR_ServiceDiscovery_001.ttcn` following the SOP.
   - For receive, use the lenient template `{ msgRes := ? }` to accept any dynamic field values extracted by `StaticExiDecoder`.

4. **Implement test behavior** in `f_SECC_CMN_TB_VTB_ServiceDiscovery_001.ttcn` following the SOP.
   - Use `{ msgRes := ? }` in the receive alt branch.

5. **Register in aggregator** -- the pipeline handles aggregator import injection.

6. **Run `bash build.sh`** -- the Multi-Agent system handles all of this automatically.

---

## Reference Files

| File | Purpose |
|------|--------|
| `exi_payload_library.json` | Pre-encoded wire bytes for all V2G messages |
| `CmnTemplates.ttcn` | Send/receive templates (`md_CMN_*`, `mdw_SECC_*`) |
| `Types_SECC_15118_2.ttcn` | Type definitions including `V2G_MessageUnion` |
| `ComponentsAndPorts.ttcn` | Port type declarations |
| `V2G_TCP_TLS_Port.cc` | C++ port adapter integrating ExiCodecAdapter |
| `StaticExiDecoder.cc` | Pure static JSON-driven EXI encode/decode, ResponseCode mapping, SessionID patching |
| `f_SECC_CMN_TB_VTB_V2GTPSessionSetup_001.ttcn` | Golden SOP reference (test behavior) |
| `f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn` | Golden SOP reference (precondition) |
| `TC_SECC_CMN_VTB_V2GTPSessionSetup_001.ttcn` | Golden SOP reference (test case) |
| `ttcn3_generation_sop.json` | Mechanical SOP with negative constraints |
| `infrastructure_agent_prompt.txt` | Infrastructure Agent prompt (SOP-locked) |
| `core_agent_prompt.txt` | Core Agent prompt (SOP-locked) |
