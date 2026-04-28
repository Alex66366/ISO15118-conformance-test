Table 56 — Semantics and type definition for CurrentDemandRes


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>ResponseCode</td><td>simpleType: 
responseCodeType 
enumeration 
refer to Annex C.6 for the type definition</td><td>ResponseCode indicating the acknowledgment status of any of the V2G messages received by the SECC.</td></tr><tr><td>DCEVSEStatus</td><td>complexType: 
DCEVSEStatusType 
refer to subclause 8.5.4.1</td><td>Current status of the EVSE</td></tr><tr><td>EVSEPresentVoltage</td><td>complexType 
PhysicalValueType 
refer to subclause 8.5.2.7</td><td>Present output voltage of the EVSE. Refer to SAE - ISO Mapping Table</td></tr><tr><td>EVSEPresentCurrent</td><td>complexType 
PhysicalValueType 
refer to subclause 8.5.2.7</td><td>Present output current of the EVSE. Refer to SAE - ISO Mapping Table</td></tr><tr><td>EVSECurrentLimitAchieved</td><td>simpleType
boolean
refer to Annex C.6 for the type definition</td><td>If set to TRUE, the EVSE has reached its current limit.</td></tr><tr><td>EVSEVoltageLimitAchieved</td><td>simpleType
boolean
refer to Annex C.6 for the type definition</td><td>If set to TRUE, the EVSE has reached its voltage limit</td></tr><tr><td>EVSEPowerLimitAchieved</td><td>simpleType
boolean
refer to Annex C.6 for the type definition</td><td>If set to TRUE, the EVSE has reached its power limit</td></tr><tr><td>EVSEMaximumVoltageLimit</td><td>complexType
PhysicalValueType
refer to subclause 8.5.2.7</td><td>Optional:
Maximum voltage the EVSE can deliver</td></tr><tr><td>EVSEMaximumCurrentLimit</td><td>complexType
PhysicalValueType
refer to subclause 8.5.2.7</td><td>Optional:
Maximum current the EVSE can deliver</td></tr><tr><td>EVSEMaximumPowerLimit</td><td>complexType
PhysicalValueType
refer to subclause 8.5.2.7</td><td>Optional:
Maximum power the EVSE can deliver</td></tr><tr><td>EVSEID</td><td>simpleType:
evseIDType
hexBinary (max length:32)
refer to Annex C.6 for the type definition</td><td>Any ID that uniquely identifies the EVSE and the power outlet the vehicle is connected to. The format of this message element is defined in Annex H.2. If an SECC cannot provide such ID data, the value of the EVSEID is set to zero (00hex).</td></tr><tr><td>SAScheduleTupleID</td><td>simpleType:
SAIDType
short
refer to Annex C.6 for the type definition</td><td>Unique identifier within a charging session for a SAScheduleTuple element. This is used by the SECC to indicate which tariff applies for the energy currently transferred. An SAID remains a unique identifier for one schedule throughout a charging session.</td></tr><tr><td>MeterInfo</td><td>complexType
MeterInfoType
refer to subclause 8.5.2.6</td><td>Optional:
Includes the MeterInfo record containing the latest meter reading and other meter relevant data.</td></tr><tr><td>ReceiptRequired</td><td>simpleType
boolean
refer to Annex C.6 for the type definition</td><td>Optional:
This element is used by the SECC to indicate that the EVCC is required to send a MeteringReceiptReq message for the purpose of signing the meter info record. If ReceiptRequired is equal to True, the EVCC is required to send a MeteringReceiptReq message including a signature. If ReceiptRequired is equal to False the EVCC is not required to send a MeteringReceiptReq message.</td></tr></table>
