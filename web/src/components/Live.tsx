import { useEffect, useState } from "react";
import { api, inr, type LiveReference, type SyncedLink } from "../api";
import { Skeleton } from "./Section";

/** Real Razorpay test-mode objects, and what became of them.
 *
 * The live slice is its own artefact, not a property of the batch on screen: it runs on a
 * separate, deliberately small batch that is never given a policy, so it would never be
 * selectable in the picker and the most checkable thing on the page — real provider ids —
 * would never appear. Look in the selected batch first, then anywhere.
 *
 * "Check payments" reads Razorpay back. A link nobody has paid produces no payment, which
 * is why the dashboard's Payments screen stays empty while Payment Links fills up; pay one
 * with a test card and this is where it turns green. */
export function Live({ batch }: { batch: string }) {
  const [refs, setRefs] = useState<LiveReference[] | null>(null);
  const [from, setFrom] = useState<string>(batch);
  const [loading, setLoading] = useState(true);
  const [synced, setSynced] = useState<Record<string, SyncedLink>>({});
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setSynced({});
    (async () => {
      const tryBatch = (b: string) => api.liveReferences(b).catch(() => [] as LiveReference[]);
      let found = await tryBatch(batch);
      let source = batch;
      if (!found.length) {
        const all = await api.batches().catch(() => []);
        for (const b of all) {
          if (b.file === batch) continue;
          const hit = await tryBatch(b.file);
          if (hit.length) {
            found = hit;
            source = b.file;
            break;
          }
        }
      }
      if (live) {
        setRefs(found);
        setFrom(source);
        setLoading(false);
      }
    })();
    return () => {
      live = false;
    };
  }, [batch]);

  async function check() {
    setChecking(true);
    setError(null);
    try {
      const links = await api.syncLive(from);
      setSynced(Object.fromEntries(links.map((l) => [l.provider_id, l])));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setChecking(false);
    }
  }

  if (loading) return <Skeleton rows={4} height={18} />;
  if (!refs?.length) return null;

  const checked = Object.keys(synced).length > 0;
  const paid = Object.values(synced).filter((l) => l.settled);
  const collected = Object.values(synced).reduce((s, l) => s + l.amount_paid_paise, 0);

  return (
    <>
      {from !== batch && (
        <p className="cap" style={{ margin: "0 0 14px" }}>
          From batch <code>{from}</code> — the live slice runs on its own batch, which is
          never given a policy, so it is excluded from the measured comparison by
          construction.
        </p>
      )}

      <div className="controls" style={{ marginBottom: 14 }}>
        <button onClick={check} disabled={checking}>
          {checking ? "Asking Razorpay…" : "Check payments"}
        </button>
        <span className="source">
          {checked
            ? `${paid.length} of ${refs.length} paid · ${inr(collected)} collected`
            : "reads the provider back; creates nothing"}
        </span>
      </div>

      {error && (
        <div className="failed" style={{ marginBottom: 14 }}>
          <h4>Could not reach Razorpay</h4>
          <p>{error}</p>
        </div>
      )}

      <div className="card scroll">
        <div className="pad">
          <table>
            <thead>
              <tr>
                <th>account</th>
                <th>razorpay id</th>
                <th className="n">amount</th>
                <th className="n">paid</th>
                <th>status</th>
                <th>link</th>
              </tr>
            </thead>
            <tbody>
              {refs.map((r) => {
                const s = synced[r.provider_id];
                return (
                  <tr key={r.provider_id}>
                    <td className="lbl">{r.account_id}</td>
                    <td className="lbl">{r.provider_id}</td>
                    <td className="n">{inr(r.amount_paise)}</td>
                    <td className={`n ${s?.settled ? "pos" : "mut"}`}>
                      {s ? inr(s.amount_paid_paise) : "—"}
                    </td>
                    <td className={`lbl ${s?.settled ? "pos" : "mut"}`}>
                      {s ? (s.settled ? "PAID" : s.status) : "not checked"}
                    </td>
                    <td>
                      {/^https:\/\//.test(r.url) ? (
                        <a href={r.url} target="_blank" rel="noopener noreferrer">
                          {r.url}
                        </a>
                      ) : (
                        r.url
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {checked && paid.length === 0 && (
        <p className="cap" style={{ marginTop: 14 }}>
          Nothing has been paid yet, so your dashboard's <b>Payments</b> screen is empty by
          definition — the links themselves are under <b>Payment Links</b>. Open one above
          and pay it with test card <code>4111 1111 1111 1111</code>, any future expiry, any
          CVV. Then check again: the payment is written to the ledger as a confirmed
          recovery, counted from what Razorpay says rather than from the fact that we asked.
        </p>
      )}
    </>
  );
}
