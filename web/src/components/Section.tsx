import type { ReactNode } from "react";
import { ApiError } from "../api";

/** A section header that names the endpoint behind it.
 *
 * The whole argument of this page is "do not trust the number, check the trail", so
 * every panel says where its numbers came from. It is an affordance, not an eyebrow. */
export function Section({
  title,
  source,
  lede,
  children,
  id,
}: {
  title: string;
  source: string;
  lede?: ReactNode;
  children: ReactNode;
  id?: string;
}) {
  return (
    <section id={id}>
      <div className="eyebrow">
        <h2>{title}</h2>
        <span className="source">{source}</span>
      </div>
      {lede && <p className="lede">{lede}</p>}
      {children}
    </section>
  );
}

/** A shape where the content will be. Never the word "loading" — a skeleton tells you
 * the page is working and roughly what is coming; a spinner tells you neither. */
export function Skeleton({ rows = 3, height = 16 }: { rows?: number; height?: number }) {
  return (
    <div style={{ display: "grid", gap: 10 }} aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <span
          key={i}
          className="skel"
          style={{ height, width: `${100 - (i % 3) * 12}%` }}
        />
      ))}
    </div>
  );
}

/** Errors explain what went wrong and how to fix it. A 409 from the scoreboard means the
 * batch was simulated but never run, and the fix is one command — so print the command. */
export function Failed({ error, batch }: { error: unknown; batch?: string }) {
  const status = error instanceof ApiError ? error.status : 0;
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className="failed" role="alert">
      <h4>{status === 409 ? "Batch not run" : status ? `Error ${status}` : "Cannot reach the API"}</h4>
      <p>
        {message}
        {status === 0 && (
          <>
            {" "}Start it with{" "}
            <code>uvicorn app.api.main:app --port 8010</code>
            {batch ? <> and reload.</> : "."}
          </>
        )}
      </p>
    </div>
  );
}
