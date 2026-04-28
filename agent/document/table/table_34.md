Table 34 — Semantics and type definition for PaymentDetailsReq


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>eMAID</td><td>simpleType: eMAIDType string (min length: 14, max length: 15) refer to Annex C.6 for the type definition</td><td>This element identifies the charging contract. The format is defined in Annex H.</td></tr><tr><td>ContractSignatureCertChain</td><td>complexType: CertificateChainType refer to subclause 8.5.2.4</td><td>This element contains the Contract Certificate and optional SubCertificates</td></tr></table>
