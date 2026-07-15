# ISO 15118-2 V2G Test Suite — Sprint 2 Golden Template

**Version:** Sprint 2 — v4 Dumb Terminal
**Purpose:** Minimal working reference for the Multi-Agent Automated Generation Framework
**Standards:** ISO 15118-2 / DIN SPEC 70121 / ISO 15118-20
**Architecture:** Universal EXI Bridge v4 — Python microservice + Siemens EXIficient

---

## Design Philosophy: Dumb Terminal

The C++ adapter knows **absolutely nothing** about:
- V2G message semantics (SessionSetupReq, ServiceDiscoveryReq, etc.)
- JSON field names or XSD element names
- ISO 15118 namespaces
- Session IDs or any application-level state

**The adapter is a pure transparent byte pipe.** TTCN-3 is the sole authority over JSON correctness.

---

## Architecture

```
TTCN-3 Test Behavior (charstring JSON)
    |
    | send(char2oct(json))        [port "out" type = OCTETSTRING]
    v
V2G_TCP_TLS_Port.cc (v4 Dumb Terminal)
    |
    | detect_namespace_from_json() — sniffs JSON key to pick namespace
    v
ENCODE request to localhost:9000
    |
    | microservice returns EXI hex bytes
    v
wire = V2GTP header (8 bytes) + EXI payload
    |
    v
TCP Socket -> SECC
```

```
TCP Socket <- SECC
    |
    v
V2G_TCP_TLS_Port.cc (v4 Dumb Terminal)
    |
    | strip V2GTP header, extract EXI bytes
    v
DECODE request to localhost:9000
    |
    | microservice returns JSON UTF-8 bytes
    v
deliver OCTETSTRING to TTCN-3     [port "in" type = OCTETSTRING]
    |
    v
TTCN-3 receive(?) -> value var octetstring
```

**Port type (ComponentsAndPorts.ttcn):**
```ttcn3
type port V2G_TCP_TLS_Port message {
    out octetstring;   // UTF-8 JSON bytes (LLM-generated)
    in  octetstring;   // UTF-8 JSON bytes (microservice-decoded)
}
```

---

## Sprint 2: The Only Send Pattern

TTCN-3 test code sends by constructing a `charstring` JSON literal and converting it:

```ttcn3
var charstring json_req := "{...well-formed JSON...}";
port.send(char2oct(json_req));
```

The `char2oct()` conversion makes the UTF-8 bytes an `OCTETSTRING`, which matches the port's `out octetstring` type. The C++ adapter receives this directly.

**For receive:**
```ttcn3
port.receive(?) -> value var octetstring resp {
    var charstring json := oct2char(resp);
    log("Decoded: ", json);
}
```

---

## IRONCLAD JSON RULES (Non-Negotiable)

Violating these silently produces 2-byte empty EXI or NullPointerException. See
`generation_patterns.json::_ironclad_json_payload_rules` for the full reference.

### Rule 1: PascalCase Keys (XSD element names, not Java field names)

MOXy uses the **XSD element name** as the JSON key, NOT the Java field name.

```ttcn3
// WRONG (camelCase / lowercase):
"{ \"appProtocol\": [...] }"
"{ \"header\": { \"sessionID\": \"...\" } }"

// CORRECT (PascalCase XSD names):
"{ \"AppProtocol\": [...] }"
"{ \"Header\": { \"SessionID\": \"...\" } }"
```

### Rule 2: hexBinary = Hex String (NOT base64)

SessionID (8 bytes), EVCCID (6 bytes), GenChallenge (16 bytes) must be hex strings.

```ttcn3
// WRONG (base64):
"{ \"EVCCID\": \"q83vASNF\" }"

// CORRECT (hex):
"{ \"EVCCID\": \"ABCDEF012345\" }"
```

### Rule 3: Namespace Whitelist

Only these 8 namespaces are accepted by EXIficient. Any other string causes NullPointerException.

| Namespace | Use for |
|---|---|
| `urn:iso:15118:2:2010:AppProtocol` | supportedAppProtocolReq/Res |
| `urn:iso:15118:2:2013:MsgDef` | ISO 15118-2 V2G_Message |
| `urn:din:70121:2012:MsgDef` | DIN SPEC 70121 V2G_Message |
| `urn:iso:std:iso:15118:-20:CommonMessages` | ISO 15118-20 common |
| `urn:iso:std:iso:15118:-20:AC` | ISO 15118-20 AC |
| `urn:iso:std:iso:15118:-20:DC` | ISO 15118-20 DC |
| `urn:iso:std:iso:15118:-20:WPT` | ISO 15118-20 WPT |
| `urn:iso:std:iso:15118:-20:ACDP` | ISO 15118-20 ACDP |

**`urn:iso:15118:2:2010:MsgDef` does NOT exist.**

### Canonical Correct Examples

```
AppProtocol REQ:  {"supportedAppProtocolReq":{"AppProtocol":[{"ProtocolNamespace":"urn:iso:15118:2:2010:MsgDef","VersionNumberMajor":2,"VersionNumberMinor":0,"SchemaID":1,"Priority":1}]}}
AppProtocol RES:  {"supportedAppProtocolRes":{"ResponseCode":"OK_SuccessfulNegotiation","SchemaID":1}}
V2G_Message SetupReq: {"V2G_Message":{"Header":{"SessionID":"0000000000000000"},"Body":{"SessionSetupReq":{"EVCCID":"020000000001"}}}}
```

---

## Precondition Chain (Demo)

```
Phase A: InitConfiguration
  -> Port mapping (TCP + UDP)
  -> SDP Discovery via UDP multicast
  -> TCP connection to SECC established

Phase B: SupportedAppProtocol (PRECONDITION)
  -> send(char2oct(JSON))
  -> receive(?) -> value var octetstring
  -> setverdict(pass/inconc)

Phase C: SessionSetup (TEST BEHAVIOR)
  -> send(char2oct(JSON with SessionID=0011223344556677))
  -> receive(?) -> value var octetstring
  -> setverdict(pass/fail/inconc)
```

---

## How to Build

```bash
cd agent/test_code/examples/demo

# Terminal 1: Start EXI microservice
python3 /home/dmc/桌面/platform/agent/test_code/shared_infrastructure/exi_microservice/exi_microservice.py &

# Terminal 2: Build
bash build.sh

# Run (requires a running SECC to connect to)
cd build/bin
./TestExecutable ../config/titan_runtime.cfg
```

---

## What's Deleted in v4 (vs v2/v3)

| Component | Status | Reason |
|---|---|---|
| `OpenV2G_Wrapper.hh/.cc` | DELETED | No C EXI encoding |
| `ExiCodecAdapter.hh/.cc` | DELETED | No C++ EXI encoding |
| `StaticExiDecoder.hh/.cc` | DELETED | No static pattern matching |
| `exi_payload_library.json` | DELETED | No pre-encoded wire bytes |
| `V2G_MessageUnion` in port | DELETED | Port now uses OCTETSTRING |
| `switch/case` for 26 message types | DELETED | Adapter is namespace-agnostic |
| `set_parameter('session_id')` injection | REMOVED | SessionID is TT-CN-3 responsibility only |

---

## Directory Structure

```
demo/
├── build.sh                              # TITAN build (V3/V4 compatible)
├── config/
│   └── titan_runtime.cfg
└── src/
    ├── ttcn3/
    │   ├── 1_Data_Definitions/
    │   │   ├── Types/
    │   │   │   └── Types_SECC_15118_2.ttcn    # V2G_MessageUnion (types only, no port binding)
    │   │   └── TimerAndConstants.ttcn
    │   ├── 2_Architecture/
    │   │   └── ComponentsAndPorts/
    │   │       └── ComponentsAndPorts.ttcn      # V2G_TCP_TLS_Port: out/in = OCTETSTRING
    │   ├── 3_Behavior_Steps/
    │   │   ├── PreConditions/
    │   │   │   └── f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn  # char2oct(JSON)
    │   │   └── TestBehavior/
    │   │       └── f_SECC_CMN_TB_VTB_V2GTPSessionSetup_001.ttcn
    │   └── 4_TestCases/
    └── cpp_adapters/SA/ports/
        ├── V2G_TCP_TLS_Port.hh          # v4 Dumb Terminal (pure byte pipe)
        ├── V2G_TCP_TLS_Port.cc          # 420 lines (vs 877 old)
        ├── V2G_UDP_SDP_Port.hh
        └── V2G_UDP_SDP_Port.cc
```

---

## Key Files

| File | Purpose |
|---|---|
| `generation_patterns.json::_ironclad_json_payload_rules` | **MANDATORY READ** before writing any JSON |
| `core_agent_prompt.txt::Ironclad JSON Payload Rules` | LLM must follow these rules |
| `V2G_TCP_TLS_Port.cc` | v4 Dumb Terminal implementation (pure byte pipe) |
| `ComponentsAndPorts.ttcn` | Port types (out/in = OCTETSTRING since Sprint 2) |
| `smoke_test.sh` | Live verification of microservice correctness (5/5 PASS) |
