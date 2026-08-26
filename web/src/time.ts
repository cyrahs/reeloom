// Time display helpers. The pages re-render on every poll (4s), so the
// relative wording stays fresh without its own timer.

const pad = (value: number) => String(value).padStart(2, "0");

export function formatClock(iso: string): string {
  const date = new Date(iso);
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** "MM-DD HH:mm", with the year prepended once it differs from today's. */
export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const year =
    date.getFullYear() === new Date().getFullYear()
      ? ""
      : `${date.getFullYear()}-`;
  return `${year}${day} ${clock}`;
}

/** Relative wording for fresh times, absolute beyond a day. */
export function formatWhen(iso: string): string {
  const elapsedMs = Date.now() - new Date(iso).getTime();
  if (elapsedMs < 60_000) return "刚刚";
  if (elapsedMs < 3_600_000) return `${Math.floor(elapsedMs / 60_000)} 分钟前`;
  if (elapsedMs < 86_400_000)
    return `${Math.floor(elapsedMs / 3_600_000)} 小时前`;
  return formatDateTime(iso);
}

/** Log timestamps: time of day, dated once the entry is not from today. */
export function formatLogTs(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  const sameDay =
    date.getFullYear() === today.getFullYear() &&
    date.getMonth() === today.getMonth() &&
    date.getDate() === today.getDate();
  const clock = formatClock(iso);
  return sameDay
    ? clock
    : `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${clock}`;
}
