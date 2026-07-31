import { useCallback, useEffect, useState } from "react";

import { IconMoon, IconSun } from "./components/Icon";

export type Theme = "light" | "dark";

export const THEME_STORAGE_KEY = "reeloom.theme";

const DARK_QUERY = "(prefers-color-scheme: dark)";

function systemTheme(): Theme {
  return window.matchMedia?.(DARK_QUERY).matches ? "dark" : "light";
}

/** A stored choice wins; without one the browser preference decides. */
export function readTheme(): Theme {
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
  return stored === "dark" || stored === "light" ? stored : systemTheme();
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(readTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // Follow the system only while the user has not picked a side themselves.
  useEffect(() => {
    const media = window.matchMedia?.(DARK_QUERY);
    if (!media?.addEventListener) return;
    const onChange = (event: MediaQueryListEvent) => {
      if (window.localStorage.getItem(THEME_STORAGE_KEY) === null) {
        setTheme(event.matches ? "dark" : "light");
      }
    };
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next = current === "dark" ? "light" : "dark";
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
      return next;
    });
  }, []);

  return { theme, toggle };
}

export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const dark = theme === "dark";
  return (
    <button
      type="button"
      className="ghost theme-toggle"
      onClick={toggle}
      aria-pressed={dark}
      aria-label={dark ? "切换到浅色主题" : "切换到深色主题"}
      title={dark ? "切换到浅色主题" : "切换到深色主题"}
    >
      {dark ? <IconMoon size={17} /> : <IconSun size={17} />}
    </button>
  );
}
