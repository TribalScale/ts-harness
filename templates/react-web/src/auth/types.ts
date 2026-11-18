export interface User {
  id: string;
  name: string;
  email?: string;
  /** Entra ID app roles (mapped from Azure AD groups). */
  roles: string[];
}

export interface Auth {
  user: User | null;
  /** True until the sign-in state is known. */
  loading: boolean;
  signIn(): Promise<void>;
  signOut(): Promise<void>;
  /** An access token for the API, refreshed as needed. */
  getAccessToken(): Promise<string | null>;
}
