# ADR 0001：PostgreSQL-first control plane

状态：Accepted

M8 起，PostgreSQL 16–18 是所有 control-plane metadata 的唯一事实来源。媒体、
write-only Secret、content-addressed Plan 和 Executor Journal 仍由文件系统各自
唯一拥有。不提供 backend toggle、filesystem fallback 或 dual-write。

Application 只依赖按 use case 命名的窄事务接口。数据库事务不得跨越扫描、TMDB、
模型调用、Secret/Plan 写入、journal fsync 或媒体移动。历史事实 append-only；
只有明确列出的 projection/head row 可以更新。

首版部署固定单进程、单 worker、单实例。进程通过 state root 上的 no-follow process
lock 和一条 pool 外 lifetime PostgreSQL advisory-lock connection 同时取得 authority；
任一锁、schema identity、database identity 或 commit outcome 不确定时 fail closed。

Migration 使用固定顺序与 SHA-256 checksum，受 transaction advisory lock 串行化。
运行时只接受显式 deployment DSN；测试只接受显式
`REELOOM_TEST_POSTGRES_DSN`，所有服务器路径都不得读取 `.env*`。
