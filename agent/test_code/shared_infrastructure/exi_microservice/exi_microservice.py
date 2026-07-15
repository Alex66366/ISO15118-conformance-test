#!/usr/bin/env python3
"""
exi_microservice.py
====================
Universal EXI Bridge — TCP Microservice

Provides EXI encode/decode as a local TCP service, bridging C++ TITAN adapters
and the Siemens EXIficient codec (via py4j + EXICodec.jar).

Architecture:
    C++ V2G_TCP_TLS_Port.cc (Dumb Terminal)
           |  send("ENCODE\n<ns>\n<json>\n\n")
           v
    localhost:9000  (this service)
           |  py4j call
           v
    JVM + EXICodec.jar + EXIficient
           |  EXI encode/decode
           v
    EXI bytes  <->  JSON string

Protocol (simple TCP, custom framing):
    Encode request:  "ENCODE\n<namespace>\n<json>\n\n"
    Encode response OK:   "OK\n<exi_hex>\n\n"
    Encode response ERR:  "ERROR\n<error_message>\n\n"

    Decode request:  "DECODE\n<namespace>\n<exi_hex>\n\n"
    Decode response OK:   "OK\n<json>\n\n"
    Decode response ERR:  "ERROR\n<error_message>\n\n"

Startup:
    python3 exi_microservice.py [--port PORT]

    The service auto-starts the JVM lazily on the first request
    (no JVM overhead when the test suite isn't running).

Environment variables:
    EXI_PORT         TCP port (default 9000)
    EXI_LOG_LEVEL    Python logging level: DEBUG|INFO|WARNING|ERROR (default INFO)
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import sys
import threading
import signal
import socket
from typing import Optional

# Local modules
from exi_codec_handler import (
    get_handler,
    EXIEncodingError,
    EXIDecodingError,
    ExiCodecHandler,
)

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("EXI_SERVER")

_log_level = os.environ.get("EXI_LOG_LEVEL", "INFO").upper()
try:
    logging.getLogger().setLevel(getattr(logging, _log_level))
except AttributeError:
    pass


# ── Protocol constants ─────────────────────────────────────────────────────────
_FRAME_TERMINATOR = b"\n\n"
_MAX_FRAME_SIZE = 10 * 1024 * 1024   # 10 MB sanity limit
_BACKLOG = 8


# ── Protocol handling ──────────────────────────────────────────────────────────


def parse_request(data: bytes) -> Optional[tuple[str, str, str]]:
    """
    Parse a request frame into (operation, namespace, payload).

    Format: "<OP>\n<namespace>\n<payload>\n\n"

    Returns None if the frame is incomplete (needs more data).
    Raises ValueError on malformed input.

    >>> parse_request(b"ENCODE\\nurn:iso:15118:2:2013:MsgDef\\n{\"msg\":\"test\"}\\n\\n")
    ('ENCODE', 'urn:iso:15118:2:2013:MsgDef', '{"msg":"test"}')
    >>> parse_request(b"DECODE\\nurn:iso:15118:2:2013:MsgDef\\n8081ff...\\n\\n")
    ('DECODE', 'urn:iso:15118:2:2013:MsgDef', '8081ff...')
    """
    if _FRAME_TERMINATOR not in data:
        return None  # Incomplete

    frame = data.split(_FRAME_TERMINATOR, 1)[0]
    parts = frame.split(b"\n", 2)

    if len(parts) < 3:
        raise ValueError(
            f"Malformed request: expected <OP>\\n<namespace>\\n<payload>, got {len(parts)} parts"
        )

    op = parts[0].decode("utf-8").strip()
    ns = parts[1].decode("utf-8").strip()
    payload = parts[2].decode("utf-8")

    if op not in ("ENCODE", "DECODE", "DECODE_XML", "DIAG"):
        raise ValueError(f"Unknown operation: '{op}' (expected ENCODE, DECODE, DECODE_XML, or DIAG)")

    return op, ns, payload


def build_response_ok(data_hex_or_json: str) -> bytes:
    """Build a success response."""
    return f"OK\n{data_hex_or_json}".encode("utf-8") + _FRAME_TERMINATOR


def build_response_error(message: str) -> bytes:
    """Build an error response."""
    # Sanitize: strip newlines to keep protocol clean
    clean = message.replace("\n", " | ")
    return f"ERROR\n{clean}".encode("utf-8") + _FRAME_TERMINATOR


# ── Client handler ─────────────────────────────────────────────────────────────


def handle_client(sock: socket.socket, addr: tuple, handler: ExiCodecHandler,
                   idle_timeout_s: float = 300.0) -> None:
    """
    Serve a single client connection.
    Each client gets its own thread; the handler is shared (thread-safe).

    idle_timeout_s : seconds before an idle (no-recv) connection is closed.
                     Default 300s (5 minutes). Sprint 3 raised this from 30s
                     to avoid spurious reconnects during long-running phases
                     like ServiceDiscovery (60s) and MeteringReceipt (60s),
                     where the C++ adapter may sit idle between encode/decode
                     calls without sending any TCP bytes. Previously 30s
                     forced a reconnect, adding ~2-3s latency on every
                     subsequent call (see BATCH_ANALYSIS_20260711.md, Sprint 3).
    """
    logger.info(f"[{addr}] Connected")
    client_buf = b""

    try:
        sock.settimeout(idle_timeout_s)  # idle recv() timeout

        while True:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                logger.warning(f"[{addr}] Recv timeout, closing")
                break

            if not chunk:
                # Clean disconnect
                logger.info(f"[{addr}] Disconnected (EOF)")
                break

            client_buf += chunk

            # Process all complete frames in the buffer
            while _FRAME_TERMINATOR in client_buf:
                # Check size limit before processing
                if len(client_buf) > _MAX_FRAME_SIZE:
                    resp = build_response_error(
                        f"Frame too large: {len(client_buf)} bytes (max {_MAX_FRAME_SIZE})"
                    )
                    sock.sendall(resp)
                    logger.error(f"[{addr}] Frame exceeded limit, closing")
                    return

                # Parse the request
                try:
                    request = parse_request(client_buf)
                except ValueError as e:
                    resp = build_response_error(f"Malformed request: {e}")
                    sock.sendall(resp)
                    logger.warning(f"[{addr}] Bad request: {e}")
                    return

                if request is None:
                    # Incomplete frame — need more data
                    break

                # Remove the processed frame from the buffer
                client_buf = client_buf.split(_FRAME_TERMINATOR, 1)[1]

                # Execute the operation
                op, ns, payload = request
                logger.debug(f"[{addr}] {op} ns={ns} payload_len={len(payload)}")

                try:
                    if op == "ENCODE":
                        exi_bytes = handler.encode(payload, ns)
                        exi_hex = exi_bytes.hex()
                        logger.info(f"[{addr}] ENCODE OK: {len(exi_bytes)} bytes")
                        resp = build_response_ok(exi_hex)

                    elif op == "DIAG":
                        # Diagnostic: return Java-side exception details.
                        out, err = handler.try_encode_raw(payload, ns)
                        if err is None:
                            body = json.dumps({
                                "ok": True,
                                "exi_hex": bytes(out).hex() if out else None,
                            })
                        else:
                            body = json.dumps(err, ensure_ascii=False)
                        resp = build_response_ok(body)

                    else:  # DECODE
                        exi_bytes = bytes.fromhex(payload)
                        json_str = handler.decode(exi_bytes, ns)
                        logger.info(f"[{addr}] DECODE OK: {len(json_str)} chars")
                        resp = build_response_ok(json_str)

                    # NOTE: DECODE_XML diagnostic has been retired.
                    # The upstream ExificientEXICodec from iso15118-master does
                    # not expose a decode_stream() method, so the old
                    # decode_to_xml() shim has been removed. Use DECODE for
                    # the JSON output (which is what callers really need).

                    sock.sendall(resp)

                except EXIEncodingError as e:
                    logger.error(f"[{addr}] ENCODE ERROR: {e}")
                    sock.sendall(build_response_error(f"ENCODE ERROR: {e}"))

                except EXIDecodingError as e:
                    logger.error(f"[{addr}] DECODE ERROR: {e}")
                    sock.sendall(build_response_error(f"DECODE ERROR: {e}"))

    except ConnectionResetError:
        logger.info(f"[{addr}] Connection reset by peer")

    except OSError as e:
        if e.errno == errno.EPIPE:
            logger.info(f"[{addr}] Broken pipe (client closed)")
        else:
            logger.error(f"[{addr}] OSError: {e}")

    finally:
        try:
            sock.close()
        except Exception:
            pass
        logger.info(f"[{addr}] Connection closed")


# ── Server ─────────────────────────────────────────────────────────────────────


def run_server(port: int = 9000, idle_timeout_s: float = 300.0) -> None:
    """Start the TCP server, accepting connections on the given port."""
    handler = get_handler()   # Lazy-initializes JVM on first use

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        srv.bind(("127.0.0.1", port))
    except OSError as e:
        logger.error(f"Failed to bind to port {port}: {e}")
        logger.error(
            f"If port {port} is in use, set EXI_PORT env var to a different port, e.g.:"
        )
        logger.error(f"  EXI_PORT=$((port + 1)) python3 exi_microservice.py")
        sys.exit(1)

    srv.listen(_BACKLOG)
    logger.info(f"[SERVER] Universal EXI Bridge listening on 127.0.0.1:{port}")
    logger.info("[SERVER] Protocol: ENCODE/DECODE on TCP, \\n\\n-framed")
    logger.info("[SERVER] JVM starts lazily on first request (py4j + EXICodec.jar)")

    # Handle shutdown signals gracefully
    def shutdown_handler(signum, frame):
        logger.info(f"[SERVER] Received signal {signum}, shutting down...")
        srv.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    while True:
        try:
            conn, addr = srv.accept()
        except OSError as e:
            if srv._closed:
                break   # Shutdown
            raise

        # One thread per client; py4j gateway is thread-safe
        t = threading.Thread(
            target=handle_client,
            args=(conn, addr, handler, idle_timeout_s),
            daemon=True,
        )
        t.start()


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Universal EXI Bridge — TCP microservice for ISO 15118 EXI codec"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EXI_PORT", "9000")),
        help="TCP port to listen on (default: 9000, or $EXI_PORT)",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=float(os.environ.get("EXI_IDLE_TIMEOUT", "300")),
        help=(
            "Per-connection idle recv() timeout in seconds (default: 300s = 5min, "
            "or $EXI_IDLE_TIMEOUT). After this many seconds with no bytes from the "
            "client, the connection is closed. Sprint 3 raised this from 30s to "
            "avoid spurious reconnects during long-running V2G phases."
        ),
    )
    args = parser.parse_args()

    logger.info(f"[SERVER] Starting Universal EXI Bridge...")
    logger.info(f"[SERVER] Python: {sys.version.split()[0]}")
    logger.info(f"[SERVER] Listening on 127.0.0.1:{args.port}, idle_timeout={args.idle_timeout}s")
    try:
        import py4j
        logger.info(f"[SERVER] py4j: {py4j.__version__}")
    except ImportError:
        logger.error("[SERVER] py4j not installed. Run: pip install py4j")
        sys.exit(1)

    run_server(port=args.port, idle_timeout_s=args.idle_timeout)


if __name__ == "__main__":
    main()
