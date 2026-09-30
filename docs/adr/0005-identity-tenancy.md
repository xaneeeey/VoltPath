# ADR-0005: OAuth2/JWT (RS256 + JWKS), RBAC from the database, Postgres RLS for tenant isolation

- Status: accepted · Date: 2026-09-30

## Context
The platform is multi-tenant (fleets from different companies), with roles that must see different things (masked vs precise location, PII), machine clients (OEM clouds), and an AI agent that acts for users. The brief asks for OAuth2/OIDC + JWT, RBAC with tenant isolation, and audit of every data access and agent action.

## Decision
- **Tokens.** The API is an OAuth2 authorization server for `password` and `client_credentials` grants. It issues **RS256 JWTs** with `kid`, publishes a **JWKS** and an OIDC discovery document. Consumers verify only against JWKS, so swapping in Keycloak, Cognito or Auth0 is a configuration change.
- **RBAC.** Permissions are data (`role_permission`) loaded at startup. Endpoints declare `require("perm")`. Location precision is itself a permission: without `location:precise`, coordinates snap to a ~5 km geohash cell.
- **Tenant isolation, defence in depth.**
  1. Redis keys are namespaced by tenant.
  2. The API connects as a non-owner role, and every request runs in a transaction with `SET LOCAL app.tenant_id`. **Row-level security** policies then filter every table.
  3. Compressed hypertables can't carry RLS, so they are exposed only through `security_barrier` views pinned to the tenant.
  A missing `WHERE tenant_id` in application code therefore cannot leak data.
- **Agent.** The copilot runs tools as `principal.as_agent()`: the caller's own permissions, tagged `actor_type=agent`. Actions are two-phase (propose, then a human confirms through a separate endpoint).
- **Audit.** The `audit_log` is append-only for the app role (no UPDATE/DELETE grants), written asynchronously in batches off the request path.
- **Erasure.** Right-to-erasure is a single SQL function that nulls PII, unlinks trips and records a receipt.

## Consequences
- Self-issued tokens mean we own key rotation. Mitigation: `kid` in headers and JWKS listing, and Helm mounts the key from a secret store.
- RLS adds a `set_config` per request (negligible) and requires disciplined use of `tenant_tx()`. Pipelines run as the owner by design (trusted, system-wide).
- mTLS for vehicles isn't applicable (device-free). OEM clouds authenticate with HMAC signatures or client credentials. TLS 1.3 terminates at the ingress (Helm values).
