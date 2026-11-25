import { createBrowserRouter, Navigate, type RouteObject } from 'react-router';
import { AppLayout } from './components/AppLayout';
import { RouteError } from './components/RouteError';
import { env } from './config/env';
import { NotesPage } from './features/notes/NotesPage';
import { staticPaths } from './routePaths';

/** Every page. Add one route per feature under the layout. */
export const routes: RouteObject[] = [
  {
    element: <AppLayout />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/notes" replace /> },
      { path: 'notes', element: <NotesPage /> },
    ],
  },
];

export const router = createBrowserRouter(routes);

declare global {
  interface Window {
    /** Test builds only: the pages the nightly smoke run opens (see routePaths.ts). */
    __APP_ROUTES__?: string[];
  }
}
if (env.VITE_AUTH_MODE === 'test') window.__APP_ROUTES__ = staticPaths(routes);
