import { screen, waitFor } from "@testing-library/react";

// Astryx mounts a visually hidden live region with role="alert" (data-astryx-live-region) for
// announcements, so "the alert" on a page means the app's own alert, not that region.
function appAlerts(): HTMLElement[] {
  return screen.queryAllByRole("alert").filter((el) => !el.hasAttribute("data-astryx-live-region"));
}

export function getAlert(): HTMLElement {
  const alerts = appAlerts();
  if (alerts.length !== 1) {
    throw new Error(`expected one app alert, found ${alerts.length}`);
  }
  return alerts[0];
}

export function findAlert(): Promise<HTMLElement> {
  return waitFor(() => getAlert());
}
