# M4 Definition of Done

日期：2026-07-23

结论：M4 已完成。Organizer Agent 可以提出 episode/subtitle mapping，但只有
确定性 kernel 校验通过并产生 `MappingSubmitted` 领域事件，run 才能进入
`BUILD_PLAN`。本阶段没有加入文件写入、移动或计划编译副作用。

## 交付核对

- [x] `submit_mapping` 使用 strict、禁止 extra keys 的嵌套 schema。
- [x] mapping 只接受与当前 run 绑定的 candidate snapshot 中的 opaque ID，不
  接受路径；reducer 会再次核对 candidate ID、catalog 和 inventory。
- [x] `get_tmdb_season` 只将编号连续的 season 记录为 typed catalog event。
- [x] `get_existing_inventory` 要求显式 capability；空库存也必须显式提供，不
  将缺失配置解释为空。
- [x] episode 边界、multi-episode range、range overlap、重复视频、重复字幕、
  未知 ID、字幕归属和库存冲突由纯领域代码校验。
- [x] `detect_subtitle_variant` 只读取 snapshot 内字幕的至多 64 KiB 前缀，并返回
  `.chs/.cht/.chi`；字幕正文不会进入 Agent observation。
- [x] 文件采样逐级使用 no-follow directory FD，并在读取前复核 root、普通文件
  和扫描时 identity；扫描时的 64 KiB 前缀摘要会在读取后再次核对。
- [x] UTF-8、带 BOM UTF-16、GB18030 和 Big5 会比较有效解码结果，不使用
  “第一个未报错的编码”直接下结论。
- [x] validation issue 使用稳定 code、最多 8 个 context 字段和每段 160-byte
  上限，不包含路径或字幕正文。
- [x] validation 失败保持 `MAP_EPISODES`，Agent 可根据 observation 修正重提。
- [x] `MappingSubmitted` 是进入 `BUILD_PLAN` 的唯一 M4 成功转换。
- [x] 最大模型轮数、工具调用数、失败数、累计 token 和 wall-clock time 都是
  immutable run budget。
- [x] 时间预算在工具执行中触发时会取消并清除 pending call，run fail closed。
- [x] domain observation 绑定确切 `call_id` 且每次调用只能记录一次；吞掉
  cancellation 的 model 结果仍会因 deadline 过期被拒绝。

## Agent 与确定性代码的分工

Agent 负责从文件名、TMDB metadata、Specials hint 和库存中推断“哪个文件对应
哪一集”。它不能决定校验结果。`submit_mapping` 会重新构造 `MappingDraft`，
依次检查 candidate capability、episode catalog、字幕关联、已检测字幕变体和
现有库存。

失败 observation 只说明最小必要事实，例如：

```json
{"code":"episode_out_of_bounds","context":{"episode_count":2,"episode_end":3}}
```

普通 assistant 文本、工具成功字符串或模型宣称“已完成”都不能推进 phase。
只有 event reducer 接受 `MappingSubmitted` 才会得到 `BUILD_PLAN`。

## 验证范围

- kernel：普通集、multi-episode、S00、边界、overlap、重复/未知 ID、字幕归属、
  inventory conflict 和三种中文字幕后缀；
- adapter：字幕 bounded read、symlink/路径逃逸拒绝、扫描后同长度改写和 I/O
  错误 fail closed；
- tools/runtime：phase policy、catalog/inventory observation、脱敏 validation、
  foreign snapshot/ID 拒绝、失败后修正、token/time budget、精确 call binding
  和 pending-call cancellation；
- integration：真实 Agents SDK Runner + scripted model 完成错误 mapping →
  结构化反馈 → 修正 mapping → `BUILD_PLAN`，并包含 candidate listing 和字幕
  variant detection；
- TMDB/Specials：既有离线测试覆盖任意 season 中的 OVA/OAD hint，M4 可将
  season 0 的单集或多集范围提交为 mapping。

验证命令：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q src tests scripts
git diff --check
```

本次完整离线结果：`251 passed`。

## 未提前实现

M5 的 canonical `RenamePlan` compiler 和 `plan_hash` 尚未接入 runtime；M6-M7
的审批、Executor、journal、rollback、持久化和生产 trace 也未开放。M4 只产生
经过校验的 immutable mapping draft，不触碰媒体文件。
