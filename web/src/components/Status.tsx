import { statusLabel, statusTone, type StatusKind } from "../labels";

export function Status({
  value,
  kind = "run",
}: {
  value: string;
  kind?: StatusKind;
}) {
  return (
    <span className={`status ${statusTone(kind, value)}`}>
      {statusLabel(kind, value)}
    </span>
  );
}

export function ShortHash({
  value,
  label,
}: {
  value: string | null;
  label?: string;
}) {
  return (
    <span className="hash-row">
      {label ? <span className="hash-label">{label}</span> : null}
      {value === null ? (
        <span className="muted">尚无计划</span>
      ) : (
        <code className="hash" title={value}>
          {value.slice(0, 10)}…{value.slice(-6)}
        </code>
      )}
    </span>
  );
}

export function PageError({
  code,
  context = {},
}: {
  code: string;
  context?: Readonly<Record<string, string>>;
}) {
  const candidate = context.candidate_id;
  const sourcePath = context.source_relative_path;
  const sourceState = context.source_state;
  const destinationPath = context.destination_relative_path;
  const destinationState = context.destination_state;
  const hasRecoveryContext =
    candidate && sourcePath && sourceState && destinationPath && destinationState;
  return (
    <div className="notice danger" role="alert">
      {errorMessage(code)} <code>{code}</code>
      {hasRecoveryContext ? (
        <p>
          候选 <code>{candidate}</code>；源 <code>{sourcePath}</code> 状态
          <code>{sourceState}</code>；目标 <code>{destinationPath}</code> 状态
          <code>{destinationState}</code>。
        </p>
      ) : null}
    </div>
  );
}

export function errorMessage(code: string) {
  return {
    atomic_move_unsupported:
      "当前挂载不支持原子不覆盖移动；源内容保持原位。修复挂载后请使用原审批执行定向恢复。",
    permission_denied:
      "目录不可写或挂载为只读；源内容保持原位。修复权限后请使用原审批执行定向恢复。",
    transient_io:
      "目录访问暂时失败；服务端没有自动重复移动，请稍后使用原事务恢复。",
    state_ambiguous:
      "移动结果无法安全确认；请保持源和目标不变并执行定向恢复。",
    recovery_required:
      "请求结果尚未安全结算，只能使用服务端返回的指定审批 ID 恢复。",
    destination_collision:
      "目标位置已经存在不同内容；系统没有覆盖文件。当前执行已终结，可重新扫描或删除记录。",
    source_drift:
      "源文件夹内容已变化，原计划不再适用；系统没有继续执行。请结束此运行并让新扫描创建新运行。",
    interaction_budget_exhausted:
      "本次 Agent 操作超过时间上限，或此运行的累计模型预算已耗尽。单次超时可重试；累计预算只能为后续新运行提高。",
  }[code] ?? "请求未完成：";
}
