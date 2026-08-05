# M7 Definition of Done

日期：2026-07-24

结论：M7 已完成。Reeloom 的领域状态和 SDK conversation 均可在进程重启后恢复；
固定离线 eval 与真实 OpenAI model 使用同一 Runner、工具、scenario 和评分器；
trace 与报告不复制不可信正文或凭据。真实网络检查保持显式 opt-in，核心测试
完全离线。

## 交付检查

- [x] 全部 runtime event 使用严格、版本化、canonical codec；未知 type、extra
  field、非规范编码和无效领域对象被拒绝。
- [x] `PlanBuilt` 解码不是反射反序列化：从 source identity 重建 snapshot，经
  mapping validator 和现有 compiler 重建 plan，再核对 canonical bytes/hash。
- [x] filesystem event/session store 绑定授权 root/run，记录先写入匿名 inode，
  `fsync` 后再以 no-replace link 原子发布；连续 sequence、digest chain、gap、
  tamper、symlink、stale writer 或非法 reducer transition 均 fail closed，
  append 前还会重验既有完整日志。
- [x] runtime、Organizer 与 approval resume 依赖 `EventStore` protocol；重启
  后恢复 exact `RunState`，不会重复 bootstrap event。
- [x] Agents SDK `Session` 与领域 store 分离；conversation 的 add/pop/clear
  都是 append-only record，不删除历史文件，也不能改变 phase 或批准状态。
- [x] scripted transcript 是 immutable、版本化 artifact，仍通过真实 Agents
  SDK Runner 和 tool loop 执行。
- [x] 固定 mapping-correction eval task 覆盖结构化失败、纠错、plan build、
  awaiting approval 和 unmapped partition；dataset 有稳定 hash。
- [x] mapping 成功以 TMDB ID、episode spans、字幕关联和 exact unmapped
  partition 的语义 ground truth 判定，不再只检查 plan 是否存在；scripted
  replay 额外严格检查固定调用与拒绝标签，live model 只按语义结果评分。
- [x] redacted trace 只输出 allowlist projection；文件名、标题、prompt、字幕
  正文、tool observation 和未知模型 token 不进入 trace。
- [x] 指标覆盖 mapping、validator first/final pass、tool/validator rejection、
  input/output/total tokens、calls、latency、显式价格成本估算、人工澄清率、
  unmapped 保留率，以及按 `kind + call_id + code` 标签计算的拒绝误报/漏报。
- [x] OpenAI adapter 显式使用 Responses-compatible HTTPS endpoint 和注入配置；
  base URL 拒绝凭据/query/fragment，并强制 response `store=False`、顺序 tool calls、
  token budget 与 SDK sensitive tracing disabled；忽略 caller 的
  body/header/query 扩展，并拒绝环境中的 `OPENAI_CUSTOM_HEADERS`。
- [x] run 的 budget 和绝对 UTC deadline 写入 `RunStarted`；重启只能继续消费
  原预算，不能重新获得 turn/token/time allowance。
- [x] approval resume 覆盖 issue、`PlanApproved`、`ApplyStarted`、journal 和
  one-time claim 之间的崩溃窗口；重启从持久事件与 executor artifact 幂等继续。
- [x] live eval 必须传 `--live`；key/base URL/model/reasoning 可由受限 loader
  从进程环境或仓库根固定 `.env` 补齐，model/reasoning 的显式 CLI 值优先；
  输出固定 dataset hash、model settings 与脱敏任务指标。

## 离线验证

- event codec：23 类 runtime event round-trip；unknown/extra/noncanonical；
  mapping 与 full `RenamePlan` 安全重建。
- checkpoint：重启 replay、tamper、sequence gap、record symlink、run mismatch、
  stale writer collision 与非法 transition 不落盘。
- session：add/pop/clear replay、tamper/gap、symlink、stale writer，以及 SDK
  Runner 历史持久化和 run ID binding。
- transcript/eval：immutable arguments、malformed arguments、strict dataset、
  stable dataset hash、完整 mapping-correction baseline。
- trace/metrics：未知模型 token 脱敏、敏感文本不出现、显式 token pricing。
- OpenAI：显式 HTTPS endpoint/client config、非法 URL/model/settings/key、
  live flag/model/key gate 与合成 dotenv；没有真实网络调用。
- 完整离线测试：`407 passed`。

## 真实网络验证边界

本里程碑没有在自动测试或本次实现中发出真实 OpenAI 请求。需要人工验证当前
model 行为时运行：

```bash
PYTHONPATH=src .venv/bin/python scripts/openai_live_smoke.py --live
```

线上结果可能随 model snapshot 和 provider 行为变化，因此报告必须连同
`dataset_hash + model + reasoning_effort + verbosity` 保存比较；价格也必须由
调用方显式提供，仓库不保存会过期的价格表。

## 保持不变的权限边界

M7 没有增加 Agent 工具，没有 MCP、shell、任意 URL、任意文件读取或 apply。
真实 model 只能调用既有八个 typed tools；Plan Compiler、approval、Executor 和
rollback 继续位于无 LLM 的确定性边界。
