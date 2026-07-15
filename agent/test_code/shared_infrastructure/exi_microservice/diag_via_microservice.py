#!/usr/bin/env python3
"""
diag_via_microservice.py
========================
Sends DIAG requests to the EXI microservice to obtain Java-side full stack
traces. Use this to figure out WHY encoding returns null.

Protocol frame: "OP\\nNAMESPACE\\nPAYLOAD\\n\\n"   (double \\n at end is the
                                                   frame terminator)
DIAG response body is a JSON object with keys:
    ok        : true (encoded) or absent
    exi_hex   : EXI bytes (hex) on success
    java_class, java_message, stack_trace  : on Java exception
    type, message, introspection_error     : Python-side exception
"""
from __future__ import annotations

import base64
import json
import socket
import sys


HOST, PORT = "127.0.0.1", 9000


def call(op: str, namespace: str, payload: str) -> dict:
    """Send DIAG/ENCODE request and parse response as JSON if possible.

    Wire format (matches smoke_test.sh and exi_microservice.py):
        "{op}\\n{namespace}\\n{payload}\\n\\n"
    The final \n\n is the frame terminator that the server reads.
    """
    msg = f"{op}\n{namespace}\n{payload}\n\n"  # double \n at end
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(120)
    s.connect((HOST, PORT))
    s.sendall(msg.encode())
    chunks = []
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        if chunk.endswith(b"\n\n"):
            break
    s.close()
    raw = b"".join(chunks).decode(errors="replace")
    # Response: "OK\\n<body>\\n\\n"
    parts = raw.split("\n", 2)
    if not parts or parts[0] != "OK":
        return {"transport_error": raw[:500]}
    body = parts[1] if len(parts) > 1 else ""
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"unparsed_body": body[:500]}


def diag(label: str, ns: str, payload: str) -> None:
    print(f"\n{'=' * 72}")
    print(f"=== {label}")
    print(f"ns       : {ns}")
    print(f"payload  : {payload[:240]}{'...' if len(payload) > 240 else ''}")
    res = call("DIAG", ns, payload)
    if "ok" in res:
        print(f"[OK] exi_hex: {(res.get('exi_hex') or '')[:80]}...")
    else:
        print("[FAIL]")
        for k in ("type", "message", "java_class", "java_message", "stack_trace",
                  "introspection_error", "transport_error", "unparsed_body"):
            if k in res:
                v = res[k]
                if k == "stack_trace":
                    print(f"  {k}:")
                    for line in str(v).splitlines():
                        print(f"    {line}")
                else:
                    print(f"  {k}: {v}")


def main() -> int:
    print(f"Target : {HOST}:{PORT}")

    # ------------------------------------------------------------------
    # 0. EXACT smoke_test.sh payloads (highest priority — these are the
    #    ones that *actually* fail in the user's run).
    # ------------------------------------------------------------------
    NS_APP = "urn:iso:15118:2:2013:appProtocol"
    sid_b64 = base64.b64encode(b"\x00" * 8).decode()
    evccid_b64 = base64.b64encode(b"\xAB\xCD\xEF\x01\x23\x45").decode()

    smoke_exact_app_req = json.dumps({
        "supportedAppProtocolReq": {
            "appProtocol": [
                {"protocolNamespace": "urn:iso:15118:2:2013:MsgDef",
                 "versionNumberMajor": 2, "versionNumberMinor": 0,
                 "schemaID": 1, "priority": 1},
                {"protocolNamespace": "urn:din:70121:x",
                 "versionNumberMajor": 1, "versionNumberMinor": 0,
                 "schemaID": 1, "priority": 2},
            ]
        }
    })

    print(f"\nWill try EXACT smoke_test payloads")

    diag("EXACT smoke_test 1: SupportedAppProtocolReq (2 entries, real namespaces)",
         NS_APP, smoke_exact_app_req)
    diag("EXACT smoke_test 4: supportedAppProtocolRes",
         NS_APP, json.dumps({"supportedAppProtocolRes": {
             "responseCode": "OK_SuccessfulNegotiation", "schemaID": 1}}))
    diag("EXACT smoke_test 2: V2G_Message / SessionSetupReq",
         "urn:iso:15118:2:2013:MsgDef",
         json.dumps({"V2G_Message": {
             "header": {"sessionID": sid_b64},
             "body": {"SessionSetupReq": {"evccid": evccid_b64}}}}))
    diag("EXACT smoke_test 3: V2G_Message / ServiceDiscoveryReq",
         "urn:iso:15118:2:2013:MsgDef",
         json.dumps({"V2G_Message": {
             "header": {"sessionID": sid_b64},
             "body": {"ServiceDiscoveryReq": {"serviceScope": "ChargingService"}}}}))

    # ------------------------------------------------------------------
    # 1. appProtocol namespace — 7 spelling variants of SupportedAppProtocolReq/Res
    # ------------------------------------------------------------------
    NS_APP = "urn:iso:15118:2:2013:appProtocol"
    print(f"\nWill try 7 variants for {NS_APP}")

    app_variants = [
        ("lowercase root + lowercase fields (current smoke test)",
         '{"supportedAppProtocolReq": {"appProtocol": [{"protocolNamespace": "x", "versionNumberMajor": 1, "versionNumberMinor": 0, "schemaID": 1, "priority": 1}]}}'),
        ("PascalCase root + PascalCase fields",
         '{"SupportedAppProtocolReq": {"AppProtocol": [{"ProtocolNamespace": "x", "VersionNumberMajor": 1, "VersionNumberMinor": 0, "SchemaID": 1, "Priority": 1}]}}'),
        ("PascalCase root + PascalCase minimal",
         '{"SupportedAppProtocolReq": {"AppProtocol": [{"ProtocolNamespace": "x"}]}}'),
        ("lowercase root + lowercase fields + null second entry",
         '{"supportedAppProtocolReq": {"appProtocol": [{"protocolNamespace": "x", "versionNumberMajor": 1, "versionNumberMinor": 0, "schemaID": 1, "priority": 1}, null]}}'),
        ("empty appProtocol array (should encode empty list)",
         '{"supportedAppProtocolReq": {"appProtocol": []}}'),
        ("SupportedAppProtocolRes lowercase",
         '{"supportedAppProtocolRes": {"responseCode": "OK_SuccessfulNegotiation", "schemaID": 1}}'),
        ("SupportedAppProtocolRes PascalCase",
         '{"SupportedAppProtocolRes": {"ResponseCode": "OK_SuccessfulNegotiation", "SchemaID": 1}}'),
    ]
    for label, payload in app_variants:
        diag(label, NS_APP, payload)

    # ------------------------------------------------------------------
    # 2. MsgDef namespace — V2G_Message / SessionSetupReq (smoke test 2)
    # ------------------------------------------------------------------
    NS_MSG = "urn:iso:15118:2:2013:MsgDef"
    sid_b64 = base64.b64encode(b"\x00" * 8).decode()
    evccid_b64 = base64.b64encode(b"\xAB\xCD\xEF\x01\x23\x45").decode()

    msg_def_variants = [
        ("V2G_Message / SessionSetupReq — MOXy lowercase (smoke test 2)",
         json.dumps({"V2G_Message": {
             "header": {"sessionID": sid_b64},
             "body": {"SessionSetupReq": {"evccid": evccid_b64}}
         }})),
        ("V2G_Message / SessionSetupReq — PascalCase evccid",
         json.dumps({"V2G_Message": {
             "header": {"SessionID": sid_b64},
             "body": {"SessionSetupReq": {"EVCCID": evccid_b64}}
         }})),
        ("V2G_Message / SessionSetupReq — evccid as hex string (not base64)",
         json.dumps({"V2G_Message": {
             "header": {"sessionID": sid_b64},
             "body": {"SessionSetupReq": {"evccid": "ABCDEF012345"}}
         }})),
        ("Bare SessionSetupReq (no V2G_Message wrapper)",
         json.dumps({"SessionSetupReq": {"evccid": evccid_b64}})),
        ("V2G_Message / SessionSetupReq — empty body",
         json.dumps({"V2G_Message": {"header": {"sessionID": sid_b64}, "body": {}}})),
        ("V2G_Message / SessionSetupReq — header only, no body key",
         json.dumps({"V2G_Message": {"header": {"sessionID": sid_b64}}})),
    ]
    print(f"\nWill try {len(msg_def_variants)} variants for {NS_MSG}")
    for label, payload in msg_def_variants:
        diag(label, NS_MSG, payload)

    # ------------------------------------------------------------------
    # 3. DIN 70121 namespace — also tested in some setups
    # ------------------------------------------------------------------
    NS_DIN = "urn:din:70121:x"
    din_variants = [
        ("V2G_Message / SessionSetupReq under DIN namespace",
         json.dumps({"V2G_Message": {
             "header": {"sessionID": sid_b64},
             "body": {"SessionSetupReq": {"evccid": evccid_b64}}
         }})),
    ]
    print(f"\nWill try {len(din_variants)} variants for {NS_DIN}")
    for label, payload in din_variants:
        diag(label, NS_DIN, payload)

    # ------------------------------------------------------------------
    # 4. ServiceDiscoveryReq (smoke test 3) + variants
    # ------------------------------------------------------------------
    sd_variants = [
        ("ServiceDiscoveryReq with serviceScope (smoke test 3 exact)",
         json.dumps({"V2G_Message": {
             "header": {"sessionID": sid_b64},
             "body": {"ServiceDiscoveryReq": {"serviceScope": "ChargingService"}}
         }})),
        ("ServiceDiscoveryReq empty (no serviceScope)",
         json.dumps({"V2G_Message": {
             "header": {"sessionID": sid_b64},
             "body": {"ServiceDiscoveryReq": {}}
         }})),
        ("ServiceDiscoveryReq with serviceCategory as string",
         json.dumps({"V2G_Message": {
             "header": {"sessionID": sid_b64},
             "body": {"ServiceDiscoveryReq": {
                 "serviceScope": "ChargingService",
                 "serviceCategory": "EVCharging"
             }}
         }})),
    ]
    print(f"\nWill try {len(sd_variants)} variants for ServiceDiscoveryReq under {NS_MSG}")
    for label, payload in sd_variants:
        diag(label, NS_MSG, payload)

    # ------------------------------------------------------------------
    # 5. AuthorizationReq — includes nested complex types and timestamp-like fields
    # ------------------------------------------------------------------
    auth_variants = [
        ("AuthorizationReq — minimal",
         json.dumps({"V2G_Message": {
             "header": {"sessionID": sid_b64},
             "body": {"AuthorizationReq": {"genChallenge": base64.b64encode(b"\x01\x02\x03\x04").decode()}}
         }})),
    ]
    print(f"\nWill try {len(auth_variants)} variants for AuthorizationReq under {NS_MSG}")
    for label, payload in auth_variants:
        diag(label, NS_MSG, payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())