Table 9 — Generic V2GTP header structure


<table><tr><td>Header field</td><td>Header field description</td><td>Header field values</td></tr><tr><td>Protocol Version</td><td>Identifies the protocol version of V2GTP messages.</td><td>0x01: V2GTP version 1
0x00, 0x02-0xFF: reserved by document</td></tr><tr><td>Inverse Protocol Version</td><td>Contains the bit-wise inverse value of the protocol version which is used in conjunction with the V2GTP protocol version as a protocol verification pattern to ensure that a correctly formatted V2GTP message is received.
Equals the &lt;Protocol Versions&gt; XOR 0xFF</td><td>0xFE: V2GTP Version 1</td></tr><tr><td>Payload Type (GH_PT)</td><td>Contains information about how to decode the payload following the V2GTP header.</td><td>Refer to Table 10 for a complete list of payload type values.</td></tr><tr><td>Payload Length (GH_PL)</td><td>Contains the length of the V2GTP message payload in bytes (i.e. excluding the generic V2GTP header bytes).</td><td>0...4294967295 (= &lt;d&gt;)</td></tr></table>
