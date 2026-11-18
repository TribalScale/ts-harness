import { isRouteErrorResponse, Link, useRouteError } from 'react-router';

export function RouteError() {
  const error = useRouteError();
  const notFound = isRouteErrorResponse(error) && error.status === 404;
  return (
    <div className="mx-auto mt-24 max-w-md space-y-3 text-center">
      <h1 className="text-xl font-semibold">
        {notFound ? 'Page not found' : 'Something went wrong'}
      </h1>
      <Link to="/" className="underline">
        Go to the start page
      </Link>
    </div>
  );
}
