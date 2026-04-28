Table 8 — Supported TCP ports for V2GTP


<table><tr><td>Name</td><td>Protocol</td><td>Port number</td><td>Description</td></tr><tr><td>V2G SRC TCP DATA</td><td>TCP(unicast)</td><td>Port number in the range of Dynamic Ports (49152-65535) as defined in IETF RFC 6335.</td><td>V2GTP source port at a Primary Actor (e.g. EVCC) that implements the V2GTP protocol.</td></tr><tr><td>V2G DST TCP DATA</td><td>TCP (unicast)</td><td>Port number at V2GTP entity providing a V2GTP destination port number in the range of Dynamic Ports (49152-65535) as defined in IETF RFC 6335. For a SECC it will be dynamically assigned by the SDP mechanism (7.10.1)</td><td>V2GTP destination port at a Primary Actor (e.g. SECC)</td></tr></table>
