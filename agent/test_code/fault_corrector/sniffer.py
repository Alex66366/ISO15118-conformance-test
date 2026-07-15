#!/usr/bin/env python3
"""
V2G_Sniffer - scapy-based background packet capture for V2G test execution.

Captures TCP/UDP traffic on V2G ports (15118) non-intrusively during test runs.
Does NOT require any modifications to TITAN or SUT code.
Outputs a PCAP file for later analysis by pcap_parser.py.

Usage:
    from fault_corrector.sniffer import V2G_Sniffer

    sniffer = V2G_Sniffer(output_pcap="/tmp/v2g_capture.pcap", tcp_port=15118)
    sniffer.start()
    # ... run TITAN tests ...
    pcap_path = sniffer.stop_and_save()
"""

import threading
from pathlib import Path
from typing import Optional

# `from scapy.all import ...` triggers scapy.arch init which calls
# `socket.socket()` and crashes with PermissionError in sandboxed envs.
# Catch any Exception (ImportError OR PermissionError) so the pipeline
# can still start. Live sniffing won't work in sandbox mode, but offline
# analysis of pre-captured PCAPs still does.
try:
    from scapy.all import sniff, TCP, UDP, IPv6, wrpcap
    SCAPY_AVAILABLE = True
except Exception:
    SCAPY_AVAILABLE = False


class V2G_Sniffer:
    """Background packet sniffer for V2G (ISO 15118) traffic.

    Uses scapy to capture TCP/UDP packets on V2G ports without
    modifying TITAN or SUT code. Runs in a daemon thread.
    """

    def __init__(self, output_pcap: str = "/tmp/v2g_capture.pcap",
                 tcp_port: int = 15118, udp_port: int = 15118,
                 interface: Optional[str] = None):
        """
        Args:
            output_pcap: Path to save captured PCAP file.
            tcp_port: TCP port to capture (default 15118 for V2GTP).
            udp_port: UDP port to capture (default 15118 for SDP).
            interface: Network interface (None = all interfaces).
        """
        if not SCAPY_AVAILABLE:
            raise ImportError(
                "scapy is required for packet capture. Install with: pip install scapy"
            )

        self.output_pcap = output_pcap
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.interface = interface
        self.pkts = []
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False

    def _filter(self, pkt) -> bool:
        """Filter function: accept V2G TCP/UDP traffic or IPv6 SDP multicast."""
        if TCP in pkt:
            return (pkt[TCP].sport == self.tcp_port or
                    pkt[TCP].dport == self.tcp_port)
        if UDP in pkt:
            return (pkt[UDP].sport == self.udp_port or
                    pkt[UDP].dport == self.udp_port)
        if IPv6 in pkt:
            # ff02::/16 is IPv6 link-local multicast (SDP)
            return pkt[IPv6].dst.lower().startswith("ff02::")
        return False

    def start(self) -> None:
        """Start sniffing in a background thread."""
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._sniff_loop, daemon=True)
        self._thread.start()

    def _sniff_loop(self) -> None:
        """Main sniff loop running in background thread."""
        sniff(
            lfilter=self._filter,
            store=False,
            prn=lambda p: self.pkts.append(p),
            stop_filter=lambda _: self._stop_event.is_set(),
            iface=self.interface,
        )

    def stop_and_save(self) -> str:
        """
        Stop sniffing and write captured packets to PCAP file.

        Returns:
            Path to the saved PCAP file.
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        Path(self.output_pcap).parent.mkdir(parents=True, exist_ok=True)
        wrpcap(self.output_pcap, self.pkts)
        return self.output_pcap

    def packet_count(self) -> int:
        """Return the number of packets captured so far."""
        return len(self.pkts)
