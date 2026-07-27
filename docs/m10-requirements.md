# M10 Requirement Matrix

| Increment | Status | Evidence |
| --- | --- | --- |
| M10.0 contract | Complete | ADR、strict family loaders、Episode 回归与 OpenAPI check |
| M10.1 domain | Complete | identity/year、single-video mapping、naming、unmapped tests |
| M10.2 plan/effect | Complete | Movie initial/amendment、two-level manifest、apply/reapply tests |
| M10.3 runtime/Agent | Complete | Movie phases/events/projection、专用 tools、scripted SDK journey |
| M10.4 initial UI loop | Complete | worker dispatch、movie enum/labels、automatic PostgreSQL journey |
| M10.5 interaction/reapply | Complete | exact completed layout、no-op、later-file isolation、head validation |
| M10.6 acceptance | Complete | offline、PostgreSQL、frontend、OpenAPI、three-browser E2E |

Movie 与 Episode 使用独立 schema family。统一 loader 先验证 duplicate key、
canonical bytes 和 hash，再按 family 严格重建语义结果；Movie destination 即使
重新计算 hash 也必须能从 identity、扩展名和字幕变体精确复现。
