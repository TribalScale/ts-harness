import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { RequireAuth } from '../auth/RequireAuth';
import { TestAuthProvider, TEST_USER_KEY } from '../auth/TestAuthProvider';
import { AppLayout } from './AppLayout';
import { RouteError } from './RouteError';

function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        element: <AppLayout />,
        errorElement: <RouteError />,
        children: [{ path: 'notes', element: <h1>Notes page</h1> }],
      },
    ],
    { initialEntries: [path] },
  );
  return render(
    <TestAuthProvider>
      <RouterProvider router={router} />
    </TestAuthProvider>,
  );
}

describe('AppLayout', () => {
  it('shows the signed-in user, the navigation and the page', () => {
    renderAt('/notes');
    expect(screen.getByText('Test User')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Notes' })).toHaveAttribute('href', '/notes');
    expect(screen.getByRole('heading', { name: 'Notes page' })).toBeInTheDocument();
  });

  it('asks to sign in after signing out', async () => {
    renderAt('/notes');
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(screen.getByRole('heading', { name: 'Sign in to continue' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Sign in with Microsoft' }));
    expect(screen.getByRole('heading', { name: 'Notes page' })).toBeInTheDocument();
  });

  it('keeps admin-only content from a user without the Admin role', () => {
    window.localStorage.setItem(
      TEST_USER_KEY,
      JSON.stringify({ id: 'u1', name: 'Plain User', roles: ['User'] }),
    );
    render(
      <TestAuthProvider>
        <RequireAuth roles={['Admin']}>
          <p>Admin settings</p>
        </RequireAuth>
      </TestAuthProvider>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('You do not have access to this page.');
    expect(screen.queryByText('Admin settings')).toBeNull();
  });

  it('shows a not-found page for an unknown path', () => {
    renderAt('/nowhere');
    expect(screen.getByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
  });
});
