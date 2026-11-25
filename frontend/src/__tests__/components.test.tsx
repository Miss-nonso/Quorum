import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { StatusBadge } from "@/components/status";
import { EmptyState, ErrorState } from "@/components/states";
import { EventFeed, Pagination } from "@/components/widgets";
import { ApiError } from "@/lib/api";

afterEach(cleanup);

describe("StatusBadge", () => {
  it("always pairs colour with an icon and a text label", () => {
    const { container } = render(<StatusBadge status="dead" />);
    expect(screen.getByText("Dead-lettered")).toBeTruthy();
    expect(container.querySelector("svg[aria-hidden='true']")).toBeTruthy();
  });

  it("uses worker vocabulary for workers", () => {
    render(<StatusBadge status="dead" kind="worker" />);
    expect(screen.getByText("Dead")).toBeTruthy();
    expect(screen.queryByText("Dead-lettered")).toBeNull();
  });

  it("falls back gracefully for unknown statuses", () => {
    render(<StatusBadge status="mystery" />);
    expect(screen.getByText("mystery")).toBeTruthy();
  });
});

describe("states", () => {
  it("explains an unreachable API and offers a retry", () => {
    let retried = false;
    render(
      <ErrorState error={new ApiError(0, "network_error", "x", null)} onRetry={() => (retried = true)} />,
    );
    expect(screen.getByRole("alert").textContent).toContain("Can't reach the API");
    screen.getByRole("button", { name: /try again/i }).click();
    expect(retried).toBe(true);
  });

  it("shows the request id so a bug report can be traced to the logs", () => {
    render(<ErrorState error={new ApiError(500, "internal_error", "Boom", "req-123")} />);
    expect(screen.getByText(/req-123/)).toBeTruthy();
  });

  it("renders meaningful empty states", () => {
    render(<EmptyState title="Nothing yet" description="Runs appear here." />);
    expect(screen.getByText("Nothing yet")).toBeTruthy();
  });
});

describe("EventFeed", () => {
  it("narrates events newest-first with machine-readable times", () => {
    render(
      <EventFeed
        events={[
          {
            id: 2,
            run_id: "r",
            task_id: "t",
            node_key: "fetch",
            type: "task_succeeded",
            payload: {},
            at: "2026-01-01T00:00:01Z",
          },
          {
            id: 1,
            run_id: "r",
            task_id: "t",
            node_key: "fetch",
            type: "task_claimed",
            payload: { worker_id: "w1234567890" },
            at: "2026-01-01T00:00:00Z",
          },
        ]}
        showRunLinks={false}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items[0].textContent).toContain("finished successfully");
    expect(items[1].textContent).toContain("worker w1234567 picked up");
    expect(items[0].querySelector("time")?.getAttribute("datetime")).toBe("2026-01-01T00:00:01Z");
  });
});

describe("Pagination", () => {
  it("disables impossible moves and labels its buttons", () => {
    render(<Pagination offset={0} limit={20} total={45} onChange={() => {}} />);
    expect((screen.getByRole("button", { name: "Previous page" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "Next page" }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText("1–20 of 45")).toBeTruthy();
    expect(screen.getByText("Page 1 of 3")).toBeTruthy();
  });
});
