# M9 Requirement Matrix

| Increment | Status | Evidence |
| --- | --- | --- |
| M9.0 browser contract | Complete | ADR、strict response models、Bearer OpenAPI snapshot/check |
| M9.1 safe read models | Complete | session/run/lineage/hash-verified preview/SSE tests |
| M9.2 config/history | Complete | retain/replace、migration、Admin history PostgreSQL journey |
| M9.3 frontend foundation | Complete | React 19/Vite 8、Admin auth、Zod、Vitest |
| M9.4 read surfaces | Complete | dashboard/run/SSE/preview/timeline UI |
| M9.5 Admin config | Complete | first-run/edit/probe/CAS/retain UX、Admin-only no-follow Pod directory picker |
| M9.6 interactions | Complete | question/revision/reapply forms、16 KiB guard、refetch |
| M9.7 effects | Complete | exact manual approval、durable settlement、exact recovery UI |
| M9.8 production | Complete | Node 24 build、manifest server、3-browser E2E、full suites |

验收结果（2026-07-26）：

- Python offline：484 passed，1 个 macOS filesystem capability skip；
- PostgreSQL 17：20 passed；runner 为每次 suite 创建唯一 schema，避免跨运行
  config capability 污染；
- frontend：lint、typecheck、11 unit tests、production build 全部通过；
- Playwright 1.61.1：实际 Reeloom API/static server + PostgreSQL 17，
  Chromium、Firefox、WebKit 共 9 个 E2E project tests 通过。
