Table 84 —Semantics and type definition for ParameterSetType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>ParameterSetID</td><td>simpleType:shortrefer to Annex C.6 for the type definition</td><td>This element is used to select a specific parameter set for a specific ServideID when selecting a service using thePaymentServiceSelectionReq message.</td></tr><tr><td>Parameter</td><td>complexType:ParameterTyperefer to subclause 8.5.2.23</td><td>This element is used by the SECC to indicate which service specific parameters can be selected for a certain service using the ParameterSetID.The number of Parameter elements is limited to 16 .</td></tr></table>
