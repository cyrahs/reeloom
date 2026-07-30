# M11 Requirement Matrix

| Increment | Status | Evidence |
| --- | --- | --- |
| Folder scanner | Complete | direct-child isolation, full-tree fingerprint, no-follow symlink and `.env*` blocking tests |
| Stable generations | Complete | per-folder settle state, config/restart identity, legacy rollout gate, concurrent DB constraints |
| Success disposition | Complete | immutable archive/remove-empty plan, exact approval, native no-replace or explicit FUSE degradation, journal and recovery |
| Failure disposition | Complete | no-video and bounded deterministic Agent/domain failures move directly to fail; other Agent/Provider failures preserve the source across three generation retries, then use `agent_retry_exhausted` |
| Missing source convergence | Complete | sustained absence of an unclaimed terminal generation becomes `blocked/source_folder_missing`; unresolved claims and recovery remain fail closed |
| Target reservation lifecycle | Complete | only the current active plan or an unsettled transaction reserves an archive/fail name; immutable detached history cannot create a phantom numeric suffix |
| Drift and late content | Complete | pre-effect generation invalidation, stable late-content replan, old combination rejection |
| API and UI | Complete | source-folder read model, exact dual-hash apply, disposition action/recovery, plain-text UI |
| Compatibility | Complete | unchanged Episode/Movie loaders and full M0-M10 offline regression |
| Atomic move compatibility | Complete | native no-replace first; Linux FUSE may use the reported checked-rename degradation, with post-syscall convergence, recovery and Admin capability probe |

Production PostgreSQL and browser acceptance use the existing explicit
PostgreSQL/E2E test gates.
