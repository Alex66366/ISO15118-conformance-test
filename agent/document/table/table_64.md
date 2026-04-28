Table 64 — Semantics and type definition for CertificateChainType


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>Id</td><td>simpleType: 
ID 
string 
refer to Annex C.6 for the type definition</td><td>Optional: 
This attribute is used for referencing the message element in the signature header when a signature needs to be applied. It is used in response messages only.</td></tr><tr><td>Certificate</td><td>simpleType: 
certificateType 
base64Binary (max length: 800) 
refer to Annex C.6 for the type definition</td><td>An x.509v3 Certificate (the &quot;client&quot; certificate). The certificate is DER encoded.</td></tr><tr><td>SubCertificates</td><td>complexType: 
SubCertificatesType 
refer to subclause 8.5.2.26</td><td>Optional: 
The Chain with all Subcertificates to the Root-Certificate (not including root certificate)</td></tr></table>
