export const PROVIDER_SCOPES = ["all", "azure", "aws", "gcp"] as const;
export type ProviderScope = (typeof PROVIDER_SCOPES)[number];

const LABELS: Record<ProviderScope, string> = {
  all: "All",
  azure: "Azure",
  aws: "AWS",
  gcp: "GCP",
};

/**
 * A cloud filter for the Overview — CloudWarden is multi-cloud, so a single
 * cloud selector scopes the cost + governance panels to one provider (or all).
 * Same segmented look as the range control (`.range-control`/`.range-opt`);
 * native `<button>`s (keyboard-operable; `aria-pressed` marks the active cloud).
 */
export function ScopeControls({
  value,
  onChange,
  disabled = false,
}: {
  value: ProviderScope;
  onChange: (provider: ProviderScope) => void;
  disabled?: boolean;
}) {
  return (
    <div className="range-control" role="group" aria-label="Cloud filter">
      {PROVIDER_SCOPES.map((p) => (
        <button
          key={p}
          type="button"
          className="range-opt"
          aria-pressed={value === p}
          disabled={disabled}
          onClick={() => onChange(p)}
        >
          {LABELS[p]}
        </button>
      ))}
    </div>
  );
}
