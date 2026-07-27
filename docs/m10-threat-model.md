# M10 threat model

M0-M9 的 root、plan、approval、journal、Bearer、secret 和浏览器边界继续适用。

| Threat | Control |
| --- | --- |
| TV/Movie 同号 ID 混用 | capability 绑定 `(work_type, tmdb_id)`；Movie tools 只接受 Movie |
| 模型选择路径 | Movie mapping 只含 opaque candidate ID；kernel 独立生成路径 |
| 缺失年份或恶意标题 | TMDB details 校验年份；复用 Unicode、保留名和长度策略 |
| 多正片/花絮误移动 | strict mapping 只允许一个 video；未选候选进入 unmapped |
| 已有目录被接管 | compile、preflight 和原子 mkdir 都要求整个 Movie 根不存在 |
| plan family 混淆 | family-specific schema/policy、duplicate-key/canonical/hash 重建 |
| rehashed 任意 destination | verifier 从 Movie identity、源扩展名、字幕 variant 重建 |
| stale reapply | amendment 绑定 parent hash 与 transaction；apply 核对 completed head |
| 后来新增文件被带入 | reapply 只扫描并复验 durable completed-layout 文件集合 |

残余风险：rename 后旧 Movie 目录可能为空；因“永不删除”不变量，M10 不清理它。
