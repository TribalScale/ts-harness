import { staticPaths } from './routePaths';

describe('staticPaths', () => {
  it('lists static pages under a layout, skipping parameters and splats', () => {
    const routes = [
      {
        children: [
          { index: true },
          { path: 'notes' },
          { path: 'notes/:id' },
          { path: 'admin', children: [{ path: 'users' }, { path: ':userId' }] },
          { path: '*' },
        ],
      },
      { path: '/about' },
    ];
    expect(staticPaths(routes)).toEqual(['/notes', '/admin', '/admin/users', '/about']);
  });

  it('includes the app routes', async () => {
    const { routes } = await import('./router');
    expect(staticPaths(routes)).toContain('/notes');
  });
});
