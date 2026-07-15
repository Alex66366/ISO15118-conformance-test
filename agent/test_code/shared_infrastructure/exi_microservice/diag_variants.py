#!/usr/bin/env python3
"""
diag_variants.py
================
Sends multiple candidate JSON formats to the EXI microservice to find the
one that actually works. Reports full Java error for each failure.
"""
import json
import socket
import sys


HOST, PORT = "127.0.0.1", 9000


def call(op: str, namespace: str, payload: str) -> str:
    """Frame: "OP\nNAMESPACE\nPAYLOAD\n" then read full response."""
    msg = f"{op}\n{namespace}\n{payload}\n"
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(60)
    s.connect((HOST, PORT))
    s.sendall(msg.encode())
    chunks = []
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    s.close()
    return b"".join(chunks).decode()


def test(label: str, ns: str, payload: str) -> None:
    print(f"\n=== {label} ===")
    print(f"ns: {ns}")
    print(f"payload: {payload[:200]}{'...' if len(payload) > 200 else ''}")
    try:
        resp = call("ENCODE", ns, payload)
        first = resp.split("\n", 1)[0]
        if first == "OK":
            print("  [OK] encoded")
        else:
            print(f"  [FAIL] {resp.strip()}")
    except Exception as e:
        print(f"  [EXC] {e}")


def main() -> int:
    candidates_app_protocol = [
        # 1. lowercase root + lowercase fields (current smoke test)
        ('{"supportedAppProtocolReq": {"appProtocol": [{"protocolNamespace": "x", "versionNumberMajor": 1, "versionNumberMinor": 0, "schemaID": 1, "priority": 1}]}}', "lowercase keys"),
        # 2. PascalCase root + PascalCase fields (XSD original)
        ('{"SupportedAppProtocolReq": {"AppProtocol": [{"ProtocolNamespace": "x", "VersionNumberMajor": 1, "VersionNumberMinor": 0, "SchemaID": 1, "Priority": 1}]}}', "PascalCase keys"),
        # 3. PascalCase root + lowercase fields (mixed)
        ('{"SupportedAppProtocolReq": {"appProtocol": [{"protocolNamespace": "x", "versionNumberMajor": 1, "versionNumberMinor": 0, "schemaID": 1, "priority": 1}]}}', "PascalCase root, lowercase fields"),
        # 4. lowercase root + PascalCase fields
        ('{"supportedAppProtocolReq": {"AppProtocol": [{"ProtocolNamespace": "x", "VersionNumberMajor": 1, "VersionNumberMinor": 0, "SchemaID": 1, "Priority": 1}]}}', "lowercase root, PascalCase fields"),
        # 5. Just a single string field (minimal)
        ('{"SupportedAppProtocolReq": {"AppProtocol": [{"ProtocolNamespace": "x"}]}}', "PascalCase minimal"),
        # 6. Wrapped in _jaxbContext-style with type
        ('{"supportedAppProtocolReq": {"appProtocol": []}}', "empty appProtocol array"),
        # 7. Try as a SupportedAppProtocolRes (response direction)
        ('{"supportedAppProtocolRes": {"responseCode": "OK_SuccessfulNegotiation", "schemaID": 1}}', "SupportedAppProtocolRes lowercase"),
        ('{"SupportedAppProtocolRes": {"ResponseCode": "OK_SuccessfulNegotiation", "SchemaID": 1}}', "SupportedAppProtocolRes PascalCase"),
    ]

    NS = "urn:iso:15118:2:2013:appProtocol"

    print(f"Target: {HOST}:{PORT}")
    print(f"Will test {len(candidates_app_protocol)} variants for namespace {NS}")

    for payload, label in candidates_app_protocol:
        test(label, NS, payload)

    return 0


if __name__ == "__main__":
    sys.exit(main())