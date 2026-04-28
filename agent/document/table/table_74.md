Table 74 — Semantics and type definition for SAScheduleTupleType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>SAScheduleTupleID</td><td>simpleType: 
SAIDType 
unsignedByte 
refer to Annex C.6 for the type definition</td><td>Unique identifier within a charging session for a SAScheduleTuple element 
An SAID remains a unique identifier for one schedule throughout a charging session.</td></tr><tr><td>PMaxSchedule</td><td>complexType: 
PMaxScheduleType 
refer to subclause 8.5.2.14</td><td>Encapsulating element describing all relevant details for one PMaxSchedule from the secondary actor</td></tr><tr><td>SalesTariff</td><td>complexType: 
SalesTariffType 
refer to subclause 8.5.2.16</td><td>Optional: Encapsulating element describing all relevant details for one SalesTariff from the secondary actor</td></tr></table>
