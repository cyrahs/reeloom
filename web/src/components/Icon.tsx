import type { ReactNode } from "react";

/**
 * One drawing style for the whole console: a 24-unit grid, hairline strokes in
 * the surrounding text colour, no fills. Icons are decorative — every place
 * that uses one keeps its own visible text or aria-label — so they are hidden
 * from assistive technology.
 */
function Glyph({
  children,
  size = 16,
  className,
}: {
  children: ReactNode;
  size?: number;
  className?: string;
}) {
  return (
    <svg
      className={className ? `icon ${className}` : "icon"}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

type IconProps = { size?: number; className?: string };

/** Brand mark: a film reel, drawn heavier because it carries the logo. */
export function IconReel({ size = 26, className }: IconProps) {
  return (
    <svg
      className={className ? `icon ${className}` : "icon"}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      aria-hidden="true"
      focusable="false"
    >
      <circle cx="12" cy="12" r="8.6" />
      <circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none" />
      <circle cx="12" cy="7.4" r="1.5" fill="currentColor" stroke="none" />
      <circle cx="12" cy="16.6" r="1.5" fill="currentColor" stroke="none" />
      <circle cx="7.4" cy="12" r="1.5" fill="currentColor" stroke="none" />
      <circle cx="16.6" cy="12" r="1.5" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconSun(props: IconProps) {
  return (
    <Glyph {...props}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4" />
    </Glyph>
  );
}

export function IconMoon(props: IconProps) {
  return (
    <Glyph {...props}>
      <path d="M20 14.3A8.4 8.4 0 1 1 9.7 4a6.6 6.6 0 0 0 10.3 10.3Z" />
    </Glyph>
  );
}

export function IconHelp(props: IconProps) {
  return (
    <Glyph {...props}>
      <circle cx="12" cy="12" r="8.6" />
      <path d="M9.6 9.4a2.5 2.5 0 1 1 3.3 2.4c-.7.3-1 .9-1 1.6v.4" />
      <path d="M11.9 17.1h.01" />
    </Glyph>
  );
}

export function IconArrowLeft(props: IconProps) {
  return (
    <Glyph {...props}>
      <path d="M19.5 12H4.5M10.5 6 4.5 12l6 6" />
    </Glyph>
  );
}

/** Source path folds down into its destination path. */
export function IconMoveTo(props: IconProps) {
  return (
    <Glyph {...props}>
      <path d="M6 5v8.5a3 3 0 0 0 3 3h9" />
      <path d="M15 13.5l3.5 3-3.5 3" />
    </Glyph>
  );
}

export function IconFilm(props: IconProps) {
  return (
    <Glyph {...props}>
      <rect x="3.4" y="5" width="17.2" height="14" rx="2.2" />
      <path d="M10.2 9.2 15.4 12l-5.2 2.8Z" />
    </Glyph>
  );
}

export function IconSubtitle(props: IconProps) {
  return (
    <Glyph {...props}>
      <rect x="3.4" y="5" width="17.2" height="14" rx="2.2" />
      <path d="M7.2 14.6h4M13.2 14.6h3.6M7.2 10.4h9.6" />
    </Glyph>
  );
}

export function IconFolder(props: IconProps) {
  return (
    <Glyph {...props}>
      <path d="M3.6 7.6A1.6 1.6 0 0 1 5.2 6h3.4l1.9 2.3h8.3a1.6 1.6 0 0 1 1.6 1.6v8.5a1.6 1.6 0 0 1-1.6 1.6H5.2a1.6 1.6 0 0 1-1.6-1.6Z" />
    </Glyph>
  );
}

export function IconChevronRight(props: IconProps) {
  return (
    <Glyph {...props}>
      <path d="M9.5 5.5 16 12l-6.5 6.5" />
    </Glyph>
  );
}

export function IconCopy(props: IconProps) {
  return (
    <Glyph {...props}>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M15.4 6.6v-1A1.6 1.6 0 0 0 13.8 4H5.6A1.6 1.6 0 0 0 4 5.6v8.2a1.6 1.6 0 0 0 1.6 1.6h1" />
    </Glyph>
  );
}

export function IconCheck(props: IconProps) {
  return (
    <Glyph {...props}>
      <path d="M4.8 12.6 9.6 17.4 19.2 6.6" />
    </Glyph>
  );
}

export function IconTrash(props: IconProps) {
  return (
    <Glyph {...props}>
      <path d="M4.6 7h14.8" />
      <path d="M9.6 7V5.6A1.6 1.6 0 0 1 11.2 4h1.6a1.6 1.6 0 0 1 1.6 1.6V7" />
      <path d="M6.6 7l.8 11.5A1.6 1.6 0 0 0 9 20h6a1.6 1.6 0 0 0 1.6-1.5L17.4 7" />
    </Glyph>
  );
}

export function IconPlus(props: IconProps) {
  return (
    <Glyph {...props}>
      <path d="M12 5v14M5 12h14" />
    </Glyph>
  );
}
