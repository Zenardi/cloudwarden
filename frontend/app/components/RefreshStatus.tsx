/**
 * A single visually-hidden polite live region for refresh status. Replaces the
 * per-container `aria-live` on the KPI trio and the AI summary, which made a
 * screen reader re-read the whole block on every refresh. One concise message
 * ("Data refreshed, as of …") announces once; an empty message leaves nothing
 * stale to read.
 */
export function RefreshStatus({ message }: { message: string }) {
  return (
    <div className="sr-only" role="status" aria-live="polite">
      {message}
    </div>
  );
}
