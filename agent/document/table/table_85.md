Table 85 —Semantics and type definition for ParameterType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>Name</td><td>simpleType: 
string 
refer to Annex C.6 for the type definition</td><td>This element is used to indicate the name of the parameter.</td></tr><tr><td rowspan="2">One of the element: 
• boolValue 
• byteValue 
• shortValue 
• intValue 
• physicalValue 
• stringValue</td><td>simpleType: 
boolean (boolValue) 
byte (byteValue) 
short (shortValue) 
int (intValue) 
string (stringValue) 
refer to Annex C.6 for the type definition</td><td rowspan="2">This element is used to indicate the value for the parameter indicated by the element Name. A choice of 6 different element types. Only one for each parameter can be selected.</td></tr><tr><td>complexType: 
PhysicalValueType (physicalValue) 
refer to subclause 8.5.2.7</td></tr></table>
