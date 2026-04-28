Table 76 — Semantics and type definition for PMaxScheduleEntryType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>RelativeTimeInterval</td><td>complexType: 
RelativeTimeInterval substitutes abstract element TimeInterval. 
refer to subclause 8.5.2.18</td><td>Extends the TimeIntervalType and defines the time interval the PMaxScheduleEntry is valid for based upon relative times.</td></tr><tr><td>PMax</td><td>complexType: 
PhysicalValueType 
refer to subclause 8.5.2.7</td><td>Defines maximum amount of power for a time interval to be drawn from the EVSE power outlet the vehicle is connected to. 
This value represents the total power over all selected phases.</td></tr></table>
