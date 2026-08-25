import { AlertTriangle } from "lucide-react";
import { ApiError, ApiUnreachableError } from "@/api/client";
import { Button } from "@/components/ui/button";

/** Turns any error a page's query can produce into one clean, specific
 * message -- never a raw JS stack trace (see the milestone's own Error
 * States section). `ApiError` carries the backend's own
 * `{"error": {code, message}}` text (already human-readable, e.g.
 * "Application 'foo' was not found."); anything else gets a generic
 * fallback rather than exposing internals. */
export function PageError({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = describeError(error);

  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-border p-8 text-center">
      <AlertTriangle className="size-8 text-destructive" aria-hidden="true" />
      <p className="text-sm font-medium">{message}</p>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}

function describeError(error: unknown): string {
  if (error instanceof ApiUnreachableError) return "Unable to reach the Argus API.";
  if (error instanceof ApiError) {
    if (error.status === 503) return "Argus database unavailable.";
    return error.message;
  }
  return "Something went wrong loading this page.";
}
