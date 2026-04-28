Table 44 — Semantics and type definition for CertificateInstallationReq


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>Id</td><td>simpleType: 
ID 
string 
refer to Annex C.6 for the type definition</td><td>This element is used for referencing the entire message body in the message header when a signature needs to be applied.</td></tr><tr><td>OEMProvisioningCert</td><td>simpleType 
certificateType 
base64Binary 
(max. length 800) 
refer to Annex C.6 for the type definition</td><td>An EV specific certificate that was earlier installed in the EVCC typically by an OEM. The ID of this certificate together with the information stored at the SA (contract partner) is used to identify the currently valid contract of the EV. 
The certificate ID is given to the SA by the customer using a different communication channel. The certificate itself (i.e. its public key) is used to encrypt in the CertificateInstallationRes later on. 
The certificate is DER encoded. 
Please refer also to Annex E.1 and Annex 0.</td></tr><tr><td>ListOfRootCertificateIDs</td><td>complexType: 
ListOfRootCertificateIDsType 
refer to subclause 8.5.2.27</td><td>This list contains the Certificate IDs of all Root Certificates currently installed in the EVCC.</td></tr></table>
