Table 37 — Semantics and type definition for AuthorizationRes


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td rowspan="3">EVSEProcessing</td><td>simpleType: 
EVSEProcessingType</td><td rowspan="3">Parameter indicating that the EVSE has finished the processing that was initiated after the AuthorizationReq or if the EVSE is still processing at the time, the response message was sent.</td></tr><tr><td>enumeration</td></tr><tr><td>refer to Annex C.6 for the type definition</td></tr><tr><td rowspan="3">ResponseCode</td><td>simpleType: 
responseCodeType</td><td rowspan="3">ResponseCode indicating the acknowledgment status of any of the V2G messages received by the SECC.</td></tr><tr><td>enumeration</td></tr><tr><td>refer to Annex C.6 for the type definition</td></tr></table>
