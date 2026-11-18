import { PublicClientApplication } from '@azure/msal-browser';
import { env } from '../config/env';

const settings = env.VITE_AUTH_MODE === 'msal' ? env : null;

/** The MSAL instance (null in test mode). */
export const msal = settings
  ? new PublicClientApplication({
      auth: {
        clientId: settings.VITE_AUTH_CLIENT_ID,
        authority: `https://login.microsoftonline.com/${settings.VITE_AUTH_TENANT_ID}`,
        redirectUri: window.location.origin,
      },
      cache: { cacheLocation: 'sessionStorage' },
    })
  : null;

/** The API scope requested with every token. */
export const scopes = settings ? [settings.VITE_API_SCOPE] : [];

export async function initMsal() {
  if (!msal) return;
  await msal.initialize();
  const result = await msal.handleRedirectPromise();
  const account = result?.account ?? msal.getAllAccounts()[0];
  if (account) msal.setActiveAccount(account);
}
