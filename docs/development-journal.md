# Reeloom 开发学习日志

这份日志把每个实现切片对应到一个 Agent 概念。目标不只是“把功能写出来”，
还要能解释模型、工具、领域状态和副作用之间的边界。

## M0.1：先建立 Agent 不能绕过的领域契约

### 这一步实现了什么

- `CandidateId` 只接受 canonical 的 `video:N` 或 `subtitle:N`。
- candidate ordinal 使用有界正整数，超长 ID 只返回结构化领域错误。
- `Candidate` 的输入字段是封闭集合，额外的 `path` 等字段会被拒绝。
- 所有 `from_dict` 入口先验证顶层值确实是 object。
- `CandidateSnapshot` 由 frozen dataclass 和 tuple 组成，并拒绝重复 ID。
- 领域失败使用稳定的 `ErrorCode` 和 `ErrorCategory`，不要求调用方解析错误文本。

### 对应的 Agent 原理

Agent loop 可以概括为：

```text
模型选择动作
→ 工具把结构化输入交给确定性内核
→ 内核接受或返回结构化错误
→ 模型根据 observation 决定下一步
```

这里最重要的是：模型的“决定”不是权限。模型未来可以请求处理 `video:1`，
但它不能提交任意文件路径，也不能靠自然语言让非法状态变合法。

opaque ID 是 capability boundary 的第一部分。它隐藏真实路径，使模型只能引用
当前 run 已经授予的对象。M0.1 只定义 ID 的纯领域格式；ID 与具体 run 和扫描
快照的绑定会在 M2 完成。

文件名、TMDB 文本和工具 observation 都必须视为不可信数据。测试中特意使用
`[ignore previous instructions].mkv` 作为展示名：内核原样保存它，但从不解释
或执行它。

### 为什么还没有接 Agents SDK

如果先接模型再补约束，很容易把 prompt 当成安全边界。M0 先用纯函数和不可变
模型定义“无论模型说什么都成立”的规则。M1 才会把这些规则放进真实的 SDK
tool loop，并用 scripted fake model 离线观察工具调用、事件和停止条件。

### 如何验证

```bash
.venv/bin/python -m pytest -q
```

测试既覆盖正常创建，也覆盖非法 ID、extra keys、ID/kind 不一致和重复 ID。

## M0.2：模型提出 mapping，内核决定它是否成立

### 这一步实现了什么

- `EpisodeSpan` 表示一个视频对应的单集或连续多集范围。
- `EpisodeCatalog` 提供与 TMDB adapter 无关的季集边界。
- `MappingDraft` 严格拒绝 extra keys、未知 candidate ID 和错误 ID kind。
- 同一集不能被两个视频范围占用，同一视频和字幕也不能重复映射。
- 字幕只能指向当前 draft 中已经映射的视频。

### 对应的 Agent 原理

未来的 `submit_mapping` 工具不会把模型输出直接写入 run state。数据流是：

```text
LLM 生成 tool arguments
→ SDK 做工具参数解析
→ kernel 校验 snapshot、集数和关联关系
→ 成功：返回不可变 MappingDraft
→ 失败：返回有限、结构化的 DomainError observation
→ LLM 根据错误码修正后重试
```

这是 Agent 系统中常见的 validator feedback loop。LLM 擅长根据文件名和元数据
推断语义，但它不适合负责“是否越界”“是否重叠”这类必须始终一致的问题。
这些规则由确定性内核处理，测试可以穷举边界，而且不需要调用模型。

`EpisodeCatalog` 不包含 TMDB SDK 类型。以后 TMDB adapter 只负责把外部数据转成
这个领域模型，kernel 不知道数据来自网络、fixture 还是真实 API。这叫
dependency inversion：高价值的领域规则不依赖易变化的外部系统。

snapshot membership 校验则补全了 capability boundary：仅仅长得像 `video:99`
还不够，它必须确实存在于当前 run 的 candidate snapshot。字幕关联校验确保
Agent 不能借一个未映射视频把额外文件悄悄带入后续计划。

### 如何验证

```bash
.venv/bin/python -m pytest -q tests/kernel/test_mapping.py
```

测试覆盖单集、多集、不可变性、extra keys、伪造 ID、错误 kind、季集越界、
range overlap、重复映射和字幕归属。

## M0.3：destination 是编译结果，不是 Agent 建议

### 这一步实现了什么

- 剧集根目录固定为 `{series_zh_cn} ({year}) {tmdb-{tmdb_id}}`。
- 季目录固定为 `Sxx`。
- 视频固定使用 `动画名 SxxExx.ext` 或 `动画名 SxxExx-Eyy.ext`。
- 字幕复用视频 base，并加入 `.chs/.cht/.chi`。
- 标题经过 Unicode normalization、控制字符与路径字符清洗、UTF-8 长度限制和
  Windows 保留设备名处理。
- 视频和字幕扩展名分别使用有限白名单并 canonicalize 为小写。

### 对应的 Agent 原理

安全能力最好通过接口结构限制，而不是通过 prompt 请求模型自律。命名函数的
输入只有：

```text
SeriesIdentity + EpisodeSpan + file extension (+ subtitle variant)
```

它没有 `destination` 参数，也没有 `episode_title` 参数。因此模型即使生成了
诸如 `../../target` 的文本，也没有位置可以把它传入命名编译器。这叫
structural safety：非法能力在类型和 API 层面不可表达。

命名代码也不是 Agent tool。它没有需要模型判断的语义，只是一段确定性的
编译过程：

```text
validated domain objects
→ canonical path components
→ PurePosixPath relative destination
```

相同输入永远得到相同输出，且不访问文件系统。后续 `Plan Compiler` 会调用它，
Agent 只能查看最终 preview。

路径文本清洗与路径 containment 是两层不同防线。M0.3 只确保不可信标题能安全
进入单个路径组件；后续 path policy 和 Executor 仍必须验证目标位于授权根目录
内，并抵抗 symlink 与 TOCTOU。不能因为“名字已经清洗”就跳过 containment。

### 如何验证

```bash
.venv/bin/python -m pytest -q tests/kernel/test_naming.py
```

测试覆盖单集、多集、`S00`、三种字幕后缀、恶意 Unicode/路径文本、保留设备名、
长 UTF-8 标题、非法扩展名，以及拒绝 destination 和 episode title。

## M0.4：语义 hint 可以影响判断，不能扩大权限

### 这一步实现了什么

- local special 只有 `video_id` 和 `ova/oad/unknown` hint；稳定顺序来自 scanner
  分配的 candidate ID ordinal，Agent 不能提交 order。
- TMDB episode 使用 `season_number + episode_number` 和同一组有限 hint。
- OVA/OAD hint 可跨 season 优先匹配，未知剩余项只在 S00 按稳定顺序回退。
- hint 证据不足时 fail closed，不静默映射到其他类型。
- resolver 直接产出已有的 `VideoMapping`，不再维护重复的 special assignment
  类型。
- 所有目标经过 `EpisodeCatalog` 校验；多余本地视频和 TMDB episode 分别显式
  进入 unmapped 和 unused。
- 仓库新增威胁模型，区分 M0 已实现控制和后续里程碑待实现控制。

### 对应的 Agent 原理

Agent 系统经常同时包含“软判断”和“硬约束”。OVA/OAD 分类是软判断：模型可以
根据文件名和 TMDB 文本提出 hint。以下规则则是硬约束：

```text
hint 只能是有限枚举
→ ID 必须属于当前 snapshot
→ 同 kind 证据优先
→ 证据冲突时 fail closed
→ 未匹配资源保持显式可见
```

这体现了 semantic authority 与 operational authority 的分离。模型可以影响
“这更像 OVA 还是 OAD”的语义判断，但不能借此选择路径、读取新文件、访问新
URL 或执行移动。

resolver 也不读取原始 title 或 filename。上游将不可信文本转为有限结构后，
resolver 只对结构做确定性运算。这样既保留 Agent 的语义能力，也缩小 prompt
injection 能传播到的范围。

一个重要的非对称规则是：明确的 OVA/OAD hint 可以指向普通 season，因为 TMDB
数据确实可能这样归档；`unknown` fallback 却只能使用 S00。否则“没有证据”会
变成把本地文件塞进常规季的权限，风险远高于保留 unmapped。

显式 `unmapped` 和 `unused` 很重要。安全系统不应通过丢弃数据来假装成功；
无法证明的映射必须保留下来，供 Agent 获取更多证据或用户审查。

完整安全边界和后续控制见 [威胁模型](threat-model.md)。

### 如何验证

```bash
.venv/bin/python -m pytest -q tests/kernel/test_specials.py
```

测试覆盖跨 season hint、S00-only fallback、混合匹配、unmapped/unused、证据
冲突、重复 ID/episode、catalog 越界、拒绝 Agent order、snapshot membership、
错误 kind 和注入式额外文本。

## M0.5：Plan 是推理与副作用之间的协议

### 这一步实现了什么

- `PlannedMove` 没有公开 destination 构造入口，只能通过视频或字幕命名工厂创建。
- `PlanDraft` 是 frozen、带 schema/policy version，并绑定单一 `SeriesIdentity`
  与 validated `MappingDraft` 的纯领域快照。
- 每个 move 的 source、关联 video、span 和 series 必须与 mapping 一致；
  unmapped 由 `snapshot - mapping` 自动推导。
- 重复 source、伪造 ID、状态冲突、遗漏和 destination collision 都会失败。
- destination collision 使用 Unicode normalization + casefold，覆盖跨平台风险。
- 同一集的多个同语言同扩展名字幕按稳定 source ID 生成 `.chs.ass`、
  `.chs.1.ass` 等名称；其他碰撞不自动解决。
- fixture 记录了对应旧 `aninamer` plan 测试的 provenance，但不导入其 runtime。

### 对应的 Agent 原理

Agent 的工作应该结束在一个可审查协议，而不是直接触发副作用：

```text
LLM semantic mapping
→ deterministic validation
→ immutable PlanDraft
→ 后续 canonical RenamePlan + hash
→ independent approval
→ isolated executor
```

`PlanDraft` 的完整分区很重要。如果系统只记录 moves，消失的文件很难区分是
“有意不处理”还是“代码漏掉了”。它不接受调用方提供 unmapped 清单，而是从
validated mapping 与 snapshot 的差集推导；同时要求每个 mapped candidate 都有
且只有一个一致的 move，因此每个 candidate 的状态都能被审计。

字幕消歧展示了“可证明的自动修复”和“危险猜测”的区别。两个 subtitle source
拥有完全相同的 canonical destination 时，可以按 scanner ID 稳定编号；视频
碰撞或仅大小写不同的目标没有安全、业务等价的修复，因此继续 fail closed。

当前对象刻意叫 `PlanDraft`。它还没有绑定授权根、source identity、canonical
bytes 或 hash，不能被批准和执行。M5 才会把它编译成真正的 `RenamePlan`。

### Golden fixture provenance

`tests/fixtures/m0_plan_cases.json` 的行为来自旧项目以下测试：

- `test_build_rename_plan_basic_and_ignores_unmapped`
- `test_build_rename_plan_disambiguates_subtitles`
- `test_build_rename_plan_includes_s00_for_ova`

fixture 只复制用户可观察规则，不导入旧项目代码或绝对路径。

### 如何验证

```bash
.venv/bin/python -m pytest -q tests/kernel/test_plan.py
```

完整 M0 验收见 [M0 Definition of Done](m0-review.md)。

### 下一步

M1 将第一次引入 Agents SDK：实现 `RunState`、phase、typed events、reducer、
tool policy、预算和 scripted fake model，并使用 SDK Runner 的真实 tool loop。
