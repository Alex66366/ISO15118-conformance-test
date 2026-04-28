Table 58 — Semantics and type definition for WeldingDetectionRes


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>ResponseCode</td><td>simpleType: 
responseCodeType 
enumeration 
refer to Annex C.6 for the type definition</td><td>ResponseCode indicating the acknowledgment status of any of the V2G messages received by the SECC.</td></tr><tr><td>DCEVSEStatus</td><td>complexType: 
DCEVSEStatusType 
refer to subclause 8.5.4.1</td><td>Current status of the EVSE</td></tr><tr><td>EVSEPresentVoltage</td><td>complexType 
PhysicalValueType 
refer to subclause 8.5.2.7</td><td>Present voltage of EVSE, refers to SAE Voltage Output</td></tr></table>
