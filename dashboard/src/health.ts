/**
 * The shape the service already emits (journeyman/support/service.py: health) and the rules for
 * turning those numbers into something a support lead can act on.
 *
 * The thresholds live next to the ones in ALERTS on the Python side. They are duplicated on
 * purpose: the dashboard must colour a tile even when the alerting pipeline is down, and a test
 * (tests/health.test.ts) pins the values so the two cannot drift apart silently.
 */

export interface Health {
  queue_depth: number;
  queue_age_seconds: number;
  dead_letters: number;
  model_calls: number;
  model_errors: number;
  validation_failure_rate: number;
  degraded_share: number;
  spent_cents: number;
  budget_used: number;
  agreement_rate: number | null;
  alerts: string[];
}

export interface Suggestion {
  id: number;
  case_id: string;
  payload: {
    queue: string | null;
    severity: number | null;
    summary: string;
    known_issue: string | null;
  };
  evidence: {
    similar_cases: { case_id: string; score: number; resolution: string }[];
    prompt_version: string;
  };
  model_id: string;
  degraded: boolean;
  confidence: number | null;
  cost_cents: number;
  human_decision: string | null;
}

export type Level = "ok" | "warn" | "bad";

export const THRESHOLDS = {
  queue_age_seconds: { warn: 120, bad: 600 },
  dead_letters: { warn: 1, bad: 1 },
  validation_failure_rate: { warn: 0.05, bad: 0.1 },
  degraded_share: { warn: 0.1, bad: 0.5 },
  budget_used: { warn: 0.8, bad: 1.0 },
} as const;

export type Metric = keyof typeof THRESHOLDS;

export function level(metric: Metric, value: number): Level {
  const t = THRESHOLDS[metric];
  if (value >= t.bad) return "bad";
  if (value >= t.warn) return "warn";
  return "ok";
}

/** Agreement is the one metric where lower is worse, so it gets its own rule. */
export function agreementLevel(rate: number | null): Level {
  if (rate === null) return "ok"; // nobody has decided yet; not a problem, just no signal
  if (rate < 0.6) return "bad";
  if (rate < 0.8) return "warn";
  return "ok";
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

export function formatCents(cents: number): string {
  return cents < 100 ? `${cents.toFixed(2)}c` : `$${(cents / 100).toFixed(2)}`;
}

export function formatPercent(fraction: number | null): string {
  return fraction === null ? "—" : `${Math.round(fraction * 100)}%`;
}

/**
 * What a lead should look at first. Sorted by how much it costs to ignore: work that is stuck,
 * then work that is silently getting worse, then money.
 */
export function attentionOrder(health: Health): Metric[] {
  const order: Metric[] = [
    "dead_letters",
    "queue_age_seconds",
    "validation_failure_rate",
    "degraded_share",
    "budget_used",
  ];
  const score = (m: Metric): number => ({ bad: 0, warn: 1, ok: 2 })[level(m, health[m])];
  return [...order].sort((a, b) => score(a) - score(b) || order.indexOf(a) - order.indexOf(b));
}
