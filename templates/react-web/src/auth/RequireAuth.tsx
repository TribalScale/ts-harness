import type { ReactNode } from 'react';
import { Button } from '../components/Button';
import { hasRole, useAuth } from './context';

/** Shows its children only to a signed-in user (with one of `roles`, when given). */
export function RequireAuth({ children, roles }: { children: ReactNode; roles?: string[] }) {
  const auth = useAuth();
  if (auth.loading) return <p className="p-8 text-gray-500">Signing in…</p>;
  if (!auth.user) {
    return (
      <div className="mx-auto mt-24 max-w-sm space-y-4 text-center">
        <h1 className="text-xl font-semibold">Sign in to continue</h1>
        <Button onClick={() => void auth.signIn()}>Sign in with Microsoft</Button>
      </div>
    );
  }
  if (roles && !hasRole(auth, ...roles)) {
    return (
      <p role="alert" className="p-8">
        You do not have access to this page.
      </p>
    );
  }
  return <>{children}</>;
}
