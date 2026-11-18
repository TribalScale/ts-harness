import { createContext, useContext } from 'react';
import type { Auth } from './types';

export const AuthContext = createContext<Auth | null>(null);

export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error('useAuth must be used inside <AuthProvider>');
  return auth;
}

export function hasRole(auth: Auth, ...roles: string[]): boolean {
  return roles.some((role) => auth.user?.roles.includes(role));
}
