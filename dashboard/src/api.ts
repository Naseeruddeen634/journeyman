/**
 * The API client.
 *
 * Every call has a timeout, because a dashboard that hangs forever on a slow backend teaches
 * people to stop trusting it, and an abort is easy to show in the UI. Errors are returned as
 * values rather than thrown, so a failed poll renders as "stale, last updated 2m ago" instead of
 * a blank screen.
 */

import type { Health, Suggestion } from "./health";

export type Result<T> = { ok: true; value: T } | { ok: false; error: string };

const TIMEOUT_MS = 8000;

export async function getJson<T>(url: string, init: RequestInit = {}): Promise<Result<T>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal });
    if (!response.ok) {
      return { ok: false, error: `${response.status} ${response.statusText}` };
    }
    return { ok: true, value: (await response.json()) as T };
  } catch (error) {
    const name = error instanceof Error ? error.name : "Error";
    return { ok: false, error: name === "AbortError" ? `no response in ${TIMEOUT_MS / 1000}s` : String(error) };
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  health: (tenant: string) => getJson<Health>(`/v1/health/${encodeURIComponent(tenant)}`),
  suggestions: (tenant: string) =>
    getJson<Suggestion[]>(`/v1/suggestions?tenant=${encodeURIComponent(tenant)}`),
  decide: (id: number, decision: "accepted" | "edited" | "rejected") =>
    getJson<{ ok: boolean }>(`/v1/suggestions/${id}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    }),
};
