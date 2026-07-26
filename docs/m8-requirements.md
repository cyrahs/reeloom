# M8 Requirement Matrix

| 增量 | 状态 | 主证明 |
| --- | --- | --- |
| M8.0 PostgreSQL foundation | 已验证 | empty/repeat/concurrent migration、checksum、health、process/advisory single-instance lock |
| M8.1 config/secret/provider | 已验证 | config CAS/history、no-follow write-only secret、origin pinning、total response/deadline bounds |
| M8.2 watcher/scheduler | 已验证 | no-follow scan、stale fence、10,000 unchanged soak、8-way registration、boot reconcile |
| M8.3 runtime/Agent/plan | 已验证 | real SDK Runner、full-state projection restart、persisted budget/session/AgentDefinition、content-addressed plan |
| M8.4 API/SSE | 已验证 | auth/role/Host/Origin/body gates、indexed async-offloaded reads、cursor/SSE、safe projections、typed idempotency |
| M8.5 question/revision | 已验证 | domain-read-only question、fresh mapping revision、single-transaction finalize、same-run PG competition |
| M8.6 approval/apply/recovery | 已验证 | reusable/replacement approval、single claim、startup effect reconcile、exact typed recovery、terminal journal summary |
| M8.7 completed/reapply | 已验证 | archived identity revalidation、no-op/supersede projection、amendment success and destination-race contract |
| M8.8 composition | 已验证 | single production builder/background owner、database-fatal health, manual/automatic PG journeys、offline + PG17 gates |
