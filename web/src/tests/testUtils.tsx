import type { ReactElement, ReactNode } from "react";
import { render } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

/** Fresh QueryClient per test -- no cache bleed between tests, and
 * retries disabled so a mocked rejection resolves to an error state
 * immediately instead of the default retry backoff. */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchInterval: false, staleTime: Infinity },
    },
  });
}

export function renderWithProviders(
  ui: ReactElement,
  {
    route = "/",
    path,
    queryClient = createTestQueryClient(),
  }: { route?: string; path?: string; queryClient?: QueryClient } = {},
) {
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[route]}>
          {/* `path` sets up a real react-router <Route> so useParams()
              resolves inside `ui`, e.g. for /applications/:key pages --
              omit it for routes with no params. */}
          {path ? <Routes><Route path={path} element={children} /></Routes> : children}
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  return { ...render(ui, { wrapper: Wrapper }), queryClient };
}
