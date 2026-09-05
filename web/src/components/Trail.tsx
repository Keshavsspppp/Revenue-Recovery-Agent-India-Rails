import { api, inr, type TimelineEvent } from "../api";
import { useAsync } from "../useAsync";
import { Failed, Skeleton } from "./Section";

/** The chain, drawn as a chain: one spine, one node per event, filled where the agent did
 * something and red where the cycle ended. */
export function Trail({ batch, account }: { batch: string; account: string }) {
  const { data, error, loading } = useAsync(
    () => (account ? api.timeline(batch, account) : Promise.resolve([])),
    [batch, account],
  );

  if (loading) return <Skeleton rows={8} height={22} />;
  if (error) return <Failed error={error} batch={batch} />;
  if (!data?.length) return <p className="cap">No events for this account.</p>;

  return (
    <div className="chain">
      {data.map((e) => (
        <Row key={e.seq} e={e} />
      ))}
    </div>
  );
}

function Row({ e }: { e: TimelineEvent }) {
  const r = (e.result ?? {}) as Record<string, unknown>;
  let out = "";
  let tone = "";

  if (e.rule_failed) {
    out = `DENY · ${e.rule_failed}`;
    tone = "neg";
  } else if (e.stage === "GATE") {
    out = "ALLOW";
    tone = "pos";
  } else if (typeof r.terminal_state === "string") {
    out = r.terminal_state;
  } else if (typeof r.rail_code === "string") {
    out = r.rail_code;
  } else if (typeof r.provider_id === "string") {
    out = r.provider_id;
  }

  const parts = Array.isArray(r.parts) ? (r.parts as number[]) : null;
  if (parts) {
    out += ` · ${parts.length} presentations, collected ${inr(Number(r.collected_paise ?? 0))}`;
  }

  const posterior = e.posterior
    ? Object.entries(e.posterior)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 2)
        .map(([c, p]) => `${c} ${p.toFixed(2)}`)
        .join("  ")
    : "";
  const evidence = (e.evidence ?? []).slice(0, 4).join("  ");

  const kind =
    e.stage === "EXECUTE" || e.stage === "GATE"
      ? "act"
      : (e.stage === "CLOSE" || e.stage === "OBSERVE") && out
        ? "stop"
        : "";

  return (
    <div className={`ev ${kind}`}>
      <span className="seq">{e.seq}</span>
      <span className="stage">{e.stage}</span>
      <div className="ev-body">
        <span className="ev-act">{e.action ?? ""}</span>
        {out && <span className={`ev-out ${tone}`}> {out}</span>}
        <div className="ev-meta">
          {e.occurred_at.slice(0, 16).replace("T", " ")} {posterior || evidence}
          {e.reason ? ` — ${e.reason}` : ""}
        </div>
      </div>
    </div>
  );
}
