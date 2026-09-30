# STRIDE threat model

Scope covers every trust boundary:
- OEM clouds → ingest (internet)
- browsers → web/API (internet)
- API → data stores (cluster network)
- the AI copilot → tools (model output is untrusted)
- operators → infrastructure (IaC, secrets)

| # | Threat (STRIDE) | Boundary / asset | Mitigation in VoltPath | Evidence |
|---|---|---|---|---|
| S1 | **Spoofing** an OEM to inject fake telemetry | OEM → ingest | HMAC-SHA256 over `timestamp.body` with a per-OEM secret; constant-time compare; optional mTLS on a separate OEM host (Helm `oemMtls`) | `tests/unit/test_ingest.py::test_rejects_bad_signature_and_replay` |
| S2 | Spoofing a user: credential stuffing | browser → API | bcrypt (cost 10) with a dummy-hash timing equaliser for unknown users; login rate limit of 20/min/IP | `test_login_rate_limited` |
| S3 | Forged or altered JWT | API | RS256 only (`algorithms=["RS256"]`, no `none`/HS confusion); issuer, audience, exp and nbf all required; `kid` in the header, JWKS published | `test_requires_auth_and_rejects_tampered_tokens` |
| T1 | **Tampering** with telemetry in transit | all | TLS 1.3 at ingress, TLS to MSK and Redis (Terraform), HMAC body integrity for webhooks | infra/terraform, helm values |
| T2 | Tampering with the audit trail | Postgres | App role has INSERT/SELECT only on `audit_log`; UPDATE, DELETE and TRUNCATE revoked | `test_audit_log_is_append_only` |
| T3 | Replay of a captured webhook | ingest | 5-minute timestamp window inside the HMAC; exact (vin, seq) dedup downstream | `test_rejects_bad_signature_and_replay` |
| R1 | **Repudiation** of an action ("I didn't book that") | API, copilot | Every authenticated request and every agent tool call is audited (`actor_type`, user, tenant, action, status, IP, request id); agent bookings require an explicit human confirm recorded as `created_via='agent'` | `test_copilot_two_phase_action_and_audit` |
| I1 | **Information disclosure** across tenants | API → DB | Postgres RLS on every tenant table; `security_barrier` views for hypertables; Redis keys namespaced by tenant; API connects as a non-owner role | `test_rls_isolates_tenants`, `test_location_masking_and_tenant_isolation` |
| I2 | Exposure of precise location / driver PII | API, UI, copilot | `location:precise` permission, otherwise ~5 km cell; `driver:read` for PII; copilot tools strip driver data before the model sees it | integration tests, `agent/tools.py` |
| I3 | Secrets in code or images | repo, images | Dev-only values in `.env.example`; production values from the secret manager via External Secrets; Semgrep secrets and Trivy secret scanning in CI | `.github/workflows/ci.yml` |
| I4 | Verbose errors leak internals | API | Generic 500 body with a request id; stack traces only in server logs | `api/app.py headers_mw` |
| D1 | **DoS** by flooding ingest | OEM → ingest | Back-pressure 429 on the producer queue; 8 MB body limit at ingress; HPA; Kafka absorbs bursts (1.87M-event backlog drained in 72 s) | load test results |
| D2 | API abuse | browser → API | Per-token and per-IP Redis rate limit (fixed window, Lua), `X-RateLimit-*` headers; page size capped at 200 | `test_security_headers_and_rate_limit_headers` |
| D3 | Expensive queries | API → DB | Keyset pagination only; bounded time ranges (`hours ≤ 720`); indexes verified with EXPLAIN | `db/explain/REPORT.md` |
| E1 | **Elevation of privilege** via the copilot (prompt injection, e.g. in a DTC description) | copilot → tools | Tools run as `principal.as_agent()` with the caller's own permissions; tool results are treated as data (system prompt); the only write tool just *proposes* and needs a human confirm on a separate endpoint by the same user; max 6 steps per turn | `test_copilot_cannot_escalate` |
| E2 | Horizontal privilege (confirming someone else's action) | copilot | Pending actions are namespaced by tenant, single-use (`GETDEL`), and bound to the proposing user | `test_copilot_two_phase_action_and_audit` |
| E3 | Container breakout / lateral movement | k8s | Non-root UID 10001, read-only root FS, all capabilities dropped, seccomp RuntimeDefault, default-deny NetworkPolicy, private EKS endpoint | `infra/helm/voltpath/templates` |

## OWASP API Top 10 (2023) mapping
| Risk | Control |
|---|---|
| API1 Broken object-level authorisation | RLS plus tenant-scoped Redis keys; cross-tenant VIN returns 404 |
| API2 Broken authentication | RS256 JWT, JWKS, bcrypt, login rate limit |
| API3 Broken object-property-level authorisation | Field-level masking (location, PII) by permission |
| API4 Unrestricted resource consumption | Rate limits, page caps, bounded ranges, body size limit, back-pressure |
| API5 Broken function-level authorisation | `require(permission)` on every route; permissions are data |
| API6 Unrestricted access to sensitive business flows | Booking needs a human confirm; OEM webhooks are signed |
| API7 SSRF | No user-supplied URLs are fetched server-side |
| API8 Security misconfiguration | Security headers, CSP, HSTS, `server_tokens off`, Trivy misconfig scan |
| API9 Improper inventory | Versioned `/v1`, OpenAPI published, web contract test |
| API10 Unsafe consumption of APIs | OEM payloads are strictly validated (regex, bounds, VIN check digit) into a DLQ; LLM output is never executed |

## Compliance (GDPR / India DPDP)
- **Data minimisation.** Drivers are the only personal data subjects; vehicles are pseudonymous (VIN).
- **Purpose limitation and access.** `driver:read` gates PII; the copilot never receives it.
- **Right to erasure.** `DELETE /v1/drivers/{id}/personal-data` runs the `erase_driver()` SQL function, which nulls PII (enforced by a CHECK constraint), unlinks trips and deletes assignments. It writes an `erasure_request` receipt and is audited.
- **Retention.** Telemetry is 30 days warm; the lake uses lifecycle rules (3 years); Kafka keeps 24 h.
- **Audit.** Every data access and agent action is audited (append-only).
