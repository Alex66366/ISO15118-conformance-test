#!/usr/bin/env python3
"""
PcapParser - Parse V2GTP packets from PCAP files captured by sniffer.py.

V2GTP message structure (per ISO 15118-2):
  - Layer 2: Ethernet (src/dst MAC)
  - Layer 3: IPv6 (SDP multicast) or IPv4 (TCP)
  - Layer 4: UDP (15118, SDP) or TCP (dynamic, V2GTP)
  - V2GTP Header (8 bytes):
      protocolVersion (1 byte)  = '01'H
      invProtocolVersion (1 byte) = 'FE'H
      payloadType (2 bytes)    = '8001'H (V2G message)
      payloadLength (4 bytes)
  - Payload: EXI-encoded V2G message
"""

from dataclasses import dataclass
from typing import List, Optional

try:
    from scapy.all import rdpcap, TCP, UDP, IPv6, Raw
    SCAPY_AVAILABLE = True
except ImportError:
    SCAPY_AVAILABLE = False


@dataclass
class V2GTP_Packet:
    """A single parsed V2GTP packet."""
    timestamp: float
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    protocol_version: str      # e.g. '01'
    inv_version: str           # e.g. 'FE'
    payload_type: str          # e.g. '8001'
    payload_length: int
    raw_bytes: bytes
    direction: str             # "EVCC->SECC" or "SECC->EVCC"
    packet_info: str            # Summary string


class PcapParser:
    """Parser for V2GTP packets from PCAP files."""

    def __init__(self, evcc_ip: str = "192.168.100.10",
                 secc_ip: str = "192.168.100.11"):
        """
        Args:
            evcc_ip: EVCC IP address (used to determine direction).
            secc_ip: SECC IP address (used to determine direction).
        """
        self.evcc_ip = evcc_ip
        self.secc_ip = secc_ip

    def parse_file(self, pcap_path: str) -> List[V2GTP_Packet]:
        """Parse a PCAP file and extract V2GTP packets.

        Args:
            pcap_path: Path to the PCAP file.

        Returns:
            List of V2GTP_Packet objects.
        """
        if not SCAPY_AVAILABLE:
            return []

        packets = []
        try:
            cap = rdpcap(pcap_path)
        except Exception:
            return []

        for pkt in cap:
            v2gtp_pkt = self._parse_packet(pkt)
            if v2gtp_pkt:
                packets.append(v2gtp_pkt)

        return packets

    def _parse_packet(self, pkt) -> Optional[V2GTP_Packet]:
        """Parse a single scapy packet into a V2GTP_Packet."""
        if not (TCP in pkt or UDP in pkt):
            return None

        # Extract IP layer
        if IPv6 in pkt:
            src_ip = pkt[IPv6].src
            dst_ip = pkt[IPv6].dst
        else:
            src_ip = getattr(pkt, 'src', '')
            dst_ip = getattr(pkt, 'dst', '')

        # Extract ports
        if TCP in pkt:
            src_port = pkt[TCP].sport
            dst_port = pkt[TCP].dport
        else:
            src_port = pkt[UDP].sport
            dst_port = pkt[UDP].dport

        # Determine direction
        if src_ip == self.evcc_ip:
            direction = "EVCC->SECC"
        elif src_ip == self.secc_ip:
            direction = "SECC->EVCC"
        else:
            direction = "UNKNOWN"

        # Extract V2GTP header if present
        proto_ver = inv_ver = payload_type = ""
        payload_len = 0
        raw_bytes = b""
        packet_info = ""

        if Raw in pkt:
            raw_bytes = bytes(pkt[Raw].load)
            if len(raw_bytes) >= 8:
                proto_ver = raw_bytes[0:1].hex().upper()
                inv_ver = raw_bytes[1:2].hex().upper()
                payload_type = raw_bytes[2:4].hex().upper()
                payload_len = int.from_bytes(raw_bytes[4:8], 'big')
                packet_info = (
                    f"V2GTP v={proto_ver} inv={inv_ver} "
                    f"type={payload_type} len={payload_len}"
                )

        return V2GTP_Packet(
            timestamp=pkt.time,
            src_ip=src_ip,
            dst_ip=dst_ip,
            src_port=src_port,
            dst_port=dst_port,
            protocol_version=proto_ver,
            inv_version=inv_ver,
            payload_type=payload_type,
            payload_length=payload_len,
            raw_bytes=raw_bytes,
            direction=direction,
            packet_info=packet_info,
        )

    def get_packet_sequence(self, packets: List[V2GTP_Packet],
                            filter_ip: Optional[str] = None) -> List[V2GTP_Packet]:
        """Get packets filtered by IP address."""
        if not filter_ip:
            return packets
        return [p for p in packets if p.src_ip == filter_ip or p.dst_ip == filter_ip]

    def find_handshake_sequence(self, packets: List[V2GTP_Packet]) -> List[V2GTP_Packet]:
        """Find SDP + SupportedAppProtocol handshake sequence."""
        seen_sdp = False
        result = []
        for pkt in packets:
            if pkt.dst_port == 15118 and pkt.src_port != 15118:
                if not seen_sdp:
                    seen_sdp = True
                result.append(pkt)
            elif seen_sdp and pkt.dst_port != 15118:
                # Dynamic TCP port after SDP
                result.append(pkt)
        return result

    def detect_missing_response(self, req_pkt: V2GTP_Packet,
                                all_packets: List[V2GTP_Packet],
                                timeout_ms: int = 3000) -> bool:
        """Detect if a response was missing within timeout window."""
        req_time = req_pkt.timestamp
        # Find response (opposite direction)
        for pkt in all_packets:
            if (abs(pkt.timestamp - req_time) < timeout_ms / 1000 and
                    pkt.direction != req_pkt.direction and
                    pkt.payload_type == req_pkt.payload_type):
                return False  # Response found
        return True  # Missing response

    def summarize(self, packets: List[V2GTP_Packet]) -> str:
        """Generate a human-readable summary of captured packets."""
        lines = [f"Total packets: {len(packets)}"]
        if not packets:
            return "\n".join(lines)

        dir_counts = {"EVCC->SECC": 0, "SECC->EVCC": 0, "UNKNOWN": 0}
        for p in packets:
            dir_counts[p.direction] = dir_counts.get(p.direction, 0) + 1

        lines.append(f"EVCC->SECC: {dir_counts['EVCC->SECC']}")
        lines.append(f"SECC->EVCC: {dir_counts['SECC->EVCC']}")

        payload_types = {}
        for p in packets:
            pt = p.payload_type or "RAW"
            payload_types[pt] = payload_types.get(pt, 0) + 1

        lines.append("Payload types:")
        for pt, count in sorted(payload_types.items()):
            lines.append(f"  {pt}: {count}")

        return "\n".join(lines)
