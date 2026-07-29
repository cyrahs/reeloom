const labels: Record<string, string> = {
  active: "进行中",
  awaiting_approval: "等待审批",
  completed: "已完成",
  failed: "失败",
  rolled_back: "已回滚",
  stopped: "已停止",
};

export function Status({ value }: { value: string }) {
  const tone =
    value === "completed"
      ? "success"
      : value === "failed"
        ? "danger"
        : value === "awaiting_approval"
          ? "warning"
          : "neutral";
  return <span className={`status ${tone}`}>{labels[value] ?? value}</span>;
}

export function ShortHash({ value }: { value: string | null }) {
  if (value === null) return <span className="muted">尚无计划</span>;
  return (
    <code className="hash" title={value}>
      {value.slice(0, 15)}…{value.slice(-8)}
    </code>
  );
}

export function PageError({ code }: { code: string }) {
  return (
    <div className="notice danger" role="alert">
      {errorMessage(code)} <code>{code}</code>
    </div>
  );
}

export function errorMessage(code: string) {
  return {
    atomic_move_unsupported:
      "当前挂载不支持原子不覆盖移动；源内容保持原位。修复挂载后请使用原审批执行 exact recovery。",
    permission_denied:
      "目录不可写或挂载为只读；源内容保持原位。修复权限后请使用 exact recovery。",
    transient_io:
      "目录访问暂时失败；服务端没有自动重复移动，请稍后使用原事务恢复。",
    state_ambiguous:
      "移动结果无法安全确认；请保持源和目标不变并执行 exact recovery。",
    recovery_required:
      "请求结果尚未安全结算，只能使用服务端返回的 exact approval ID 恢复。",
  }[code] ?? "请求未完成：";
}
