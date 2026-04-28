Table 103 — Semantics and type definition for DC_EVPowerDeliveryParameterType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>DC_EVStatus</td><td>complexType
DC_EVStatusType
refer to subclause 8.5.4.2.</td><td>Current status of the EV.</td></tr><tr><td>BulkChargingComplete</td><td>simpleType:
boolean
refer to Annex C.6 for the type definition</td><td>Optional:
If set to TRUE, the EV indicates that bulk charge (approx. 80% SOC) is complete.</td></tr><tr><td>ChargingComplete</td><td>simpleType:
boolean
refer to Annex C.6 for the type definition</td><td>If set to TRUE, the EV indicates that full charge (100% SOC) is complete.</td></tr></table>
