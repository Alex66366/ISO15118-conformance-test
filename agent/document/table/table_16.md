Table 16 — SDP security and protocol option encoding


<table><tr><td>Description</td><td>Security</td><td>Transport protocol</td></tr><tr><td>Byte no. SDP request message</td><td>1</td><td>2</td></tr><tr><td>Byte no. SDP response message</td><td>19</td><td>20</td></tr><tr><td rowspan="4">Applicable values</td><td>0x00 = secured with TLS</td><td>0x00= TCP</td></tr><tr><td>0x01-0x0F = reserved</td><td>0x01-0x0F = reserved</td></tr><tr><td>0x10 = No transport layer security</td><td>0x10 = reserved for UDP</td></tr><tr><td>0x11-0xFF = reserved</td><td>0x11-0xFF = reserved</td></tr></table>
