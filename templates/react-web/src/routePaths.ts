/** The shape of a route this helper reads (a react-router RouteObject has these fields). */
export interface RouteLike {
  path?: string;
  index?: boolean;
  children?: RouteLike[];
}

/**
 * Every page a person can open by URL alone: the static paths in the route tree, absolute and
 * in order. Paths with parameters (`:id`) or splats (`*`) are left out, since opening them needs
 * a real id. The nightly smoke run opens each one against the real API, so a new route is
 * covered the day it is added, without editing a list.
 */
export function staticPaths(routes: RouteLike[], parent = ''): string[] {
  const found: string[] = [];
  for (const route of routes) {
    const segment = route.path ?? '';
    if (segment.includes(':') || segment.includes('*')) continue;
    const full = segment.startsWith('/')
      ? segment
      : `${parent.replace(/\/$/, '')}/${segment}`.replace(/\/+/g, '/');
    if (route.path) found.push(full === '' ? '/' : full);
    if (route.children) found.push(...staticPaths(route.children, route.path ? full : parent));
  }
  return [...new Set(found)];
}
