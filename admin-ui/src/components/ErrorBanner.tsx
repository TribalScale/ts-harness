import { Banner } from "@astryxdesign/core/Banner";
import { Button } from "./Button";

export function ErrorBanner({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <Banner
      className="error"
      status="error"
      role="alert"
      title={message}
      container="section"
      endContent={
        onRetry ? (
          <Button variant="ghost" onClick={onRetry}>
            Retry
          </Button>
        ) : undefined
      }
    />
  );
}
