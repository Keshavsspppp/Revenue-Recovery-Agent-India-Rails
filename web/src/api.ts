/** The typed edge of the FastAPI surface. docs/09-API.md.
 *
 * Nothing here computes: every field is read from an endpoint that reads the ledger. If a
 * figure differs between this app and `rr report`, that is a bug in the API layer, not a
 * difference of opinion.
 */

export interface ArmMetrics {
  arm: string;
  accounts: number;
  at_risk_paise: number;
  recovered_paise: number;
  cost_paise: number;
  contacts: number;
  attempts: number;
  recovered_accounts: number;
}

export interface Segment {
  segment: string;
  accounts: number;
  treatment_n: number;
  holdout_n: number;
  rate_treatment: number;
  rate_holdout: number;
  incremental_rate: number;
  incremental_recovered_paise: number;
  at_risk_paise: number;
}

export interface HarmCounters {
  opt_outs_per_1k: number;
  complaints_per_1k: number;
  disputes_per_1k: number;
  mandate_cancellations_per_1k: number;
  hardship_exits_per_1k: number;
  contacts_p50: number;
  contacts_p95: number;
  contacts_per_recovery: number;
}

export interface Scoreboard {
  batch: {
    batch_id: string;
    seed: number;
    policy: string;
    policy_version: string;
    lambda_harm: number;
    n_accounts: number;
  };
  treatment: ArmMetrics;
  holdout: ArmMetrics;
  incremental_rate: number;
  incremental_recovered_paise: number;
  holdout_recovered_rate_adjusted_paise: number;
  cost_delta_paise: number;
  net_incremental_paise: number;
  ci95_paise: [number, number];
  denominators: Record<string, { rate: number; n: number }>;
  harm: { treatment: HarmCounters; holdout: HarmCounters };
  compliance: {
    notice_window_violations: number;
    unmapped_code_count: number;
    policy_denials_by_rule: Record<string, number>;
  };
  diagnostics: {
    self_cure_share: number;
    hardship_detector?: {
      precision: number;
      recall: number;
      base_rate: number;
      true_positive: number;
      false_positive: number;
      false_negative: number;
    };
    days_to_recover_treatment: { p50: number; p90: number; n: number };
    days_to_recover_holdout: { p50: number; p90: number; n: number };
  };
  segments: { cause: Segment[]; amount: Segment[]; category: Segment[]; tier: Segment[] };
}

export interface BatchSummary {
  file: string;
  batch_id: string;
  seed: number;
  policy: string;
  status: string;
  accounts: number;
  events: number;
}

export interface TimelineEvent {
  seq: number;
  occurred_at: string;
  stage: string;
  action: string | null;
  rule_failed: string | null;
  reason: string | null;
  basis: string | null;
  posterior: Record<string, number> | null;
  evidence: string[] | null;
  result: Record<string, unknown> | null;
  notes: string | null;
  hash: string;
}

export interface GateVerdict {
  verdict: "ALLOW" | "DENY";
  rule_id_failed: string | null;
  reason: string | null;
  basis: string | null;
  rule_ids_passed: string[];
  policy_version: string;
  dry_run: boolean;
}

export interface SyncedLink {
  account_id: string;
  provider_id: string;
  status: string;
  amount_paise: number;
  amount_paid_paise: number;
  settled: boolean;
  paid_at: string | null;
  url: string;
}

export interface LiveReference {
  account_id: string;
  provider: string;
  provider_id: string;
  url: string;
  amount_paise: number;
}

/** An error carrying what the API said, so a panel can show the fix rather than a stack. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "ApiError";
  }
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new ApiError(body.detail ?? `${res.status} ${res.statusText}`, res.status);
  }
  return res.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  batches: () => get<BatchSummary[]>("/batches"),

  scoreboard: (batch: string, bootstrap = 4000) =>
    get<Scoreboard>(`/batches/${enc(batch)}/scoreboard?bootstrap=${bootstrap}`),

  interesting: (batch: string) =>
    get<Record<string, string[]>>(`/batches/${enc(batch)}/interesting`),

  timeline: (batch: string, account: string) =>
    get<TimelineEvent[]>(`/batches/${enc(batch)}/accounts/${enc(account)}/timeline`),

  liveReferences: (batch: string) =>
    get<LiveReference[]>(`/live/references?batch=${enc(batch)}`),

  syncLive: async (batch: string): Promise<SyncedLink[]> => {
    const res = await fetch("/live/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch }),
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(out.detail ?? "sync failed", res.status);
    return out as SyncedLink[];
  },

  evaluate: async (body: Record<string, unknown>): Promise<GateVerdict> => {
    const res = await fetch("/policy/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new ApiError(out.detail ?? "evaluation failed", res.status);
    return out as GateVerdict;
  },
};

/* ---- formatting ---------------------------------------------------------
   Money is integer paise everywhere and formatted only here, at the edge. The Indian
   grouping is not cosmetic: ₹17,39,386 is how the number is read in the room. */

export const inr = (paise: number): string =>
  (paise < 0 ? "−" : "") + "₹" + Math.abs(Math.round(paise / 100)).toLocaleString("en-IN");

export const pct = (x: number): string => (x * 100).toFixed(1) + "%";

export const signedPct = (x: number): string =>
  (x >= 0 ? "+" : "−") + Math.abs(x * 100).toFixed(1) + "%";

export const num = (n: number): string => n.toLocaleString("en-IN");

export const sign = (x: number): "pos" | "neg" | "mut" =>
  x > 0 ? "pos" : x < 0 ? "neg" : "mut";
