# M0 Definition of Done

审查日期：2026-07-23

结论：M0 领域契约与威胁模型已完成，可以进入 M1。

## 交付检查

| 要求 | 实现 | 结果 |
| --- | --- | --- |
| 项目模型与错误分类 | frozen dataclass、`ErrorCode`、`ErrorCategory` | 通过 |
| candidate strict schema | opaque ID、ordinal 上限、object/fields、snapshot uniqueness | 通过 |
| mapping strict schema | bounds、overlap、snapshot membership、subtitle ownership | 通过 |
| 完整命名契约 | root、Sxx、single/range、subtitle variant、无 episode title | 通过 |
| OVA/OAD 规则 | typed hint 跨 season，unknown 仅 S00 fallback | 通过 |
| immutable plan contract | 绑定 mapping/series、推导 candidate 分区、稳定顺序 | 通过 |
| destination collision | subtitle 安全消歧，其他 exact/casefold collision 拒绝 | 通过 |
| 威胁模型 | trust boundary、攻击面、当前/后续控制 | 通过 |
| 旧项目行为 fixture | 记录测试 provenance，不导入旧 runtime | 通过 |

## 负向测试检查

- extra keys、错误 JSON ID 类型、非 canonical ID 和重复 ID；
- season/episode 越界、range overlap 和伪造 snapshot ID；
- destination/episode title/path/instructions 注入；
- Unicode 路径字符、控制字符、保留设备名和非法扩展名；
- OVA/OAD 证据冲突、普通 season unknown fallback 和 catalog 越界；
- 重复 source、mapping/move 不一致、跨 series、candidate 遗漏和 destination
  collision；
- 非 object 顶层 schema 和超长 candidate ordinal；
- prompt-injection 风格 filename/title 只作为数据。

全部测试离线运行，不依赖 Agents SDK、模型、TMDB 网络或真实文件操作。

## 刻意留给后续里程碑

- M1：SDK Runner、tool loop、phase、events、预算和 scripted fake model。
- M2：scanner、source identity、symlink/no-follow、`.env*` 与授权根。
- M3/M4：TMDB adapter、工具 schema、真实 mapping feedback loop。
- M5：canonical RenamePlan、授权根绑定、canonical bytes 和 `plan_hash`。
- M6：审批、final preflight、journal、rename、rollback 和 nonce replay 防护。

因此 M0 的 `PlanDraft` 不能被批准或执行。
