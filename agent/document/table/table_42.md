Table 42 — Semantics and type definition for CertificateUpdateReq


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>Id</td><td>simpleType: 
ID 
string 
refer to Annex C.6 for the type definition</td><td>This element is used for referencing the entire message body in the message header when a signature needs to be applied.</td></tr><tr><td>ContractSignatureCertChain</td><td>complexType: 
CertificateChainType 
refer to subclause 8.5.2.5</td><td>Contains the currently available signature certificate including the certificate chain in the EVCC. The SECC uses this certificate(chain) to check the message signature included in the header of the message. 
The complete chain is transmitted to allow stand-alone verification of the signature.</td></tr><tr><td>eMAID</td><td>simpleType: 
eMAIDType 
string (min length: 14, max length: 15) 
refer to Annex C.6 for the type definition</td><td>This element identifies the charging contract. The format is defined in Annex H.</td></tr><tr><td>ListOfRootCertificateIDs</td><td>complexType: 
ListOfRootCertificateIDsType 
refer to subclause 8.5.2.27</td><td>This list contains the Certificate IDs of all Root Certificates currently installed in the EVCC.</td></tr></table>
