/**
 * What the screen must do when the backend misbehaves, which is the only interesting case.
 */

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App, { SuggestionCard } from "../src/App";
import type { Health, Suggestion } from "../src/health";

const health: Health = {
  queue_depth: 3,
  queue_age_seconds: 900,
  dead_letters: 0,
  model_calls: 40,
  model_errors: 1,
  validation_failure_rate: 0.15,
  degraded_share: 0.2,
  spent_cents: 340,
  budget_used: 0.34,
  agreement_rate: 0.82,
  alerts: ["queue_age_seconds: the oldest case has been waiting more than 10 minutes"],
};

const suggestion: Suggestion = {
  id: 7,
  case_id: "c-101",
  payload: { queue: "identity", severity: 2, summary: "Redirect URI mismatch after the rename.", known_issue: "old-1" },
  evidence: {
    similar_cases: [{ case_id: "old-1", score: 0.5, resolution: "Added the new reply URL." }],
    prompt_version: "triage-v1",
  },
  model_id: "cheap-model",
  degraded: false,
  confidence: 0.9,
  cost_cents: 0.009,
  human_decision: null,
};

function mockFetch(responses: Record<string, unknown>, failHealth = false) {
  return vi.fn(async (url: string) => {
    if (failHealth && url.includes("/health/")) throw new Error("connection refused");
    const key = Object.keys(responses).find((k) => url.includes(k));
    const body = key === undefined ? "" : JSON.stringify(responses[key]);
    return {
      ok: key !== undefined,
      status: key ? 200 : 404,
      statusText: key ? "OK" : "Not Found",
      headers: { get: (header: string) => (header === "Content-Length" ? String(body.length) : null) },
      text: async () => body,
      json: async () => JSON.parse(body),
    } as unknown as Response;
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("SuggestionCard", () => {
  it("never shows a suggestion without the evidence behind it", () => {
    render(<SuggestionCard suggestion={suggestion} onDecide={() => {}} />);
    expect(screen.getByText(/Redirect URI mismatch/)).toBeDefined();
    expect(screen.getByText("old-1")).toBeDefined();
    expect(screen.getByText(/Added the new reply URL/)).toBeDefined();
    expect(screen.getByText(/triage-v1/)).toBeDefined();
  });

  it("marks a retrieval-only suggestion so nobody reads it as an AI triage", () => {
    const degraded: Suggestion = {
      ...suggestion,
      degraded: true,
      model_id: "retrieval-only",
      payload: { ...suggestion.payload, queue: null, severity: null },
    };
    render(<SuggestionCard suggestion={degraded} onDecide={() => {}} />);
    expect(screen.getByText("retrieval only")).toBeDefined();
  });

  it("offers a decision once, then shows what was decided", () => {
    const { rerender } = render(<SuggestionCard suggestion={suggestion} onDecide={() => {}} />);
    expect(screen.getByText("Accept")).toBeDefined();
    rerender(<SuggestionCard suggestion={{ ...suggestion, human_decision: "accepted" }} onDecide={() => {}} />);
    expect(screen.queryByText("Accept")).toBeNull();
    expect(screen.getByText(/decision recorded: accepted/)).toBeDefined();
  });
});

describe("App", () => {
  it("shows the worst metric first", async () => {
    vi.stubGlobal("fetch", mockFetch({ "/health/": health, "/suggestions": [suggestion] }));
    render(<App tenant="acme" pollMs={100000} />);
    await waitFor(() => expect(screen.getByText("15m 0s")).toBeDefined());
    const tiles = document.querySelectorAll(".tile-label");
    expect(tiles[0]?.textContent).toBe("oldest case waiting");
    expect(screen.getByText(/waiting more than 10 minutes/)).toBeDefined();
  });

  it("says the numbers are stale instead of going blank when the backend is down", async () => {
    vi.stubGlobal("fetch", mockFetch({ "/suggestions": [suggestion] }, true));
    render(<App tenant="acme" pollMs={100000} />);
    await waitFor(() => expect(screen.getByRole("status")).toBeDefined());
    expect(screen.getByRole("status").textContent).toContain("Showing the last good numbers");
    expect(screen.getByTestId("suggestion-c-101")).toBeDefined();
  });
});
