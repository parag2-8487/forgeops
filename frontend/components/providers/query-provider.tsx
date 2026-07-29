"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { ApiProblemError } from "@/lib/api";

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: (failureCount, error) => {
          // 4xx are the caller's fault: retrying is wasted work and hides bugs.
          if (
            error instanceof ApiProblemError &&
            error.problem.status >= 400 &&
            error.problem.status < 500
          ) {
            return false;
          }
          // Retry up to 2 times for 5xx/transport errors.
          return failureCount < 2;
        },
      },
    },
  });
}

export function QueryProvider({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(makeQueryClient);

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

// Export for testing
export { makeQueryClient };
