Table 67 — Semantics and type definition for PhysicalValueType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>Multiplier</td><td>simpleType: 
unitMultiplierType 
byte (range: -3..+3) 
refer to Annex C.6 for the type definition</td><td>The Multiplier defines the exponent to base 10 (dec). The final physical value is determined by: Value * 10^ Multiplier [Unit].</td></tr><tr><td>Unit</td><td>simpleType: 
unitSymbolType 
enumeration 
refer to Annex C.6 for the type definition</td><td>Unit of the value</td></tr><tr><td>Value</td><td>simpleType 
short 
refer to Annex C.6 for the type definition</td><td>Value which has to be multiplied</td></tr></table>
