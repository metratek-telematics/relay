# Security Rules

## Secrets
Never:
- print secrets;
- commit secrets;
- copy secrets into test fixtures;
- include tokens/passwords in logs, PR bodies, screenshots, prompts, or generated docs.

Treat .env, credentials, SSH keys, API keys, cookies, certificates, connection strings and auth tokens as sensitive.

## Authorization
Do not weaken authorization to make a feature work.
Do not replace permission checks with UI-only hiding.
Preserve server-side enforcement.

## Input/output
Validate untrusted input at the correct trust boundary.
Encode/escape output appropriate to context.
Avoid unsafe shell string construction.
Avoid SQL string interpolation.
Avoid unsafe HTML insertion.

## Dependencies
Do not disable security warnings without understanding them.
Do not add abandoned/unnecessary dependencies.

## Browser
Avoid:
- unsafe innerHTML for untrusted values;
- credential leakage in URLs;
- insecure storage of sensitive tokens;
- wildcard postMessage handling;
- overbroad CORS changes.

## Logging
Logs should aid diagnosis without containing secrets, personal data beyond need, or full sensitive payloads.
