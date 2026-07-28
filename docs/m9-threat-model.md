# M9 threat model

## Protected assets

M8 的 media roots、plan、approval、journal、secret、PostgreSQL history 和 Agent
session 继续受保护。M9 额外保护 Admin Bearer、interaction text、relative
filename display 和浏览器 mutation intent。

## Trust boundaries

| Boundary | Threat | Control |
| --- | --- | --- |
| Static shell | public file read、path traversal、asset substitution | exact `/`、manifest asset allowlist、hash assets、no directory listing |
| Browser token | XSS、URL/log/storage leak、shared device | Admin bootstrap、fixed localStorage key、strict CSP、no third-party script、logout/401 clear |
| API response → DOM | filename/message HTML injection、URL activation | runtime schema validation、React text rendering、no Markdown/innerHTML/autolink |
| UI → mutation | stale head、double submit、uncertain response | exact If-Match、stable idempotency key、disabled duplicate submit、durable refetch |
| SSE | cursor loss、duplicate event、connection amplification | one stream per run page、Last-Event-ID、bounded backoff、cursor-ahead resync |
| Plan store → preview | tamper、wrong plan kind、absolute path leak | lineage pinning、canonical/hash verification、typed projector、allowlisted fields |
| Config editing | root/secret loss、capability substitution、无界 Agent 成本 | Admin-readable paths、write-only secret、exact revision retain/replace、bounded run budget |
| Pod directory browser | path escape、symlink traversal、file or dotenv disclosure | Admin-only endpoint、rooted at current Pod `/`、relative canonical navigation、no-follow directory descriptors、directory names only、`.env*` exclusion |
| Run record deletion | active/recovery state hidden、double submit、audit loss | terminal settlement gate、stable idempotency key、append-only tombstone、preserved immutable history |

## Residual risks

- A successful same-origin script injection can read the Admin token and has the
  same authority as the UI. Deployment must use TLS, CSP and no third-party
  active content; Admins must log out on shared devices.
- A compromised host, reverse proxy, PostgreSQL or Admin credential remains
  outside the application trust boundary.
- Explicit interaction text is now an Admin-readable PostgreSQL record and is
  retained with the run backup set.
- The Admin config read model and directory browser intentionally reveal
  configured paths and browsable directory names visible inside the Reeloom
  Pod, but never file content.
