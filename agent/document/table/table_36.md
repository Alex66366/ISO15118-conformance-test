Table 36 — Semantics and type definition for AuthorizationReq


<table><tr><td>Element Name</td><td>Type</td><td>Semantics</td></tr><tr><td>Id</td><td>simpleType: 
ID 
string 
refer to Annex C.6 for the type definition</td><td>Optional: 
This attribute is used for referencing the message body in the signature header when a signature needs to be applied.</td></tr><tr><td>GenChallenge</td><td>simpleType 
genChallengeType 
base64Binary 
(length 16) 
refer to Annex C.6 for the type definition</td><td>Optional: 
The challenge sent by the SECC in PaymentDetailsRes message. This element contains the generated random number.</td></tr></table>
