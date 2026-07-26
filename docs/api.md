# M8 HTTP API

所有 `/api/v1/*` 请求使用 `Authorization: Bearer ...`，不接受 Cookie 或 query
token。mutation 还需 `Idempotency-Key` 与 `If-Match`（exact revision 或
`plan_hash`）。请求使用 strict JSON；duplicate key、额外字段、超限 body/page/text
会被拒绝。

主要 endpoint：

- `GET/PUT /api/v1/admin/config`
- `POST /api/v1/admin/config/provider-probe`
- `GET /api/v1/discoveries`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/plan`
- `GET /api/v1/runs/{run_id}/events`
- `GET /api/v1/runs/{run_id}/events/stream`
- `POST /api/v1/runs/{run_id}/interactions`
- `POST /api/v1/runs/{run_id}/approve-and-apply`
- `POST /api/v1/runs/{run_id}/reapply`
- `POST /api/v1/operations/runs/{run_id}/recover`

SSE 使用 durable PostgreSQL `event_id` 作为 `id`，通过 `Last-Event-ID` 恢复。
cursor ahead/invalid fail closed。浏览器投影只包含 allowlisted typed fields，不返回
absolute path、prompt、Secret、DSN、canonical plan 或 tool observation。

`GET /api/v1/runs/{run_id}` 在 approval 已 claim、但 settlement 尚未持久化时返回
`recovery_approval_id`；此时普通 apply fail closed，operator 必须调用 exact
`recover`。所有 mutation 的幂等结果只由对应的 typed resolver 从 durable owner
重建，不会重新执行 provider、Agent 或 filesystem effect。
