# M10 HTTP API 与 Web UI

所有 `/api/v1/*` 请求使用 `Authorization: Bearer ...`，不接受 Cookie 或 query
token。mutation 还需 `Idempotency-Key`；修改配置或计划的 mutation 另需
`If-Match`（exact revision 或 `plan_hash`）。请求使用 strict JSON；duplicate
key、额外字段、超限 body/page/text 会被拒绝。

主要 endpoint：

- `GET /api/v1/session`
- `GET/PUT /api/v1/admin/config`
- `GET /api/v1/admin/directories`
- `POST /api/v1/admin/config/provider-probe`
- `POST /api/v1/admin/watches/{watch_id}/move-capability-probe`
- `GET /api/v1/discoveries`
- `GET /api/v1/folders`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `DELETE /api/v1/runs/{run_id}`
- `GET /api/v1/runs/{run_id}/plan`
- `GET /api/v1/runs/{run_id}/plans`
- `GET /api/v1/runs/{run_id}/plans/{version}/preview`
- `GET /api/v1/runs/{run_id}/interactions`
- `GET /api/v1/runs/{run_id}/events`
- `GET /api/v1/runs/{run_id}/events/stream`
- `POST /api/v1/runs/{run_id}/interactions`
- `POST /api/v1/runs/{run_id}/approve-and-apply`
- `POST /api/v1/runs/{run_id}/folder-disposition`
- `POST /api/v1/runs/{run_id}/reapply`
- `POST /api/v1/operations/runs/{run_id}/recover`
- `POST /api/v1/operations/runs/{run_id}/folder-disposition/recover`

SSE 使用 durable PostgreSQL `event_id` 作为 `id`，通过 `Last-Event-ID` 恢复。
cursor ahead/invalid fail closed。除 Admin-only 配置和目录选择接口外，浏览器投影
只包含 allowlisted typed fields，不返回 absolute path、prompt、Secret、DSN、
canonical plan 或 tool observation。

`GET /api/v1/runs/{run_id}` 在 approval 已 claim、但 settlement 尚未持久化时返回
`recovery_approval_id`；此时普通 apply fail closed，Admin 必须调用 exact
`recover`。所有 mutation 的幂等结果只由对应的 typed resolver 从 durable owner
重建，不会重新执行 provider、Agent 或 filesystem effect。

## 浏览器边界

同源 UI 只公开 `GET/HEAD /` 与 build manifest 中的哈希资源，不提供任意 URL 的
SPA fallback。`/api/*`、OpenAPI snapshot 和 manifest 本身都继续要求 Bearer。
浏览器经 session endpoint 确认 `role=admin` 后才保存 token；同一个 Admin
Bearer 也是非 UI API client 的唯一 credential。

Plan preview 分页返回 `move | unmapped | unchanged`、candidate ID、kind 和相对
source/destination。服务端先固定 PostgreSQL lineage，再在事务外加载并校验
canonical content-addressed plan。响应不包含 absolute root、source identity、
digest、canonical bytes、journal 或 rollback。

Config 中每个 watch 直接绑定入站 `root` 与最终 `library_root`；`work_type`
不参与路径选择。Admin GET 返回每个 watch 的 `root` 与 `library_root`，方便
结构化配置表单显示当前路径；provider secret 仍只返回
`api_key_configured`。PUT 兼容显式 string/key 格式，也接受绑定 exact
`If-Match` revision 的 `{mode:"retain"}` 或 `{mode:"replace", ...}`。省略
watch 表示删除；stale revision、缺失 retain target 和格式混用均 fail closed。
旧数据库 revision 中按类型保存的 route 只在读取历史记录时转换，不属于当前
HTTP wire。

Config 还包含严格的 `agent_budget`：

```json
{
  "max_model_turns": 64,
  "max_tool_calls": 64,
  "max_failures": 16,
  "max_total_tokens": 100000,
  "max_elapsed_seconds": 600
}
```

预算绑定创建 run 时的 exact config revision；后续配置修改不会增加已有 run
或 interaction 的剩余预算。时间上限范围为 1–3600 秒，其余字段也有 OpenAPI
声明的有界上限。旧 config revision 缺少该字段时使用上述默认值。

目录选择接口只枚举当前 Reeloom Pod 从 `/` 可见的真实目录，并返回所选目录的
absolute path 供结构化配置表单使用。它不读取或返回文件，不跟随 symlink，并
隐藏和拒绝所有 `.env*` 路径；其他 Pod 的文件系统不在其可见范围内。

Admin interaction history 只返回 M9 起显式保存的 request message 与 final reply；
旧记录标记 `content_available=false`。prompt、SDK transcript、tool call 和
observation 永不进入此投影。每页最多 100 条。

所有 read model 中的 `work_type` 为 `anime | tv | movie`。Movie 不增加 endpoint；
它复用相同的 lineage、preview、interaction、approve/apply、reapply 和 recovery
契约。Movie initial preview 返回 `move/unmapped`，amendment 返回
`move/unchanged`，且仍只包含相对路径。

M11 folder discovery 为 `DiscoverySummary` 和 run read model 增加可空的
`source_folder`。新 folder run 的 apply body 同时携带服务端已投影的 exact
`folder_disposition_plan_hash`；legacy run 继续接受原 wire。媒体 settlement 与
folder settlement 是两个 durable owner：成功媒体的残留目录由独立
`folder_disposition` approval/transaction 收敛，失败或断线不会让浏览器推断其
已归档。所有 bucket 目标都只以 `archive/name[.n]` 或 `fail/name[.n]` 相对路径
返回。

终态且没有未结算 interaction、media/folder transaction 的 run 会暴露
`delete_run` action。Admin 使用 `DELETE /api/v1/runs/{run_id}` 主动删除后，
该 run 及其 plans、events、interactions 会从公开 read model 隐藏，关联
discovery/folder projection 的 `run_id` 变为 `null`。实现只追加不可逆
tombstone，不删除媒体、canonical plan、journal 或底层审计历史；重复请求必须
复用原 `Idempotency-Key`。

Canonical OpenAPI snapshot 位于 [openapi-v1.json](openapi-v1.json)，可运行：

```bash
PYTHONPATH=src .venv/bin/python scripts/export_openapi.py --check
```
