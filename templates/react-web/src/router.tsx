import { createBrowserRouter, Navigate } from 'react-router';
import { AppLayout } from './components/AppLayout';
import { RouteError } from './components/RouteError';
import { NotesPage } from './features/notes/NotesPage';

/** Every page. Add one route per feature under the layout. */
export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/notes" replace /> },
      { path: 'notes', element: <NotesPage /> },
    ],
  },
]);
