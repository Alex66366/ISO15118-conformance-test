Table 77 — Semantics and type definition for SalesTariffType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>Id</td><td>simpleType: 
ID 
string 
refer to Annex C.6 for the type definition</td><td>Optional: 
This attribute is used for referencing the message element in the signature header when a signature needs to be applied.</td></tr><tr><td>SalesTariffID</td><td>simpleType: 
SAIDType 
short 
refer to Annex C.6 for the type definition</td><td>SalesTariff identifier used to identify one sales tariff. 
An SAID remains a unique identifier for one schedule throughout a charging session.</td></tr><tr><td>SalesTariffDescription</td><td>simpleType: 
tariffDescriptionType 
string (max. length: 32) 
refer to Annex C.6 for the type definition</td><td>Optional: 
A human readable title/short description of the sales tariff e.g. for HMI display purposes</td></tr><tr><td>NumEPriceLevels</td><td>simpleType 
unsignedByte 
refer to Annex C.6 for the type definition</td><td>Optional: 
Defines the overall number of distinct price levels used across all provided SalesTariff elements.</td></tr><tr><td>SalesTariffEntry</td><td>complexType: 
SalesTariffEntryType 
refer to subclause 8.5.2.17</td><td>Encapsulating element describing all relevant details for one time interval of the SalesTariff. 
The number of SalesTariffEntry elements is limited by the parameter MaxEntriesSAScheduleTuple (according to [V2G2-261]). The maximum value shall be 1024.</td></tr></table>
