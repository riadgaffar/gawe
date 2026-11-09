# Pinned timestamp-authority roots

Signed timestamps are checked against these roots only, never the system trust store, so the
check is the same on every machine and works offline. Each file's header gives the certificate's
SHA-256 fingerprint and where it came from. Change them only on purpose, with a reason in the
commit message.

| File | Root | Authority |
|---|---|---|
| `digicert_trusted_root_g4.pem` | DigiCert Trusted Root G4 | `digicert` (`timestamp.digicert.com`) |
| `usertrust_rsa_ca.pem` | USERTrust RSA Certification Authority (Sectigo's chain reaches it through a cross-signed Time Stamping Root R46) | `sectigo` (`timestamp.sectigo.com`) |
