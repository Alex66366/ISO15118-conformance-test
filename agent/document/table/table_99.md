Table 99 — Semantics and type definition for DC_EVStatusType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>EVReady</td><td>simpleType: 
boolean 
refer to Annex C.6 for the type definition</td><td>If set to TRUE, the EV is ready to charge.</td></tr><tr><td>EVErrorCode</td><td>simpleType: 
DC_EVErrorCodeType 
enumeration 
refer to Annex C.6 for the type definition</td><td>Indicates the EV internal status.Refer to Table 100 for details.</td></tr><tr><td>EVRESSSOC</td><td>simpleType: 
percentValueType 
byte (range: 0-100) 
refer to Annex C.6 for the type definition</td><td>State of charge of the EV&#x27;s battery (RESS)</td></tr></table>
