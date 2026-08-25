import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});

// jsdom doesn't implement matchMedia -- src/hooks/useTheme.ts reads it
// on first render to pick a default theme.
if (!window.matchMedia) {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }) as unknown as MediaQueryList;
}

// jsdom doesn't implement ResizeObserver -- Recharts' ResponsiveContainer
// and React Flow both use it.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
if (!("ResizeObserver" in window)) {
  // @ts-expect-error -- test-environment polyfill, not a real ResizeObserver
  window.ResizeObserver = MockResizeObserver;
}
