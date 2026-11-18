import { NavLink, Outlet } from 'react-router';
import { useAuth } from '../auth/context';
import { RequireAuth } from '../auth/RequireAuth';
import { Button } from './Button';

const nav = [{ to: '/notes', label: 'Notes' }];

/** App shell: top bar, navigation, the signed-in user. Pages render in <Outlet />. */
export function AppLayout() {
  const auth = useAuth();
  return (
    <RequireAuth>
      <div className="min-h-screen bg-gray-50 text-gray-900">
        <header className="flex items-center justify-between border-b border-gray-200 bg-white px-6 py-3">
          <nav aria-label="Main" className="flex items-center gap-6">
            <span className="font-semibold">App</span>
            {nav.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) => (isActive ? 'font-medium' : 'text-gray-500')}
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="flex items-center gap-4 text-sm">
            <span>{auth.user?.name}</span>
            <Button variant="secondary" onClick={() => void auth.signOut()}>
              Sign out
            </Button>
          </div>
        </header>
        <main className="mx-auto max-w-5xl p-6">
          <Outlet />
        </main>
      </div>
    </RequireAuth>
  );
}
