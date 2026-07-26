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
      请求未完成：<code>{code}</code>
    </div>
  );
}
