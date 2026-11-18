import { useMemo, useState, type ReactNode } from 'react';
import { AuthContext } from './context';
import type { Auth, User } from './types';

/** Key acceptance tests set (page.addInitScript) to choose who is signed in. */
export const TEST_USER_KEY = 'test-auth-user';

function readUser(): User | null {
  const raw = window.localStorage.getItem(TEST_USER_KEY);
  if (raw === null) return { id: 'user-1', name: 'Test User', roles: ['User'] };
  return raw === 'signed-out' ? null : (JSON.parse(raw) as User);
}

/** VITE_AUTH_MODE=test only: a fake signed-in user, no Microsoft sign-in. */
export function TestAuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(readUser);
  const auth = useMemo<Auth>(
    () => ({
      user,
      loading: false,
      signIn: async () => setUser({ id: 'user-1', name: 'Test User', roles: ['User'] }),
      signOut: async () => setUser(null),
      getAccessToken: async () => (user ? `test-token-${user.id}` : null),
    }),
    [user],
  );
  return <AuthContext.Provider value={auth}>{children}</AuthContext.Provider>;
}
