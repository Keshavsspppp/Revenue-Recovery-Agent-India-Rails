import type { HarmCounters, Scoreboard } from "../api";

const ROWS: Array<[keyof HarmCounters, string]> = [
  ["opt_outs_per_1k", "opted out"],
  ["complaints_per_1k", "complained"],
  ["mandate_cancellations_per_1k", "cancelled the mandate"],
  ["disputes_per_1k", "disputed"],
  ["hardship_exits_per_1k", "routed out for hardship"],
];

/** Harm on the same screen as the money, deliberately.
 *
 * The `vs` column is the difference rather than a pair of bars: two numeric columns
 * already carry the comparison, and five near-empty mini-bars beside them carried
 * nothing. */
export function Harm({ board }: { board: Scoreboard }) {
  const t = board.harm.treatment;
  const h = board.harm.holdout;
  const c = board.compliance;
  const hd = board.diagnostics.hardship_detector;
  const denials = Object.entries(c.policy_denials_by_rule ?? {});
  const refused = denials.reduce((s, [, n]) => s + n, 0);

  const delta = (d: number) =>
    d === 0 ? "—" : (d > 0 ? "+" : "−") + Math.abs(d).toFixed(1);

  return (
    <>
      <table>
        <thead>
          <tr>
            <th>per 1,000 accounts</th>
            <th className="n">agent</th>
            <th className="n">holdout</th>
            <th className="n">vs</th>
          </tr>
        </thead>
        <tbody>
          {ROWS.map(([k, label]) => {
            const d = t[k] - h[k];
            const cls = d > 0 ? "neg" : d < 0 ? "pos" : "mut";
            return (
              <tr key={k}>
                <td>{label}</td>
                <td className={`n ${cls}`}>{t[k].toFixed(1)}</td>
                <td className="n mut">{h[k].toFixed(1)}</td>
                <td className={`n ${cls}`}>{delta(d)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div style={{ marginTop: 20 }}>
        <div className="kv">
          <span>
            Notice-window violations <span className="mut">— our own defect counter</span>
          </span>
          <b className={c.notice_window_violations ? "neg" : "pos"}>
            {c.notice_window_violations}
          </b>
        </div>
        <div className="kv">
          <span>Contacts per account <span className="mut">— p95</span></span>
          <b>
            {t.contacts_p95.toFixed(0)} <span className="mut">vs {h.contacts_p95.toFixed(0)}</span>
          </b>
        </div>
        <div className="kv">
          <span>Actions the gate refused</span>
          <b>{refused}</b>
        </div>
        {/* Zero is the expected value for this one, and a counter that always reads zero
            is furniture. It earns a line only when it has something to report. */}
        {c.unmapped_code_count > 0 && (
          <div className="kv">
            <span>Unmapped rail codes</span>
            <b className="neg">{c.unmapped_code_count}</b>
          </div>
        )}
      </div>

      {refused > 0 && (
        <p className="cap" style={{ marginTop: 10 }}>
          {denials.map(([rule, n], i) => (
            <span key={rule}>
              {i > 0 && " · "}
              <code>{rule}</code> {n}
            </span>
          ))}
        </p>
      )}

      {hd && (
        <p className="cap" style={{ marginTop: 16 }}>
          Hardship detector, scored against ground truth: precision{" "}
          <b>{hd.precision.toFixed(2)}</b>, recall <b>{hd.recall.toFixed(2)}</b> —
          precise rather than complete, on purpose.
        </p>
      )}
    </>
  );
}
