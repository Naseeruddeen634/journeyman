/**
 * The support lead's screen: is the queue moving, is the quality holding, what is it costing,
 * and what did the agent suggest.
 *
 * One rule runs through the UI: never show a suggestion without its evidence. A label with no
 * reasoning next to it gets accepted without being read, and then the agreement rate stops
 * meaning anything.
 */

import { useCallback, useEffect, useState } from "react";

import { api } from "./api";
import {
  agreementLevel,
  attentionOrder,
  formatCents,
  formatDuration,
  formatPercent,
  level,
  type Health,
  type Level,
  type Metric,
  type Suggestion,
} from "./health";

const LABELS: Record<Metric, string> = {
  dead_letters: "dead letters",
  queue_age_seconds: "oldest case waiting",
  validation_failure_rate: "answers rejected",
  degraded_share: "degraded (no model)",
  budget_used: "daily budget used",
};

function value(health: Health, metric: Metric): string {
  if (metric === "queue_age_seconds") return formatDuration(health.queue_age_seconds);
  if (metric === "dead_letters") return String(health.dead_letters);
  return formatPercent(health[metric]);
}

export function Tile({ label, text, tone }: { label: string; text: string; tone: Level }) {
  return (
    <div className={`tile tile-${tone}`} data-testid={`tile-${label}`}>
      <div className="tile-value">{text}</div>
      <div className="tile-label">{label}</div>
    </div>
  );
}

export function SuggestionCard({
  suggestion,
  onDecide,
}: {
  suggestion: Suggestion;
  onDecide: (id: number, decision: "accepted" | "edited" | "rejected") => void;
}) {
  const { payload, evidence } = suggestion;
  const decided = suggestion.human_decision !== null;
  return (
    <article className="card" data-testid={`suggestion-${suggestion.case_id}`}>
      <header>
        <span className="case">{suggestion.case_id}</span>
        {suggestion.degraded ? (
          <span className="badge badge-warn">retrieval only</span>
        ) : (
          <span className="badge">
            {payload.queue} · sev {payload.severity}
          </span>
        )}
        <span className="muted">
          {suggestion.model_id} · {evidence.prompt_version} · {formatCents(suggestion.cost_cents)}
        </span>
      </header>
      <p>{payload.summary}</p>
      {evidence.similar_cases.length > 0 && (
        <ul className="evidence">
          {evidence.similar_cases.map((n) => (
            <li key={n.case_id}>
              <b>{n.case_id}</b> <span className="muted">({n.score})</span> {n.resolution}
            </li>
          ))}
        </ul>
      )}
      {decided ? (
        <p className="muted">decision recorded: {suggestion.human_decision}</p>
      ) : (
        <div className="actions">
          <button onClick={() => onDecide(suggestion.id, "accepted")}>Accept</button>
          <button onClick={() => onDecide(suggestion.id, "edited")}>Edit</button>
          <button onClick={() => onDecide(suggestion.id, "rejected")}>Reject</button>
        </div>
      )}
    </article>
  );
}

export default function App({ tenant = "acme", pollMs = 15000 }: { tenant?: string; pollMs?: number }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [staleSince, setStaleSince] = useState<number | null>(null);
  const [error, setError] = useState<string>("");

  const refresh = useCallback(async () => {
    const [h, s] = await Promise.all([api.health(tenant), api.suggestions(tenant)]);
    if (h.ok) {
      setHealth(h.value);
      setStaleSince(null);
      setError("");
    } else {
      // Keep the last good numbers on screen and say they are stale. A blank dashboard during a
      // backend blip is how people learn to ignore the dashboard.
      setError(h.error);
      setStaleSince((previous) => previous ?? Date.now());
    }
    if (s.ok) setSuggestions(s.value);
  }, [tenant]);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), pollMs);
    return () => clearInterval(timer);
  }, [refresh, pollMs]);

  const decide = async (id: number, decision: "accepted" | "edited" | "rejected") => {
    setSuggestions((current) =>
      current.map((s) => (s.id === id ? { ...s, human_decision: decision } : s)),
    );
    const result = await api.decide(id, decision);
    if (!result.ok) {
      setError(`could not record the decision: ${result.error}`);
      setSuggestions((current) =>
        current.map((s) => (s.id === id ? { ...s, human_decision: null } : s)),
      );
    } else {
      void refresh();
    }
  };

  return (
    <main>
      <h1>Journeyman for Support · {tenant}</h1>

      {error && (
        <p className="banner" role="status">
          {staleSince
            ? `Showing the last good numbers: ${error} (${formatDuration((Date.now() - staleSince) / 1000)} old)`
            : error}
        </p>
      )}

      {health && (
        <section className="tiles">
          {attentionOrder(health).map((metric) => (
            <Tile
              key={metric}
              label={LABELS[metric]}
              text={value(health, metric)}
              tone={level(metric, health[metric])}
            />
          ))}
          <Tile
            label="agreement rate"
            text={formatPercent(health.agreement_rate)}
            tone={agreementLevel(health.agreement_rate)}
          />
          <Tile label="spent today" text={formatCents(health.spent_cents)} tone="ok" />
        </section>
      )}

      {health && health.alerts.length > 0 && (
        <ul className="alerts">
          {health.alerts.map((alert) => (
            <li key={alert}>{alert}</li>
          ))}
        </ul>
      )}

      <section className="suggestions">
        {suggestions.length === 0 ? (
          <p className="muted">No suggestions yet.</p>
        ) : (
          suggestions.map((s) => <SuggestionCard key={s.id} suggestion={s} onDecide={decide} />)
        )}
      </section>
    </main>
  );
}
