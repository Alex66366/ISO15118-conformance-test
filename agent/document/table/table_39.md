Table 39 — Semantics and type definition for ChargeParameterDiscoveryRes


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>EVSEProcessing</td><td>simpleType: 
EVSEProcessingType 
enumeration 
refer to Annex C.6 for the type definition</td><td>Parameter indicating that the EVSE has finished the processing that was initiated after the ChargeParameterDiscoveryReq or that the EVSE is still processing at the time, the response message was sent.</td></tr><tr><td>ResponseCode</td><td>simpleType: 
responseCodeType 
enumeration 
refer to Annex C.6 for the type definition</td><td>ResponseCode indicating the acknowledgment status of any of the V2G messages received by the SECC.</td></tr><tr><td>SAScheduleList</td><td>complexType: 
SAScheduleListType substitutes abstract type SASchedulesType 
refer to subclause 8.5.2.12</td><td>Optional: 
Includes several tuples of schedules from secondary actors. The SECC shall only omit the parameter &#x27;SAScheduleList&#x27; in case EVSEProcessing is set to &#x27;Ongoing&#x27;.</td></tr><tr><td>AC_EVSEChargeParameter</td><td>complexType: 
AC_EVSEChargeParameterType 
substitutes abstract type EVSE_ChargeParameterType 
refer to subclause 8.5.3.3</td><td>This element is used by the SECC for initiating the target setting process for AC charging.</td></tr><tr><td>DC_EVSEChargeParameter</td><td>complexType: 
DC_EVSEChargeParameterType 
substitutes abstract type EVSE_ChargeParameterType 
refer to subclause 8.5.4.4</td><td>This element is used by the SECC for initiating the target setting process for DC charging.</td></tr></table>
