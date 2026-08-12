# AGENTS.md — Reeloom

Reeloom 把监控目录里的下载内容识别、重命名并归入媒体库。模型只回答"哪个文件
是哪一集"，其余全部由确定性代码决定。

改动前先读 [docs/rebuild-plan.md](docs/rebuild-plan.md)：它记录了 V2 为什么
这样设计，以及 V1 里哪些机制被删掉、为什么删。

## 1. 安全不变量

1. 执行路径上永不直接删除文件；永不覆盖已存在的目标。目录只用 `rmdir`
   清理。受控例外仅两处：
   (a) discard 会删除 reeloom 自己下载的 `.acquired` 字幕暂存
   （可重新下载），保证 fail/ 里是与原始下载一致的内容；
   (b) 洗版：被替换或判定为重复的文件先以 rename 移入监控目录下的
   `.reeloom-trash/<run-id>/<来源>/` 回收区（不放媒体库内，Emby 会扫库；
   记入 executed_moves，保留期内可 revert 复原），再由 worker 的定期
   清理（`trash.purge_run_trash`，全代码库唯一硬删除点）在
   `trash_retention_days` 到期且 run 终态后删除；空目录只用 `rmdir`
   顺手清掉。洗版判定完全由确定性代码做出（`replace.py`）；模型从不决定
   删除。额外目录匹配时模型只见顶层文件夹名清单，返回序号或放弃，
   从不产生路径。
2. 模型没有路径输入通道。它提交 candidate ID 和集数；标题、年份来自 TMDB，
   目标路径由 `naming.py` 计算。
3. Agent 工具不得提供 shell、任意文件读写、任意 URL 或 apply 能力。
4. scanner 不跟随 symlink；执行前校验目标父目录仍在授权根内。
5. 出站网络仅限 TMDB、模型 provider、ACG.RIP、Telegram，且不接受自定义
   base URL、proxy、登录、验证码规避或入站 webhook。
6. filename、TMDB 文本、字幕文本、论坛标题都是不可信数据。
7. 任何代码都不读取 `.env*`；含 `.env*` 的文件夹直接拒绝扫描。
8. 执行必须 forward-only 且幂等：重跑一遍 plan 是 no-op。

## 2. 架构边界

```text
scanner.py / library.py    读文件系统：发现、快照、静置窗口、已有库文件夹与库存清单
naming.py / planner.py     纯函数：命名规则、映射校验、plan 编译
replace.py                 纯函数：洗版判定矩阵、决议、plan 增补
trash.py                   回收区路径与唯一的硬删除入口
subtitles.py               字幕语言判定（纯函数 + 一个文件读取入口）
agent/                     模型循环、工具、prompt
adapters/                  tmdb / llm / acgrip / telegram / archive / ffprobe
executor.py + rename.py    幂等移动、复原、放弃、回收区移动
server/                    api、worker、compare、composition、notify、subtitles
```

`naming.py` 与 `planner.py` 不做 I/O。`executor.py` 不认识模型、TMDB 或
plan 之外的任何东西。

## 3. 不要重新引入的东西

这些在 V1 里存在，是 bug 的主要来源，已被删除：

- event sourcing、reducer、state codec —— 状态就是 `run.state` 一列。
- journal / rollback / 定向 recovery —— 幂等重放取代它们。
- plan hash、内容寻址 plan store、审批 nonce/expiry/claim —— 默认自动执行。
- lease、instance lock、idempotency 层 —— 单进程单 worker。
- notification outbox / projector —— 直接发送，失败记日志。
- 剧集/电影/字幕三套平行 plan 体系 —— 统一 plan 模型。
- SSE —— UI 轮询。

新增防护前先确认对应的失败真实存在。拿不准就先问，不要先加保护。

## 4. 编码与测试

- Python 3.11+，`pathlib.Path`，完整类型标注，frozen dataclass 优先。
- library code 用 `logging`，不用 `print`。
- 自定义错误类型带稳定 `code` 和可操作 context。
- 依赖用 `uv sync` 安装（含 dev group 的 pytest）。
- `uv run pytest -q -m "not postgres"` 必须离线通过：模型、
  TMDB、ACG.RIP、Telegram 全部使用 fake/mock transport。
- 驱动真实外部二进制（7z、ffmpeg/ffprobe）的测试打 `binaries("<tool>")`
  marker：本地缺工具时跳过；CI 的 python-binaries job 设
  `REELOOM_TEST_REQUIRE_BINARIES=1`，缺工具会失败而不是静默跳过。
- 仓库层测试打 `postgres` marker，需要 `REELOOM_TEST_POSTGRES_DSN`；
  本地不强制，CI 的 Tests workflow 会在 service container 上跑。
- 文件行为用 `tmp_path`，并覆盖 symlink 逃逸、路径逃逸、目标已存在、
  中断重放和复原重放。
- 前端：`npm run lint && npm run typecheck && npm test && npm run build`。
