import type { ReactNode } from 'react';
import { env } from '../config/env';
import { MsalAuthProvider } from './MsalAuthProvider';
import { TestAuthProvider } from './TestAuthProvider';

export function AuthProvider({ children }: { children: ReactNode }) {
  return env.VITE_AUTH_MODE === 'msal' ? (
    <MsalAuthProvider>{children}</MsalAuthProvider>
  ) : (
    <TestAuthProvider>{children}</TestAuthProvider>
  );
}
