# MediaMTX RTSPS certificate

Install a valid TLS certificate and matching private key for the configured RTSPS hostname:

- fullchain.pem
- privkey.pem

The private key must be readable by the MediaMTX container and must never be committed.
Deployment scripts stop before bringing up the stack if either file is missing.
Use a dedicated certificate or copy the current certificate from the reverse proxy using
a root-only operation. Protect the private key with restrictive permissions.
