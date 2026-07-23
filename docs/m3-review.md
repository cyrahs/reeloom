# M3 Definition of Done

日期：2026-07-23

结论：M3 已完成。实现保持单 Agent、单 provider port 和四个 TMDB 业务工具，
未引入 MCP、通用 HTTP 工具、真实网络测试或文件副作用。

## 交付核对

- [x] provider-neutral `TmdbProvider` 可由 fake 与 HTTP adapter 实现。
- [x] HTTP adapter 固定 TMDB API v3 host 和 endpoint，不接受任意 URL。
- [x] search、series、season 与 select tools 使用 strict execution-side schema。
- [x] 每个工具都经过 phase、capability、调用预算和 observation 上限。
- [x] search ID 通过 `TmdbCandidatesObserved` 绑定到当前 run。
- [x] `media_type`（TV/Movie namespace）与 `work_type`
  （anime/tv_series/movie archive category）明确分离。
- [x] `RunStarted` 必须显式绑定 trusted `work_type`，search filter 不能跨类型。
- [x] capability 使用 `(work_type, tmdb_id)`，不混淆 TV/Movie 同号 ID。
- [x] anime search 和 details 都要求 TMDB Animation genre 16，不做 fallback。
- [x] movie search 使用 `/search/movie` 并解析 movie title/release 字段。
- [x] movie metadata 使用 `/movie/{id}` 并返回有界字段；`adult` 必须是严格
  boolean，metadata 尚未开放为 Agent 工具。
- [x] `search_tmdb` schema 不包含 adult 开关；工具执行端固定传
  `include_adult=true`，模型不能关闭或改变该策略。
- [x] 非候选 ID 在 provider 调用前被拒绝。
- [x] `SeriesSelected` 是进入 `MAP_EPISODES` 的唯一 M3 领域转换。
- [x] zh-CN 名称优先，original name 为回退，首播年份缺失时 fail closed。
- [x] OVA/OAD hint 支持中英文显式证据且不限定 season 0。
- [x] timeout、response body、结果数、文本、cache 和 observation 均有上限。
- [x] 网络/HTTP/响应错误只暴露稳定 code 与 `retryable`。
- [x] 凭据不进入模型输入、领域对象、cache key、`repr` 或异常 cause。
- [x] SDK tool loop 使用 scripted model + fake TMDB 离线验证。
- [x] 官方 OpenAPI 示例投影 fixture 覆盖 TV/Movie search、TV details 与
  Season details 的真实字段形状和额外字段。
- [x] 独立 live smoke 只有在 `--live` 与凭据同时存在时才触网；凭据优先来自
  进程环境，否则 no-follow 读取仓库根固定 `.env` 的单一 key。pytest 不收集
  live check，不读取真实 `.env`；脚本不接受任意文件/URL 且不记录 TMDB 文本；
  adult capability 同时验证显式关闭、启用搜索和 metadata 标记。

## 安全边界

Agent 可以自主选择搜索词、候选调查顺序、语言和 season，但不能：

- 提供 URL、host、path、HTTP method 或凭据；
- 改变当前 run 从父目录配置继承的 `work_type`；
- 查询或选择当前 run 未观察到的 TMDB ID；
- 在选中剧集前查询 season；
- 通过 assistant 文本直接推进 phase；
- 获得任意 HTTP、shell、文件读取或文件写入能力。

HTTP adapter 的凭据必须由调用方显式传入。只有 opt-in live smoke 可以从固定
`.env` 加载 `TMDB_API_KEY` 后注入 adapter；library 和 Agent tool 不读取配置
文件。测试只用 fake provider、内存 transport 和 `tmp_path` 合成 dotenv，不读
仓库真实 `.env`，也不访问真实网络。

Movie candidate 已能搜索并返回类型信息，但 `select_series` 会以
`unsupported_work_type` fail closed。M3 不把 movie 伪装为 episode series；
后续必须先定义 movie identity、单视频 mapping、命名和 plan contract。

## 验证范围

- kernel：有界文本、language/year、重复 season/episode、zh-CN identity、
  OVA/OAD 分类；
- adapter：固定 TV/Movie search endpoint、anime genre filtering、显式 adult
  search、movie metadata、cache、series/season parsing、timeout、body 上限、
  HTTP 错误和凭据脱敏；
- contract fixture：记录官方 OpenAPI URL 与抓取日期，覆盖空 release date、
  season 顶层 ID、episode 的 season/episode number 和未使用的真实响应字段；
- tools/runtime：零/单/歧义结果、typed candidate capability、跨类型拒绝、phase
  policy、结构化失败；
- integration：真实 Agents SDK Runner 完成 search → inspect → select → season，
  并拒绝 extra URL 字段。

验证命令：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests scripts
git diff --check
```

可选的线上连通性与真实响应 smoke（不属于离线验收）：

```bash
PYTHONPATH=src .venv/bin/python scripts/tmdb_live_smoke.py --live
```

## 未提前实现

M4 的 inventory、字幕检测和 mapping submission 尚未实现；M5-M7 的 canonical
plan/hash、审批、Executor、rollback、持久化和生产 trace 也未提前开放。
