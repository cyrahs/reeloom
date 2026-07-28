# M11 Requirement Matrix

| Increment | Status | Evidence |
| --- | --- | --- |
| Folder scanner | Complete | direct-child isolation, full-tree fingerprint, no-follow symlink and `.env*` blocking tests |
| Stable generations | Complete | per-folder settle state, config/restart identity, legacy rollout gate, concurrent DB constraints |
| Success disposition | Complete | immutable archive/remove-empty plan, exact approval, atomic no-replace rename, journal and recovery |
| Failure disposition | Complete | no-video and bounded deterministic Agent/domain failures; transient failures remain in place |
| Drift and late content | Complete | pre-effect generation invalidation, stable late-content replan, old combination rejection |
| API and UI | Complete | source-folder read model, exact dual-hash apply, disposition action/recovery, plain-text UI |
| Compatibility | Complete | unchanged Episode/Movie loaders and full M0-M10 offline regression |

Production PostgreSQL and browser acceptance use the existing explicit
PostgreSQL/E2E test gates.
