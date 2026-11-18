import { InteractionRequiredAuthError, type AccountInfo } from '@azure/msal-browser';
import { MsalProvider, useIsAuthenticated, useMsal } from '@azure/msal-react';
import { useMemo, type ReactNode } from 'react';
import { AuthContext } from './context';
import { msal, scopes } from './msal';
import type { Auth, User } from './types';

function toUser(account: AccountInfo): User {
  const claims = (account.idTokenClaims ?? {}) as { roles?: string[]; oid?: string };
  return {
    id: claims.oid ?? account.localAccountId,
    name: account.name ?? account.username,
    email: account.username,
    roles: claims.roles ?? [],
  };
}

function Bridge({ children }: { children: ReactNode }) {
  const { instance, accounts, inProgress } = useMsal();
  const signedIn = useIsAuthenticated();
  const account = accounts[0] ?? null;

  const auth = useMemo<Auth>(
    () => ({
      user: signedIn && account ? toUser(account) : null,
      loading: inProgress !== 'none',
      signIn: () => instance.loginRedirect({ scopes }),
      signOut: () => instance.logoutRedirect(),
      async getAccessToken() {
        if (!account) return null;
        try {
          return (await instance.acquireTokenSilent({ scopes, account })).accessToken;
        } catch (error) {
          if (error instanceof InteractionRequiredAuthError) {
            await instance.acquireTokenRedirect({ scopes, account });
          }
          throw error;
        }
      },
    }),
    [instance, account, signedIn, inProgress],
  );

  return <AuthContext.Provider value={auth}>{children}</AuthContext.Provider>;
}

/** Microsoft Entra ID sign-in (redirect flow). */
export function MsalAuthProvider({ children }: { children: ReactNode }) {
  if (!msal) throw new Error('MSAL is not configured');
  return (
    <MsalProvider instance={msal}>
      <Bridge>{children}</Bridge>
    </MsalProvider>
  );
}
