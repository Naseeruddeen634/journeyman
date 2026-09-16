import { describe, expect, it, vi } from "vitest";

import { getJson } from "../src/api";

function reply(status: number, body: string, headers: Record<string, string> = {}) {
  return vi.fn(async () => ({
    ok: status < 400,
    status,
    statusText: status === 204 ? "No Content" : "OK",
    headers: { get: (k: string) => headers[k] ?? null },
    text: async () => body,
    json: async () => JSON.parse(body),
  }) as unknown as Response);
}

describe("getJson", () => {
  it("treats 204 with no body as a success, not a parse failure", async () => {
    vi.stubGlobal("fetch", reply(204, ""));
    // Recording a decision returns 204. Reading it as a failure rolled back a card the server
    // had already saved, which is worse than an error: the screen and the database disagreed.
    expect(await getJson("/v1/suggestions/1/decision", { method: "POST" })).toEqual({
      ok: true,
      value: undefined,
    });
  });

  it("reports a non-JSON 200 as an error instead of throwing", async () => {
    vi.stubGlobal("fetch", reply(200, "<html>gateway</html>"));
    const result = await getJson("/v1/health/acme");
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("not JSON");
  });

  it("passes the status through when the server says no", async () => {
    vi.stubGlobal("fetch", reply(409, '{"error":"already decided"}'));
    const result = await getJson("/v1/suggestions/1/decision", { method: "POST" });
    expect(result).toEqual({ ok: false, error: "409 OK" });
  });
});
