import { inr, num, pct, sign, signedPct, type Scoreboard } from "../api";

/** The per-cause cut, on the same zero axis as the headline so the page has one grammar.
 *
 * Sorted by value contributed, which puts the row where the agent destroys value at the
 * bottom — visible, not omitted. That row is the reason this panel exists. */
export function Causes({ board }: { board: Scoreboard }) {
  const rows = [...board.segments.cause].sort(
    (x, y) => y.incremental_recovered_paise - x.incremental_recovered_paise,
  );
  const peak = Math.max(...rows.map((r) => Math.abs(r.incremental_rate))) || 1;

  return (
    <table>
      <thead>
        <tr>
          <th>cause</th>
          <th className="n">n</th>
          <th className="n">agent</th>
          <th className="n">holdout</th>
          <th className="n">lift</th>
          <th />
          <th className="n">value</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const w = (Math.abs(r.incremental_rate) / peak) * 50;
          const left = r.incremental_rate >= 0 ? 50 : 50 - w;
          const colour = r.incremental_rate >= 0 ? "var(--pos)" : "var(--neg)";
          return (
            <tr key={r.segment}>
              <td className="lbl">{r.segment}</td>
              <td className="n mut">{num(r.accounts)}</td>
              <td className="n">{pct(r.rate_treatment)}</td>
              <td className="n mut">{pct(r.rate_holdout)}</td>
              <td className={`n ${sign(r.incremental_rate)}`}>{signedPct(r.incremental_rate)}</td>
              <td>
                <div className="div">
                  <span className="mid" />
                  <i style={{ left: `${left}%`, width: `${w}%`, background: colour }} />
                </div>
              </td>
              <td className={`n ${sign(r.incremental_recovered_paise)}`}>
                {inr(r.incremental_recovered_paise)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
