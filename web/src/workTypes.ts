export const workTypes = ["anime", "tv", "movie"] as const;

export type WorkType = (typeof workTypes)[number];

export function workTypeLabel(value: WorkType): string {
  return {
    anime: "动画",
    tv: "电视剧",
    movie: "电影",
  }[value];
}
