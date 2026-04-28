Table 32 — Semantics and type definition for PaymentServiceSelectionReq


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>SelectedPaymentOption</td><td>simpleType: 
paymentOptionType 
enumeration 
refer to Annex C.6 for the type definition</td><td>This element is used for indicating the payment type selected for the use of all selected services in the selectedServiceList.</td></tr><tr><td>SelectedServiceList</td><td>complexType: 
SelectedServiceelistType 
refer to subclause 8.5.2.24</td><td>List contains all selected ServiceIDs and the optional parameterSetID for applicable for the respective serviceID.</td></tr></table>
