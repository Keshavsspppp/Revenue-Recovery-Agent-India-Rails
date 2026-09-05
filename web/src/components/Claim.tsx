import { inr, num, pct, sign, type Scoreboard } from "../api";

/** The claim, with the interval as the object rather than a parenthesis under one.
 *
 * Zero is drawn as a full-height rule and the band is tinted on both sides of it, so an
 * interval that contains zero *looks* like it contains zero instead of being explained
 * away in a caption. The page has to be honest in both states; the copy branches on which
 * one is true. */
export function Claim({ board }: { board: Scoreboard }) {
  const t = board.treatment;
  const h = board.holdout;
  const incr = board.incremental_recovered_paise;
  const [lo, hi] = board.ci95_paise;
  const holdoutAdj = board.holdout_recovered_rate_adjusted_paise;

  // Pad the axis so the interval never touches an edge, and always include zero.
  const min = Math.min(lo, 0);
  const max = Math.max(hi, 0);
  const pad = (max - min) * 0.12 || 1;
  const a = min - pad;
  const b = max + pad;
  const at = (v: number) => ((v - a) / (b - a)) * 100;
  const crosses = lo <= 0 && hi >= 0;
  const span = at(hi) - at(lo);
  const zeroPct = span > 0 ? ((at(0) - at(lo)) / span) * 100 : 0;

  const peak = Math.max(t.recovered_paise, holdoutAdj) || 1;

  const arm = (k: "t" | "h", name: string, value: number, rate: number, n: number) => (
    <div className={`arm ${k}`}>
      <div className="arm-head">
        <span className="arm-name">
          {name} <span className="mut">n={num(n)}</span>
        </span>
        <span className="arm-val">
          {inr(value)} · {pct(rate)}
        </span>
      </div>
      <div className="arm-bar">
        <i style={{ width: `${((value / peak) * 100).toFixed(1)}%` }} />
      </div>
    </div>
  );

  return (
    <div className="claim">
      <div className="card pad">
        <div className={`fig ${sign(incr)}`}>{inr(incr)}</div>
        <div className="fig-sub">
          incremental recovered · {pct(board.incremental_rate)} of at-risk · net of cost{" "}
          {inr(board.net_incremental_paise)}
        </div>

        <div
          className="axis"
          role="img"
          aria-label={`95% confidence interval, ${inr(lo)} to ${inr(hi)}, ${
            crosses ? "contains zero" : "excludes zero"
          }`}
        >
          <div className="axis-track">
            <div className="axis-line" />
            <div
              className={`axis-band${crosses ? " crosses" : ""}`}
              style={
                {
                  left: `${at(lo)}%`,
                  width: `${span.toFixed(2)}%`,
                  "--zero": `${zeroPct.toFixed(2)}%`,
                } as React.CSSProperties
              }
            />
            <div className="axis-zero" style={{ left: `${at(0)}%` }} />
            <div className={`axis-est${incr < 0 ? " neg" : ""}`} style={{ left: `${at(incr)}%` }} />
            <div className="tick zero" style={{ left: `${at(0)}%` }}>ZERO</div>
            <div className="tick" style={{ left: `${at(lo)}%` }}>{inr(lo)}</div>
            <div className="tick" style={{ left: `${at(hi)}%` }}>{inr(hi)}</div>
          </div>
        </div>
        <p className="cap">
          95% CI, stratified bootstrap over accounts.{" "}
          {crosses ? (
            <>
              The interval contains zero, so <b>this batch does not establish the headline</b>.
              Reported as measured rather than tuned — the cut by cause below is where the
              effect actually lives.
            </>
          ) : (
            <>
              The interval excludes zero. The holdout is randomised and untouched by the
              agent's decisions, so the gap is attributable to the policy and not to the batch.
            </>
          )}
        </p>
      </div>

      <div className="card pad">
        {arm("t", "agent", t.recovered_paise, t.recovered_paise / t.at_risk_paise, t.accounts)}
        {arm(
          "h",
          "holdout, rate-adjusted",
          holdoutAdj,
          h.recovered_paise / h.at_risk_paise,
          h.accounts,
        )}
        <table style={{ marginTop: 22 }}>
          <thead>
            <tr>
              <th>denominator</th>
              <th className="n">rate</th>
              <th className="n">n</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(board.denominators).map(([k, v]) => (
              <tr key={k}>
                <td className="lbl">{k.replace(/_/g, " ")}</td>
                <td className="n">{pct(v.rate)}</td>
                <td className="n mut">{num(v.n)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="cap" style={{ marginTop: 12 }}>
          All three published, denominator printed beside each. Vendors quote the first;
          the last one is the honest one.
        </p>
      </div>
    </div>
  );
}
