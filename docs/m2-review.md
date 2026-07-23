# M2 Definition of Done

审查日期：2026-07-23

结论：M2 安全候选快照已完成，可以进入 M3。

## 交付检查

| 要求 | 实现 | 结果 |
| --- | --- | --- |
| 授权根 | absolute、existing directory、完整 ancestor no-symlink | 通过 |
| no-follow scanner | directory FD、`O_NOFOLLOW`、`follow_symlinks=False` | 通过 |
| immutable snapshot | frozen records、stable sort、deterministic snapshot ID | 通过 |
| run-scoped ID | 视频/字幕分别分配 `video:N` / `subtitle:N` | 通过 |
| internal capability table | ID 绑定 exact relative path，Agent 不可见 | 通过 |
| extension policy | scanner 与 naming 共用 video/subtitle 白名单 | 通过 |
| exclusion policy | `.env*`、symlink、unsupported、special file | 通过 |
| scan budgets | entries、candidates、depth、relative-path bytes | 通过 |
| paginated tool | strict kind/cursor/limit、page/display/body 上限 | 通过 |
| runtime binding | `CandidateSnapshotCreated` 写入 snapshot ID/count | 通过 |
| capability gate | 未绑定 snapshot event 时 candidate tool 不 dispatch | 通过 |
| offline Agent loop | fake model 经真实 SDK Runner 分页真实 tmp snapshot | 通过 |

## 负向测试检查

- relative root、absolute candidate、`..`、重复 separator、Windows absolute/path
  separator；
- root symlink、root ancestor symlink、file symlink、directory symlink；
- symlink 指向不存在的 `.env*` target 时也不解析 target；
- `.env*` root/component 在 filesystem lookup 前拒绝；
- unsupported 和多重临时 extension 不进入快照；
- duplicate scanned path、candidate/depth 上限和越界 cursor；
- extra tool fields、非法 kind/cursor/limit 和 observation source contract；
- prompt-injection filename 只是 bounded display data，控制字符被中和；
- Agent 工具没有 path、shell、URL 或目录遍历参数。

## 架构边界检查

- `kernel/scanner.py` 只构造 deterministic domain snapshot，不执行 I/O。
- `adapters/filesystem.py` 隔离 `scandir/open/stat`。
- `policy/path_policy.py` 负责授权、containment 与 symlink policy。
- SDK 类型仍只存在于 `agents/`；scanner、policy 和 kernel 不依赖 SDK。
- scanner 只读取目录项和有限 metadata，不读取媒体、字幕或 `.env*` 内容。
- snapshot display name 永远不作为实际 source path 使用。
- M2 没有新增任何写文件或业务网络 capability。

## 刻意留给后续里程碑

- M3：TMDB search/series/season tools、HTTP timeout/cache/body limits。
- M4：字幕有限采样、mapping feedback loop、token/time 总预算。
- M5：完整 source identity、canonical snapshot bytes 与 `plan_hash`。
- M6：执行前重新打开 source、TOCTOU preflight、审批、journal 与 rollback。
