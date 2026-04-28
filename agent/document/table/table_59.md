Table 59 —Semantics and type definition for ServiceType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>ServiceID</td><td>simpleType: 
unsignedShort 
refer to Annex C.6 for the type definition</td><td>Unique identifier of the service</td></tr><tr><td>ServiceName</td><td>simpleType: 
serviceTypeName 
string (max length: 32) 
refer to Annex C.6 for the type definition</td><td>Optional: 
Human readable service name</td></tr><tr><td>ServiceCategory</td><td>simpleType: 
serviceCategoryType 
enumeration 
refer to Annex C.6 for the type definition</td><td>Category of a service, corresponds to the defined services, derived from the base service</td></tr><tr><td>ServiceScope</td><td>simpleType: 
serviceScopeType 
string (max length: 64) 
refer to Annex C.6 for the type definition</td><td>Optional: 
Additional information about usage of that service</td></tr><tr><td>FreeService</td><td>simpleType: 
boolean 
refer to Annex C.6 for the type definition</td><td>This element is used by the SECC to indicate if a service can be used by the EVCC free of charge or not. If FreeService is equal to true, the EV can use the offered service without payment. If FreeService is equal to false, the service, if used by the EV, will be billed using the payment method negotiated using the payment option message element.</td></tr></table>
