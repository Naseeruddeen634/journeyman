import { describe, expect, it } from "vitest";

import {
  agreementLevel,
  attentionOrder,
  formatCents,
  formatDuration,
  formatPercent,
  level,
  THRESHOLDS,
  type Health,
} from "../src/health";

const healthy: Health = {
  queue_depth: 0,
  queue_age_seconds: 0,
  dead_letters: 0,
  model_calls: 100,
  model_errors: 0,
  validation_failure_rate: 0,
  degraded_share: 0,
  spent_cents: 12,
  budget_used: 0.1,
  agreement_rate: 0.9,
  alerts: [],
};

describe("thresholds", () => {
  // These mirror ALERTS in journeyman/support/service.py. If someone changes one side only,
  // this test is the thing that notices.
  it("match the service's paging thresholds", () => {
    expect(THRESHOLDS.queue_age_seconds.bad).toBe(600);
    expect(THRESHOLDS.dead_letters.bad).toBe(1);
    expect(THRESHOLDS.validation_failure_rate.bad).toBe(0.1);
    expect(THRESHOLDS.budget_used.bad).toBe(1.0);
  });

  it("grade a value into ok, warn and bad", () => {
    expect(level("queue_age_seconds", 30)).toBe("ok");
    expect(level("queue_age_seconds", 200)).toBe("warn");
    expect(level("queue_age_seconds", 900)).toBe("bad");
    expect(level("dead_letters", 0)).toBe("ok");
    expect(level("dead_letters", 1)).toBe("bad");
  });

  it("treats agreement rate the other way round, and 'no data' as not a problem", () => {
    expect(agreementLevel(null)).toBe("ok");
    expect(agreementLevel(0.95)).toBe("ok");
    expect(agreementLevel(0.7)).toBe("warn");
    expect(agreementLevel(0.4)).toBe("bad");
  });
});

describe("attentionOrder", () => {
  it("puts what is broken first", () => {
    const broken: Health = { ...healthy, dead_letters: 2, budget_used: 0.9 };
    expect(attentionOrder(broken)[0]).toBe("dead_letters");
  });

  it("keeps a stable order when everything is fine", () => {
    expect(attentionOrder(healthy)).toEqual([
      "dead_letters",
      "queue_age_seconds",
      "validation_failure_rate",
      "degraded_share",
      "budget_used",
    ]);
  });

  it("ranks a stuck queue above a spent budget", () => {
    const both: Health = { ...healthy, queue_age_seconds: 900, budget_used: 1.2 };
    const order = attentionOrder(both);
    expect(order.indexOf("queue_age_seconds")).toBeLessThan(order.indexOf("budget_used"));
  });
});

describe("formatting", () => {
  it("reads durations the way a person says them", () => {
    expect(formatDuration(45)).toBe("45s");
    expect(formatDuration(150)).toBe("2m 30s");
    expect(formatDuration(7200)).toBe("2.0h");
  });

  it("switches from cents to dollars where cents stop being readable", () => {
    expect(formatCents(12.345)).toBe("12.35c");
    expect(formatCents(250)).toBe("$2.50");
  });

  it("shows a dash rather than 0% when there is no data", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(0)).toBe("0%");
    expect(formatPercent(0.876)).toBe("88%");
  });
});
