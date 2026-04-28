Table 78 — Semantics and type definition for SalesTariffEntryType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>RelativeTimeInterval</td><td>complexType: 
RelativeTimeInterval substitutes abstract element TimeInterval. 
refer to subclause 8.5.2.18</td><td>Extends the TimelIntervalType and defines the time interval the SalesTariffEntry is valid for based upon relative times.</td></tr><tr><td>EPriceLevel</td><td>simpleType 
unsignedByte 
refer to Annex C.6 for the type definition</td><td>Optional: 
Defines the price level of this SalesTariffEntry (referring to NumEPriceLevels). Small values for the EPriceLevel represent a cheaper TariffEntry. Large values for the EPriceLevel represent a more expensive TariffEntry.</td></tr><tr><td>ConsumptionCost</td><td>complexType: 
ConsumptionCostType 
refer to subclause 8.5.2.19</td><td>Optional: 
Defines additional means for further relative price information and/or alternative costs.</td></tr><tr><td colspan="3">NOTE The definition of EPriceLevel allows simple price-based optimization by the EVCC without any ConsumptionCost element.</td></tr></table>
