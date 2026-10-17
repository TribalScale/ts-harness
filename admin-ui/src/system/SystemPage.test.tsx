import { act, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage } from "../test/render";
import { SystemPage } from "./SystemPage";

function healthReport(status: "ok" | "degraded" | "unavailable" = "ok") {
  return {
    status,
    version: "2026.8.7",
    components: {
      provider: { status: "ok", detail: null },
      memory_api: { status: "ok", detail: null },
    },
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("SystemPage health", () => {
  it("renders all-ok backend health as healthy", async () => {
    mockFetch((url) => {
      expect(url).toBe("/healthz");
      return jsonResponse(healthReport());
    });

    renderPage(<SystemPage />);

    expect(await screen.findAllByText("Healthy")).toHaveLength(3);
    expect(screen.queryByText("Degraded")).not.toBeInTheDocument();
  });

  it("shows an unconfigured integration without marking the page degraded", async () => {
    mockFetch(() =>
      jsonResponse({
        status: "ok",
        version: "0.1.0",
        components: {
          provider: { status: "ok", detail: null },
          channels: {
            status: "not_configured",
            detail: "Set SLACK_BOT_TOKEN, DISCORD_WEBHOOK_URL, or TELEGRAM_BOT_TOKEN.",
          },
        },
      })
    );

    renderPage(<SystemPage />);

    expect(await screen.findByText("Not configured")).toBeInTheDocument();
    expect(screen.getByText(/SLACK_BOT_TOKEN/)).toBeInTheDocument();
    expect(screen.getAllByText("Healthy").length).toBeGreaterThan(0);
    expect(screen.queryByText("Degraded")).not.toBeInTheDocument();
  });

  it("renders mixed backend health with friendly names and details", async () => {
    mockFetch(() =>
      jsonResponse({
        ...healthReport("degraded"),
        components: {
          provider: { status: "ok", detail: null },
          memory_api: { status: "unavailable", detail: "connection refused" },
        },
      })
    );

    renderPage(<SystemPage />);

    expect(await screen.findByText("Memory API")).toBeInTheDocument();
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
    expect(screen.getByText("connection refused")).toBeInTheDocument();
  });

  it("refreshes health in the background every 30 seconds", async () => {
    vi.useFakeTimers();
    const fetchMock = mockFetch(() => jsonResponse(healthReport()));

    renderPage(<SystemPage />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getAllByText("Healthy")).toHaveLength(3);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("connects the system tabs to the rendered panel", async () => {
    mockFetch(() => jsonResponse(healthReport()));

    renderPage(<SystemPage />);

    await screen.findAllByText("Healthy");
    const tab = screen.getByRole("tab", { name: "Health" });
    const panel = screen.getByRole("tabpanel");
    expect(tab.getAttribute("aria-controls")).toBe(panel.id);
    expect(panel.getAttribute("aria-labelledby")).toBe(tab.id);
  });

  it("keeps stale health visible when a background refresh fails", async () => {
    vi.useFakeTimers();
    mockFetch((url) => {
      if (url === "/healthz") {
        const calls = vi.mocked(globalThis.fetch).mock.calls.length;
        return calls === 1
          ? jsonResponse(healthReport())
          : jsonResponse({ error: "temporary outage" }, 400);
      }
      return jsonResponse({});
    });

    renderPage(<SystemPage />);

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("Provider")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(screen.getByText("Health data may be stale")).toBeInTheDocument();
    expect(screen.getByText("Provider")).toBeInTheDocument();
  });
});

describe("SystemPage cron", () => {
  it("refreshes jobs and announces success after a manual trigger", async () => {
    const user = userEvent.setup();
    mockFetch((url, init) => {
      if (url === "/admin/cron/jobs" && !init?.method) {
        const calls = vi
          .mocked(globalThis.fetch)
          .mock.calls.filter(([input]) => String(input) === "/admin/cron/jobs").length;
        return jsonResponse({
          jobs: [
            {
              name: "daily-summary",
              agent_id: "ops-agent",
              cron: "0 10 * * 1",
              prompt: "Summarize work",
              skill_id: "ops",
              timezone: "UTC",
              next_run: calls === 1 ? "2026-08-08T10:00:00Z" : "2026-08-09T10:00:00Z",
            },
          ],
        });
      }
      if (url === "/admin/cron/jobs/daily-summary/trigger") {
        expect(init?.method).toBe("POST");
        return jsonResponse({ status: "queued" });
      }
      return jsonResponse({});
    });

    renderPage(<SystemPage section="cron" />);

    expect(await screen.findByText("2026-08-08T10:00:00Z")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Trigger" }));

    expect(await screen.findByText("Triggered daily-summary")).toBeInTheDocument();
    expect(screen.getByText("2026-08-09T10:00:00Z")).toBeInTheDocument();
  });
});
