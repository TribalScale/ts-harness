import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";

/** What an approval gate asks a human to approve (GET /admin/workflows/gate-review). */
export type GateReviewData = {
  task_id: string;
  step_name?: string;
  kind: "documents" | "release" | "none";
  from_step?: string;
  summary?: string;
  warnings?: string[];
  confidence?: string | null;
  documents?: { key: string; title: string; markdown: string }[];
  release?: {
    environment: string;
    repo: string;
    sha: string;
    base_branch?: string | null;
    pr_urls: string[];
    ci: {
      state?: string | null;
      checks: Record<string, string>;
      failed: string[];
    };
    rollback_sha?: string | null;
    required_roles?: string[];
    commit_url?: string;
    compare_url?: string;
    previous?: {
      environment: string;
      status?: string | null;
      evidence_url?: string | null;
    };
  };
};

function prLabel(url: string): string {
  const match = url.match(/\/pull\/(\d+)/);
  return match ? `PR #${match[1]}` : url;
}

function humanize(value: string): string {
  return value.replace(/_/g, " ");
}

function ReleaseReview({
  release,
}: {
  release: NonNullable<GateReviewData["release"]>;
}) {
  const checks = Object.entries(release.ci.checks || {});
  return (
    <dl className="gate-review-facts">
      <dt>Promote to</dt>
      <dd>
        <strong>{release.environment}</strong>
        {release.required_roles && release.required_roles.length > 0 && (
          <span className="muted">
            {" "}
            · needs {release.required_roles.join(", ")}
          </span>
        )}
      </dd>
      <dt>Commit</dt>
      <dd>
        <span className="mono">{release.repo}</span>{" "}
        {release.commit_url ? (
          <a
            href={release.commit_url}
            target="_blank"
            rel="noreferrer"
            className="mono"
          >
            {release.sha.slice(0, 12)}
          </a>
        ) : (
          <span className="mono">{release.sha.slice(0, 12)}</span>
        )}
        {release.base_branch && (
          <span className="muted"> on {release.base_branch}</span>
        )}
      </dd>
      {release.pr_urls.length > 0 && (
        <>
          <dt>Includes</dt>
          <dd className="gate-review-links">
            {release.pr_urls.map((url) => (
              <a key={url} href={url} target="_blank" rel="noreferrer">
                {prLabel(url)}
              </a>
            ))}
          </dd>
        </>
      )}
      {release.compare_url && (
        <>
          <dt>Changes</dt>
          <dd>
            <a href={release.compare_url} target="_blank" rel="noreferrer">
              Compare with what {release.environment} runs now
            </a>
          </dd>
        </>
      )}
      <dt>CI</dt>
      <dd>
        {release.ci.state || "unknown"}
        {checks.length > 0 && (
          <span className="muted">
            {" "}
            ({checks.map(([name, state]) => `${name}: ${state}`).join(", ")})
          </span>
        )}
      </dd>
      {release.previous && (
        <>
          <dt>{release.previous.environment}</dt>
          <dd>
            {release.previous.status || "unknown"}
            {release.previous.evidence_url && (
              <>
                {" · "}
                <a
                  href={release.previous.evidence_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  deploy run
                </a>
              </>
            )}
          </dd>
        </>
      )}
      {release.rollback_sha && (
        <>
          <dt>Rolls back to</dt>
          <dd className="mono">{release.rollback_sha.slice(0, 12)}</dd>
        </>
      )}
    </dl>
  );
}

export function GateReviewBody({ review }: { review: GateReviewData }) {
  if (review.kind === "release" && review.release)
    return <ReleaseReview release={review.release} />;
  if (review.kind !== "documents")
    return <p className="muted">This gate has no document to review.</p>;
  const documents = review.documents || [];
  return (
    <div className="gate-review-docs">
      {review.summary && <p>{review.summary}</p>}
      {review.warnings && review.warnings.length > 0 && (
        <div className="gate-review-warnings">
          <strong>
            Warnings from{" "}
            {review.from_step ? humanize(review.from_step) : "the agent"}
          </strong>
          <ul>
            {review.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}
      {documents.map((doc, index) => (
        <details key={doc.key} className="gate-review-doc" open={index === 0}>
          <summary>{doc.title}</summary>
          <pre>{doc.markdown}</pre>
        </details>
      ))}
    </div>
  );
}

export function GateReview({ taskId }: { taskId: string }) {
  const [review, setReview] = useState<GateReviewData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setReview(null);
    setError(null);
    apiFetch<GateReviewData>(
      `/admin/workflows/gate-review?task_id=${encodeURIComponent(taskId)}`,
    )
      .then((data) => live && setReview(data))
      .catch((e: any) => live && setError(String(e?.message || e)));
    return () => {
      live = false;
    };
  }, [taskId]);

  return (
    <section className="gate-review" aria-label="What you are approving">
      <h4>What you are approving</h4>
      {error && <p className="error">{error}</p>}
      {!review && !error && <p className="muted">Loading…</p>}
      {review && <GateReviewBody review={review} />}
    </section>
  );
}
