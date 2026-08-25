import { useCallback, useEffect, useState } from "react";

type Theme = "light" | "dark";

const STORAGE_KEY = "argus-theme";

function systemPrefersDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function readInitialTheme(): Theme {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === "light" || stored === "dark") return stored;
  return systemPrefersDark() ? "dark" : "light";
}

function applyTheme(theme: Theme) {
  document.documentElement.classList.toggle("dark", theme === "dark");
}

/** Dark mode, supported from the start (per the milestone's own
 * design direction) -- defaults to the OS preference on first visit,
 * then remembers an explicit user choice in localStorage. Toggling
 * only ever flips the `.dark` class `src/index.css`'s
 * `@custom-variant dark` reads -- no inline style, no per-component
 * theme prop threading. */
export function useTheme(): { theme: Theme; toggleTheme: () => void } {
  const [theme, setTheme] = useState<Theme>(readInitialTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const toggleTheme = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === "dark" ? "light" : "dark";
      localStorage.setItem(STORAGE_KEY, next);
      return next;
    });
  }, []);

  return { theme, toggleTheme };
}
