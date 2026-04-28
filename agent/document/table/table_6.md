Table 6 — TLS authentication


<table><tr><td>TLS Authentication</td><td>Requirements to EVCC</td><td>Requirements to SECC</td></tr><tr><td>Unilateral (server side)</td><td>Availability of root certificates to check the authenticity of the SECC Certificate Functional support for OCSP request/response processing to check the validation state of the SECC Certificate during TLS handshake</td><td>SECC Certificate and corresponding private key OCSP response to provide information about the validation state of own SECC Certificate The SECC shall cache and store internally OCSP responses for its own chain of sub-cs certificates regularly (at least once per week).</td></tr></table>
