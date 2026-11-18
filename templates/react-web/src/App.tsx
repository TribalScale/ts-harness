import { QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { RouterProvider } from 'react-router';
import { setTokenProvider } from './api/client';
import { AuthProvider } from './auth/AuthProvider';
import { useAuth } from './auth/context';
import { queryClient } from './queryClient';
import { router } from './router';

function TokenBridge({ children }: { children: ReactNode }) {
  const auth = useAuth();
  // Set during render, not in an effect: children's effects (their first queries) run first.
  setTokenProvider(auth.getAccessToken);
  return <>{children}</>;
}

export function App() {
  return (
    <AuthProvider>
      <TokenBridge>
        <QueryClientProvider client={queryClient}>
          <RouterProvider router={router} />
        </QueryClientProvider>
      </TokenBridge>
    </AuthProvider>
  );
}
