import { useEffect, useMemo, useState } from "react";
import { api, num } from "./api";
import { useAsync } from "./useAsync";
import { Failed, Section, Skeleton } from "./components/Section";
import { Claim } from "./components/Claim";
import { Causes } from "./components/Causes";
import { Gate } from "./components/Gate";
import { Harm } from "./components/Harm";
import { Trail } from "./components/Trail";
import { Live } from "./components/Live";

const GROUPS: Array<[string, string]> = [
  ["mandate_repaired", "mandate repaired onto another rail"],
  ["hardship_exit", "routed out for hardship"],
  ["stopped_early", "stopped: not worth continuing"],
  ["denied", "had an action refused"],
  ["live_razorpay", "live on Razorpay"],
  ["recovered", "recovered"],
];

export default function App() {
  const batches = useAsync(() => api.batches(), []);
  const [batch, setBatch] = useState<string>("");

  // Batches that have never been run have no arms and no outcomes, so every figure on the
  // scoreboard would divide by zero. Offer them only if there is nothing else.
  const usable = useMemo(
    () => (batches.data ?? []).filter((b) => b.policy && b.policy !== "not run"),
    [batches.data],
  );

  const choices = usable.length ? usable : (batches.data ?? []);

  useEffect(() => {
    if (!batch && choices.length) setBatch(choices[0].file);
  }, [choices, batch]);

  // Each panel owns its own request. Nothing waits on the scoreboard except the two
  // panels that actually need it.
  const board = useAsync(() => (batch ? api.scoreboard(batch) : Promise.reject(new Error("no batch"))), [batch]);
  const picks = useAsync<Record<string, string[]>>(
    () => (batch ? api.interesting(batch) : Promise.resolve({})),
    [batch],
  );

  const [account, setAccount] = useState("");
  useEffect(() => {
    const first = GROUPS.map(([k]) => picks.data?.[k]?.[0]).find(Boolean);
    if (first) setAccount(first);
  }, [picks.data]);

  const meta = board.data?.batch;

  return (
    <>
      <header className="header">
        <div className="bar">
          <h1 className="mark">
            rr <span>/ revenue recovery</span>
          </h1>
          {/* A dropdown with one option is not a control. Most runs have exactly one
              usable batch, so show what is loaded and offer the picker only when there
              is something to pick. */}
          {choices.length > 1 ? (
            <select
              value={batch}
              onChange={(e) => setBatch(e.target.value)}
              aria-label="Batch"
            >
              {choices.map((b) => (
                <option key={b.file} value={b.file}>
                  {b.file} · {num(b.accounts)} accounts
                </option>
              ))}
            </select>
          ) : (
            <span className="meta">
              {choices[0] ? `${choices[0].file} · ${num(choices[0].accounts)} accounts` : ""}
            </span>
          )}
          <span className="spacer" />
          <span className="meta">
            {meta
              ? `seed ${meta.seed} · ${meta.policy} · ${meta.policy_version} · λ ${meta.lambda_harm}`
              : ""}
          </span>
        </div>
      </header>

      <main>
        <Section
          title="The claim"
          source="GET /batches/{b}/scoreboard · ledger stage OBSERVE"
          lede={
            <>
              Recovery measured against a randomised 20% holdout running the merchant's
              normal behaviour. <strong>The gap is the only number this project claims.</strong>{" "}
              Everything the holdout recovered would have come back anyway.
            </>
          }
        >
          {board.loading ? (
            <Skeleton rows={5} height={26} />
          ) : board.error ? (
            <Failed error={board.error} batch={batch} />
          ) : (
            <Claim board={board.data!} />
          )}
        </Section>

        <Section
          title="By cause"
          source="scoreboard.segments.cause"
          lede={
            <>
              One number over a heavy-tailed batch hides more than it shows. This is the cut
              that says what the agent is good at — <strong>and where it destroys value</strong>.
            </>
          }
        >
          <div className="card scroll">
            <div className="pad">
              {board.loading ? (
                <Skeleton rows={7} height={18} />
              ) : board.error ? (
                <Failed error={board.error} batch={batch} />
              ) : (
                <Causes board={board.data!} />
              )}
            </div>
          </div>
        </Section>

        <section>
          <div className="two">
            <div>
              <div className="eyebrow">
                <h2>The gate</h2>
                <span className="source">POST /policy/evaluate · executes nothing</span>
              </div>
              <p className="lede">
                A pure function every action passes through before it reaches a rail — not a
                prompt asking the model nicely. Set the clock to 19:30 and place a voice call.
              </p>
              <Gate batch={batch} account={account} />
            </div>
            <div>
              <div className="eyebrow">
                <h2>What it cost the customer</h2>
                <span className="source">scoreboard.harm</span>
              </div>
              <p className="lede">
                On the same screen as the money, deliberately. A recovery number without its
                harm number is an incomplete result.
              </p>
              <div className="card pad">
                {board.loading ? (
                  <Skeleton rows={6} height={18} />
                ) : board.error ? (
                  <Failed error={board.error} batch={batch} />
                ) : (
                  <Harm board={board.data!} />
                )}
              </div>
            </div>
          </div>
        </section>

        <Section
          title="One account, end to end"
          source="GET /batches/{b}/accounts/{id}/timeline · hash-chained"
          lede={
            <>
              Detect, assign, diagnose, narrow, propose, gate, execute, observe, close. Every
              row below is a ledger entry; the chain is hashed and <code>rr verify</code> checks it.
            </>
          }
        >
          <div className="controls" style={{ marginBottom: 16 }}>
            <select
              value={account}
              onChange={(e) => setAccount(e.target.value)}
              aria-label="Account"
              disabled={picks.loading}
            >
              {GROUPS.filter(([k]) => picks.data?.[k]?.length).map(([k, label]) => (
                <optgroup key={k} label={label}>
                  {picks.data![k].map((a) => (
                    <option key={a} value={a}>{a}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <span className="source">grouped by what happened to them</span>
          </div>
          <div className="card scroll">
            <div className="pad">
              {batch && <Trail batch={batch} account={account} />}
            </div>
          </div>
        </Section>

        <Section
          title="Live on Razorpay test mode"
          source="GET /live/references"
          lede={
            <>
              The measurement above is simulated at scale — you cannot randomise a control
              group against a live provider. <strong>These are real objects</strong>, created
              by the same agent, through the same gate, written to the same ledger. The links open.
            </>
          }
        >
          {batch && <Live batch={batch} />}
        </Section>
      </main>
    </>
  );
}
