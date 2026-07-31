import {
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

const EDGE_MARGIN = 12;

/**
 * Explanatory text that stays out of the way until asked for.
 * Hover, focus or tap the marker to reveal it; nothing here is required
 * to understand the primary action next to it.
 */
export function Hint({
  children,
  label = "说明",
  align = "center",
}: {
  children: ReactNode;
  label?: string;
  align?: "center" | "end";
}) {
  const [open, setOpen] = useState(false);
  const [shift, setShift] = useState(0);
  const bubble = useRef<HTMLSpanElement>(null);
  const id = useId();

  // The marker can sit anywhere on the line, so pull the opened bubble back
  // inside the viewport instead of letting it push the page sideways.
  useLayoutEffect(() => {
    if (!open) {
      setShift(0);
      return;
    }
    const element = bubble.current;
    if (!element) return;
    const box = element.getBoundingClientRect();
    const viewport = document.documentElement.clientWidth;
    const overflowRight = box.right - (viewport - EDGE_MARGIN);
    if (overflowRight > 0) {
      setShift(-Math.min(overflowRight, box.left - EDGE_MARGIN));
    } else if (box.left < EDGE_MARGIN) {
      setShift(EDGE_MARGIN - box.left);
    }
  }, [open]);

  return (
    <span
      className="hint"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        className="hint-button"
        aria-label={label}
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        onClick={() => setOpen((current) => !current)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
      >
        ?
      </button>
      {open ? (
        <span
          ref={bubble}
          className={align === "end" ? "hint-bubble end" : "hint-bubble"}
          style={shift ? { transform: `translateX(${shift}px)` } : undefined}
          role="tooltip"
          id={id}
        >
          {children}
        </span>
      ) : null}
    </span>
  );
}
