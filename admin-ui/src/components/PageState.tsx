import { EmptyState } from "@astryxdesign/core/EmptyState";
import { Button } from "./Button";

export function PageState({
  kind,
  title,
  detail,
  onRetry,
}: {
  kind: "loading" | "empty" | "error" | "stale";
  title: string;
  detail?: string;
  onRetry?: () => void;
}) {
  const retry = onRetry ? (
    <Button variant="secondary" onClick={onRetry}>
      Try again
    </Button>
  ) : undefined;

  if (kind === "empty") {
    return <EmptyState title={title} description={detail} actions={retry} />;
  }

  return (
    <section className={`page-state page-state-${kind}`} role={kind === "error" ? "alert" : "status"}>
      <h3>{title}</h3>
      {detail && <p>{detail}</p>}
      {retry}
    </section>
  );
}
