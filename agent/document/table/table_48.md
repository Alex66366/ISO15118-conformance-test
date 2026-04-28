Table 48 — Semantics and type definition for MeteringReceiptReq


<table><tr><td>Element Name</td><td>Type</td><td>Semantic</td></tr><tr><td>Id</td><td>simpleType: 
ID 
string 
refer to Annex C.6 for the type definition</td><td>Optional: 
This attribute is used for referencing the message body in the signature header when a signature needs to be applied.</td></tr><tr><td>SessionID</td><td>simpleType: 
SessionIDType 
hexBinary (max length: 8) 
refer to Annex C.6 for the type definition</td><td>This message element is used by EVCC and SECC for uniquely identifying a V2G Communication Session. This element is identical with the one included in the message header. It is placed in the body in addition to be able to apply a signature to it (complete body is signed).</td></tr><tr><td>SAScheduleTupleID</td><td>simpleType: 
SAIDType 
short 
refer to Annex C.6 for the type definition</td><td>Optional: 
Unique identifier within a charging session for a SAScheduleTuple element. This element is just an echo of the value received either in the ChargingStatusRes (during AC charging) or the CurrentDemandRes (during DC charging) message from the SECC. 
An SAID remains a unique identifier for one schedule throughout a charging session.</td></tr><tr><td>MeterInfo</td><td>complexType 
MeterInfoType 
refer to subclause 8.5.2.6.</td><td>If the SECC indicated in the ChargingStatusRes (AC charging)/CurrentDemandRes (DC charging) that a MeteringReceiptReq is required this message element is the echo of the MeterInfo record received in the ChargingStatusRes (AC charging)/CurrentDemandRes (DC charging) from the SECC.</td></tr></table>
