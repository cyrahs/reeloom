# M8 deployment

Reeloom M8 固定为一个 server process、一个 worker 和一个 PostgreSQL 17
control plane。`compose.yaml` 是最小 production composition；部署必须通过 secret
manager 注入所有 `${...:?required}` 值，不能把 credential 写进仓库或 `.env*`。
Reeloom server 本身不会加载 dotenv。

部署前应按 [M8 threat model](m8-threat-model.md) 校验反向代理、credential、mount 和
backup 边界。

启动顺序：

1. 用 `REELOOM_MIGRATION_POSTGRES_DSN` 指向 migration role；用
   `REELOOM_POSTGRES_DSN` 指向独立的 `reeloom_app` role。两套 credential
   必须不同，密码若进入 DSN 必须 URL encode。启动会以 migration role 串行应用并
   核对 version/checksum，业务 pool 只使用 application role。
2. 启动 PostgreSQL 17，确认备份与恢复演练已完成。
3. 将 state root 与授权 media roots 挂载到固定绝对路径。
4. 以 `REELOOM_WORKERS=1` 启动 server。启动必须同时取得 state-root process lock
   与 PostgreSQL lifetime advisory lock。
5. 用 exact Host 请求 `/health`，再开放反向代理流量。

`REELOOM_TMDB_API_KEY` 由 deployment secret manager 注入，仅构造唯一允许的业务
网络 adapter；server 不读取 dotenv。模型 provider key 仍通过 admin config
write-only 写入 filesystem SecretStore。

Bearer credential 分为 `admin`、`operator`、`viewer`。反向代理必须保留 Host，
禁止改写 Origin，并关闭响应缓存和代理缓冲 SSE。所有 provider origin 必须显式
列入 `REELOOM_PROVIDER_ORIGINS`。

## Backup / restore

必须一起备份以下 owner，不能从其中一份推导或覆盖另一份：

- PostgreSQL：config、watch/discovery、run/runtime/session、interaction、lineage、
  approval/settlement；
- state root：write-only Secret、content-addressed Plan、append-only Journal；
- media roots：实际归档媒体。

恢复时先停止 server，恢复 PostgreSQL 和 state root 到同一一致性时间点，再恢复
media snapshot。启动 reconciler 只会终止遗留 interaction、重排旧 boot job、或按
terminal journal 补 settlement；它不会调用 Agent 来猜测 recovery。

## Failure behavior

schema mismatch、checksum drift、PostgreSQL 版本不是 17、数据库不可达、第二实例、
第二 worker、state-root symlink 或不安全权限都会 fail closed。数据库故障期间不得
执行新的 rename。terminal journal 已落盘但 settlement 未提交时，只能走 operator
`recover`。
