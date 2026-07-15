import { useId, useRef } from "react";
import { nextRadioIndex } from "../lib/roving";

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
 * Same segmented look as the range control (`.range-control`/`.range-opt`).
 * Modeled as a WAI-ARIA radiogroup: one-of-N selection announced as
 * "Cloud, Azure, selected", with roving tabindex + arrow-key navigation.
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
  const captionId = useId();
  const radios = useRef<(HTMLButtonElement | null)[]>([]);
  return (
    <div className="control-group">
      <span className="control-label" id={captionId}>
        Cloud
      </span>
      <div className="range-control" role="radiogroup" aria-labelledby={captionId}>
        {PROVIDER_SCOPES.map((p, i) => (
          <button
            key={p}
            ref={(el) => {
              radios.current[i] = el;
            }}
            type="button"
            role="radio"
            className="range-opt"
            aria-checked={value === p}
            tabIndex={value === p ? 0 : -1}
            disabled={disabled}
            onClick={() => onChange(p)}
            onKeyDown={(e) => {
              const next = nextRadioIndex(e.key, i, PROVIDER_SCOPES.length);
              if (next === null) return;
              e.preventDefault();
              onChange(PROVIDER_SCOPES[next]);
              radios.current[next]?.focus();
            }}
          >
            {LABELS[p]}
          </button>
        ))}
      </div>
    </div>
  );
}
