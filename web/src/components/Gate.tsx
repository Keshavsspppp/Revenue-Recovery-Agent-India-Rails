import { useState } from "react";
import { api, type GateVerdict } from "../api";

const ACTIONS = [
  "VOICE_CONFIRM_PTP",
  "SEND_MESSAGE",
  "SEND_PAYMENT_LINK",
  "REQUEST_PTP",
  "RETRY_DEBIT",
  "SPLIT_DEBIT",
];

/** The gate, live. Executes nothing.
 *
 * The verdict is written to the ledger as a dry run, so the refusal produced on stage
 * appears in the trail shown thirty seconds later. */
export function Gate({ batch, account }: { batch: string; account: string }) {
  const [action, setAction] = useState(ACTIONS[0]);
  const [at, setAt] = useState("2026-09-10T19:30");
  const [busy, setBusy] = useState(false);
  const [verdict, setVerdict] = useState<GateVerdict | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function evaluate() {
    setBusy(true);
    setError(null);
    const debit = action === "RETRY_DEBIT" || action === "SPLIT_DEBIT";
    try {
      setVerdict(
        await api.evaluate({
          batch,
          account_id: account,
          action,
          at: `${at}:00+05:30`,
          channel: debit ? null : action === "VOICE_CONFIRM_PTP" ? "VOICE" : "SMS",
          rail: debit ? "ENACH" : null,
        }),
      );
    } catch (e) {
      setVerdict(null);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card pad">
      <div className="controls">
        <label className="source" htmlFor="gate-action" style={{ position: "absolute", left: -9999 }}>
          Action
        </label>
        <select id="gate-action" value={action} onChange={(e) => setAction(e.target.value)}>
          {ACTIONS.map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
        <label className="source" htmlFor="gate-at" style={{ position: "absolute", left: -9999 }}>
          Simulated IST time
        </label>
        <input
          id="gate-at"
          type="datetime-local"
          value={at}
          onChange={(e) => setAt(e.target.value)}
        />
        <button className="primary" onClick={evaluate} disabled={busy || !account}>
          {busy ? "Evaluating…" : "Evaluate"}
        </button>
      </div>

      {error && (
        <div className="verdict deny">
          <span className="neg">{error}</span>
        </div>
      )}

      {verdict && (
        <div className={`verdict ${verdict.verdict === "DENY" ? "deny" : "allow"}`}>
          <div className="controls">
            <span className="stamp">{verdict.verdict}</span>
            {verdict.rule_id_failed && <span className="rule-id">{verdict.rule_id_failed}</span>}
          </div>
          {verdict.reason && (
            <>
              <div className="reason">{verdict.reason}</div>
              <div className="basis">{verdict.basis}</div>
            </>
          )}
          <div className="passed">
            {verdict.rule_ids_passed.join("  ") || "no rules evaluated"}
          </div>
          <p className="cap" style={{ marginTop: 12 }}>
            Policy {verdict.policy_version}. Written to the ledger as a dry run — this
            decision is now in the trail below.
          </p>
        </div>
      )}
    </div>
  );
}
