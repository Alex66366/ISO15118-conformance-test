Table 2 — Certificate extension examples


<table><tr><td>Certificate extensions</td><td>Description</td></tr><tr><td>Key usage</td><td>Usage of the corresponding private key (e.g. Digital Signature, Non-Repudiation, Key Encipherment, ...)</td></tr><tr><td>Extended Key Usage</td><td>Further specification of key usage using OIDs, e.g.: 
Server Authentication (1.3.6.1.5.5.7.3.1) 
Client Authentication (1.3.6.1.5.5.7.3.2) 
Note, sometimes here also the following values are encoded: 
Netscape SGC (1.3.6.1.4.1.311.10.3.3) 
Microsoft SGC (2.16.840.1.113730.4.1) 
SGC stands for Server Gated Crypto and indicated that the server may also use strong cryptography for the connection with the client&#x27;s browser. This extension was used at the time of strong crypto export control to enable financial web site to provide appropriate protection of the data transfer.</td></tr><tr><td>CRL distribution point</td><td>Location to retrieve certificate revocation lists</td></tr><tr><td>OCSP</td><td>Location to retrieve OCSP response (exists only for SubCA certificates). Refer to IETF RFC 6960 for details.</td></tr><tr><td>Authority information</td><td>Additional authorization information</td></tr><tr><td>Subject alternative name</td><td>Alternative names of the entity</td></tr><tr><td>Basic constraint = CA</td><td>True if the certificate is V2G Root Certificate or SubCA certificate.</td></tr></table>
