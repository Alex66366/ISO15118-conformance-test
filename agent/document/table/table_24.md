Table 24 — Semantics and type definition for a V2G message header


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>SessionID</td><td>simpleType: 
SessionIDType: hexBinary (max length: 8)</td><td>This message element is used by EVCC and SECC for uniquely identifying a V2G Communication Session. Refer to subclause 8.4.2 for requirements relative to this message element.</td></tr><tr><td>Notification</td><td>complexType: 
NotificationType, see subclause 8.5.2.8</td><td>Optional: 
This element is used by the SECC for transmitting additional fault information when an error occurred on the SECC side.</td></tr><tr><td>xmlns:Signature</td><td>separate namespace: 
&quot;http://www.w3.org/2000/09/xhtmlsig#&quot;</td><td>Optional: 
This element is used if a certain V2G message requires to be signed.</td></tr></table>
