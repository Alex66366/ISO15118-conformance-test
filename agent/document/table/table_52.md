Table 52 — Semantics and type definition for CableCheckRes


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>EVSEProcessing</td><td>simpleType: 
EVSEProcessingType 
enumeration 
refer to Annex C.6 for the type definition</td><td>Parameter indicating that the EVSE has finished the processing that was initiated after the CableCheckReq or if the EVSE is still processing at the time, the response message was sent.</td></tr><tr><td>ResponseCode</td><td>simpleType: 
responseCodeType 
enumeration 
refer to Annex C.6 for the type definition</td><td>ResponseCode indicating the acknowledgment status of any of the V2G messages received by the SECC.</td></tr><tr><td>DC_EVSEStatus</td><td>complexType: 
DC_EVSEStatusType 
refer to subclause 8.5.4.1</td><td>Current status of the EVSE</td></tr></table>
