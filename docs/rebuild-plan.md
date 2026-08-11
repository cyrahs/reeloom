# Reeloom V2 重构计划

从零重写。旧代码只作为领域规则参考（命名规则、TMDB 交互、字幕分类、ACG.RIP
适配细节），完成后删除。不兼容旧数据库，新库从空 schema 开始。

## 0. 仓库策略

同仓库、单分支重写，不建新 repo：

- 从 `main` 切出 `v2` 分支，分支上第一步清空 `src/`、`tests/`、`web/src/`
  与旧里程碑文档（保留本文件），按新结构从零搭建；新旧代码不共存。
- 旧实现随时通过 `git show main:<path>` 或 main worktree 对照参考；
  golden 测试用例从 main 移植。
- 重写期间 `main` 保持可部署，线上旧版不受影响。
- M-G 收尾时 `v2` 合回 `main`，该合并即"删除旧代码"；历史永久可查。
- 保留 issue/PR 历史与 `ghcr.io/cyrahs/reeloom` 镜像仓库的连续性。

## 1. 为什么重写

V1 是 67k 行 Python + 6k 行前端，实现的核心功能只有一件事：

> 监控文件夹 → LLM+TMDB 识别 → 生成重命名计划 → 移动文件 → 通知。

复杂度主要来自五个与部署形态不匹配的设计（单机、单进程、单管理员）：

1. 三套平行 plan 体系（剧集 / 电影 / 字幕获取），各自带 amendment、
   forward execution、repository、executor 副本。
2. v1 journal/rollback 与 v2 forward-only 两代执行引擎并存。
3. 手写 event sourcing（event_codec 1.8k + reducer 1.5k + state_codec 0.9k 行）。
4. 手写 SQL 仓库层（仅三个最大的 repository 就 7k+ 行）。
5. 分布式级防护：lease 心跳、instance lock、idempotency 层、one-time
   approval nonce、digest 链、no-replace link 原子发布。

近期 bug（recovery dead ends、zero-effect recovery drift、blocked runs）
几乎全部产自这些机制本身，而不是文件操作。V2 的策略是：**用"从不删除、
从不覆盖、失败即停、可幂等重放"这一条底线替代全部精密恢复机制**。

## 2. 已确认的产品决策

| 决策 | 结论 |
| --- | --- |
| 附加功能 | 保留：ACG.RIP 字幕获取、Telegram 通知、电影支持、问答/计划修订 |
| 数据库 | PostgreSQL（仓库层大幅精简，纯状态表，无 event sourcing） |
| 执行模式 | **默认自动**：计划生成后立即执行，无审批窗口 |
| 文件入口 | CloudDrive 离线下载，文件分批出现 → 保留 120s 静置窗口（可配置） |
| 目标已存在 | 丢弃新文件（移入 fail 桶，通知中列出，不阻塞、不等人工） |
| 字幕获取 | 全自动：搜索 → 选 release → 下载解压 → 发布，搜不到不阻塞 |
| 人工介入 | 识别/映射失败时 run 进入 needs_attention，通过 UI 问答/修订/放弃 |
| 事后修订 | 已完成的 run 可基于移动前的文件结构与 Agent 交流得到新计划，reapply 先把已归档/移动的文件复原再应用新计划 |
| duplicate 落点 | 移入该 watch root 的 `fail/<原文件夹名>/`，保留相对结构 |
| 补季命名 | 沿用已有系列文件夹；老文件夹缺 `{tmdb-id}` 时重命名补上 |
| 鉴权 | 单一 Admin Bearer token |

## 3. 保留的不变量（V1 的正确部分）

这些是 V1 里真正有价值的设计，原样继承：

- 模型永远没有路径输入通道：Agent 用 run 内 opaque candidate ID 提交映射，
  目标路径由确定性代码根据 TMDB 结果和命名规则计算。
- 从不删除媒体文件，从不覆盖已有目标（rename 使用 `RENAME_NOREPLACE` /
  `RENAME_EXCL`，FUSE 明确不支持时降级为 check-then-rename）。
- scanner 不跟随 symlink；所有源和目标必须在配置的根目录内。
- `archive` / `fail` 桶、隐藏文件夹、根下散文件不参与发现。
- 网络出站仅限 TMDB、模型 provider、ACG.RIP、Telegram，不接受自定义
  base URL 之外的任意 URL。
- 测试离线，模型 / TMDB / ACG.RIP 使用 scripted fake。
- 命名产物不变：`Series (Year) {tmdb-123}/S01/Series S01E01.mkv`、
  `Movie (Year) {tmdb-456}/Movie (Year).mkv`、字幕 `.chs/.cht/.chi` 后缀。

## 4. 核心流程（唯一的一条流水线）

```text
scanner 发现稳定子文件夹（120s 无变化）
  → 创建 run，快照文件清单（candidate ID + 相对路径 + size）
  → Agent 识别：search_tmdb / get_details / get_inventory → submit_plan
  → 代码校验映射并编译 plan（moves + unmapped + identity），存入 run 行
  → 立即执行：逐项 mkdir + no-replace rename
      目标已存在 → 该源文件移入 fail 桶（记为 duplicate）
  → 残余文件与空壳文件夹移入 archive 桶
  → 若为番剧且视频缺中文字幕 → 字幕获取子任务（同一 run 内）：
      search ACG.RIP → 模型选 release → 下载解压 → 校验 → no-replace 发布
  → Telegram 发送结果摘要（含 duplicate 与未映射清单、字幕结果）

任何阶段识别失败 / 校验反复不过 → run 置 needs_attention → Telegram 通知
  → 用户在 UI 问答、给出修订意见让 Agent 重跑、或放弃（文件夹移入 fail）
```

### 事后修订与 reapply

已完成（done）的 run 支持事后修订，前提假设：归档后的文件不会被再次
手动移动。流程：

```text
用户在 run 页发起修订会话（Agent 看到的是移动前的文件快照 + 上一版计划
  + 用户意见）
  → Agent 产出新计划
  → reapply：按 run 记录的"实际执行移动清单"逆序复原——
      媒体库中的文件、fail 桶中的 duplicate、archive 桶中的残余、
      以及本 run 获取发布的字幕，全部 rename 回原始源文件夹
      （ACG.RIP 字幕原本不存在，复原后留在源文件夹中，作为新计划的
      普通字幕候选参与映射）
  → 按新计划正常走 executing → acquiring_subs → done
```

复原使用与正向执行完全相同的幂等语义：目标（原路径）已存在则跳过并记录，
源缺失则记入 missing 清单继续，永不覆盖。因此 reapply 中断后重跑同样安全。

### 补季与 tmdb-id 补齐

目标系列文件夹已存在时沿用现有文件夹，不重排旧内容。若现有文件夹缺
`{tmdb-id}` 后缀（旧命名），plan 中包含一个确定性的文件夹重命名操作，
把它改为规范名（补上 id）；新集随后写入该文件夹。若规范名文件夹与旧名
文件夹同时存在（罕见的双文件夹情形），不做合并：直接使用 `{tmdb-id}`
文件夹作为目标，旧文件夹原样保留并在通知中提示。

关键简化：**电影和剧集共用同一个 plan 模型**（moves 列表 + identity），
电影只是没有 season 结构的特例；**字幕获取不再是独立 plan family**，它是
run 流水线的一个阶段，复用同一条 never-overwrite 写入路径，没有独立审批
scope、successor、marker 体系。

## 5. 架构

```text
src/reeloom/
  config.py          # 环境变量 + DB 配置模型
  db.py              # asyncpg/psycopg 连接 + schema DDL + 迁移（单文件）
  models.py          # Run / Plan / Move / Identity 等 frozen dataclass
  scanner.py         # 发现 + 稳定性窗口
  naming.py          # 命名规则（纯函数）
  planner.py         # 映射校验 + plan 编译（纯函数）
  agent/
    loop.py          # 模型 tool-call 循环（简单顺序循环 + 轮数/超时上限）
    tools.py         # search_tmdb / get_details / get_inventory /
                     # submit_plan / search_sub / select_release
    prompts.py
  executor.py        # 幂等 forward-only 移动 + archive/fail 处置
  subtitles.py       # 字幕分类（chs/cht/chi）+ 获取流水线
  adapters/
    tmdb.py
    openai.py        # OpenAI-compatible client
    acgrip.py        # 搜索/帖子/附件 + 冷却
    telegram.py      # 直接发送 + 简单重试
    archive7z.py     # 7z/rar/zip 解压
  server/
    api.py           # FastAPI：runs / attention / interact / config / auth
    worker.py        # 单 worker：扫描 tick + run 推进
  web/               # React UI（3 页：Dashboard / Run / Settings）
```

目标规模：Python ~8–12k 行，前端 ~3k 行，测试 ~8k 行。

## 6. 数据模型（Postgres，纯状态表）

```sql
watch_config   -- inbound root, library root, media type, provider, telegram,
               -- stability_seconds, enabled
run            -- id, config_id, folder_name, state, snapshot JSONB,
               -- plan JSONB, executed_moves JSONB, result JSONB,
               -- error JSONB, created/updated
run_log        -- run_id, ts, level, message   （仅供 UI 展示的追加日志）
interaction    -- run_id, role, content, ts    （问答与修订意见）
```

- run 的当前状态就是 `run.state` 一列；历史只是日志，不参与状态恢复。
- `snapshot` 是移动前的文件清单（candidate ID + 相对路径 + size），事后
  修订时 Agent 看到的就是它。
- `executed_moves` 追加记录每一次实际完成的移动（含入 fail/archive 与
  字幕发布），是崩溃重放和 reapply 复原的唯一依据；每完成一个 move 更新
  一次该列。
- plan 是 run 行里的一个 JSONB 快照，不做内容寻址、不算 hash、无独立
  store；修订产生新计划时，旧计划作为结构化条目写入 `run_log`。
- 无 approval 表、无 operation ledger、无 outbox、无 scheduler 表。
- 密钥（provider key、TMDB key、Telegram token）存 `watch_config` /
  全局 settings 表，依赖 DB 与主机本身的访问控制。

## 7. 状态机（唯一的一个）

```text
pending → identifying → executing → acquiring_subs → done
              │              │            │            │ 用户发起修订
              └──────────────┴────────────┴→ needs_attention
                                               │ 用户修订 → identifying
                                               │ 用户放弃 → discarded（文件夹入 fail）

done → （修订会话得到新计划）→ reverting → executing → … → done
任何未捕获错误 → failed（可从 UI 重试，即重置为 pending 重扫）
```

崩溃恢复 = 幂等重放，没有 journal：

- 重启时 `identifying` 的 run 重置为 `pending`（识别无副作用，重跑即可）。
- 重启时 `executing` / `acquiring_subs` 的 run 直接重新执行 plan——每个
  move 的结果是确定的：源在 → 执行；源不在且目标在 → 已完成，跳过；
  源在且目标在 → duplicate 入 fail 桶；源和目标都不在 → 记入 result
  的 missing 清单，继续（通知中告知，不阻塞）。
- 重启时 `reverting` 的 run 按 `executed_moves` 逆序继续复原，规则同上
  （原路径已存在则跳过，源缺失则记录继续）。

## 8. Agent 设计

- 直接用 OpenAI-compatible client 写一个 ~150 行的顺序 tool-call 循环，
  不引入 Agents SDK：V2 不再需要 SDK 的 session 持久化和 model protocol
  重放体系，唯一需要的就是"循环调用工具直到 submit_plan 成功或超限"。
- 工具输入输出用 Pydantic 严格 schema；映射校验失败返回结构化错误码让
  模型自我纠正，连续 N 次失败或超时/超轮数 → needs_attention。
- 修订 = 把用户意见（和上一版计划）追加进对话上下文重跑识别阶段，Agent
  始终基于移动前的 `snapshot` 工作；问答 = 只读的一问一答，不改变 run
  状态。needs_attention 时修订直接重新识别；done 时修订先经 reverting
  复原再执行新计划。
- 保留离线 scripted model 测试模式（一个实现同接口的 fake client），但
  删除 V1 的 transcript/eval/trace 基础设施。

## 9. 明确删除的机制及理由

| 删除 | 理由 |
| --- | --- |
| event sourcing 全套（codec/reducer/replay/digest 链） | 状态就是一行，重放靠幂等执行器 |
| v1 journal/rollback | forward-only + never-overwrite 使 rollback 无意义 |
| 三套 plan family 与 amendment 体系 | 统一 plan 模型；修订 = 重新生成 |
| plan hash / 内容寻址 plan store | 无审批绑定需求，plan 是 run 的字段 |
| approval nonce/expiry/claim、lease、instance lock、idempotency 层 | 单进程单 worker，DB 行状态足够 |
| notification outbox/projector/intents | 直接发送 + 重试一次，失败记日志 |
| subtitle successor / marker / publication 独立体系 | 并入 run 流水线 |
| scheduler_repository / forward_operation_repository / queries 巨石 | 纯状态表 + 少量查询函数 |
| SSE 流 | 单用户 UI 用 5s 轮询，删除全部重连/缓冲要求 |
| eval harness、redacted trace、pricing | 需要时再加，不进 V2 初版 |

## 10. 里程碑

每步都保持可离线测试、可运行：

1. **M-A 骨架**：config、DB schema、models、scanner（含稳定窗口）、
   run 状态机推进（Agent 用 stub），单进程 worker 跑通 pending→done 空流程。
2. **M-B 内核**：naming + planner 纯函数与全部命名/校验/冲突测试
   （移植 V1 的领域测试用例作为 golden case）。
3. **M-C Agent**：TMDB adapter、tool loop、识别与映射，scripted model 离线
   测试 + 一个 `--live` smoke。
4. **M-D 执行器**：幂等 forward-only 移动、duplicate→fail、残余→archive、
   `executed_moves` 记录、逆序复原（reverting）、tmdb-id 文件夹补齐重命名、
   崩溃重放测试（tmp_path 模拟中断，正向与复原都测）。
5. **M-E API + UI**：FastAPI + 三页 React UI（轮询）、needs_attention
   问答/修订/放弃、done 后修订 + reapply。
6. **M-F 字幕获取**：ACG.RIP adapter（含冷却）、解压校验、自动发布，
   接入 run 流水线。
7. **M-G 通知 + 收尾**：Telegram 摘要、部署文档更新、compose 更新、
   **删除全部 V1 代码与失效文档**。

## 11. 已定的边界规则

1. **事后修订依赖的假设**：归档后的文件不会被手动移动。复原时若发现
   文件缺失或原路径被占用，不视为错误：跳过并记录，最终在通知与 UI 中
   列出，由用户自行处理。
2. **duplicate 落点**：`fail/<原文件夹名>/` 下保留相对结构。
3. **补季**：沿用已有系列文件夹；缺 `{tmdb-id}` 时由 plan 内确定性
   重命名操作补上；新旧双文件夹并存时用 `{tmdb-id}` 文件夹、旧的保留
   并提示，不自动合并。
4. **鉴权**：单一 Admin Bearer token + localStorage，无其余会话机制。
