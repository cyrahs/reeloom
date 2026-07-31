import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { THEME_STORAGE_KEY, ThemeToggle } from "../src/theme";

function stubSystemTheme(dark: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: dark,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

afterEach(() => {
  delete document.documentElement.dataset.theme;
});

test("follows the browser preference until the user picks a side", async () => {
  stubSystemTheme(true);

  render(<ThemeToggle />);

  expect(document.documentElement.dataset.theme).toBe("dark");
  expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();

  await userEvent.click(screen.getByRole("button", { name: "切换到浅色主题" }));

  expect(document.documentElement.dataset.theme).toBe("light");
  expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  expect(
    screen.getByRole("button", { name: "切换到深色主题" }),
  ).toHaveAttribute("aria-pressed", "false");
});

test("a stored choice survives a browser preferring the other palette", () => {
  stubSystemTheme(false);
  window.localStorage.setItem(THEME_STORAGE_KEY, "dark");

  render(<ThemeToggle />);

  expect(document.documentElement.dataset.theme).toBe("dark");
});
