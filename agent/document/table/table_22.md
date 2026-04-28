Table 22 — Semantics and type definition for supportedAppProtocolRes message elements


<table><tr><td>Element/Attribute Name</td><td>Type</td><td>Semantics</td></tr><tr><td>SchemaID</td><td>simpleType: 
unsignedByte 
refer to Annex C.2 for the 
type definition</td><td>Optional: 
This message element is used by the SECC to reference one of the EVCC supported protocols received in the request message.</td></tr><tr><td>ResponseCode</td><td>simpleType: 
responseCodeType 
enumeration 
refer to Annex C.2 for the 
type definition</td><td>This message element is used by the SECC for indicating whether the list of protocols received from the EVCC includes at least one protocol matching with the protocols supported by the SECC.</td></tr></table>
