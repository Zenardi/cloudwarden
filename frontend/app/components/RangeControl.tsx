import { useId, useRef } from "react";
import { nextRadioIndex } from "../lib/roving";

export const RANGE_OPTIONS = [7, 30, 90] as const;
export type RangeDays = (typeof RANGE_OPTIONS)[number];

/**
 * A 7 / 30 / 90-day segmented control that sets the cost-trend window. Modeled as
 * a WAI-ARIA radiogroup — one-of-N selection announced as "Range, 30d, selected",
 * with roving tabindex + arrow-key navigation. `disabled` locks it during a fetch.
 */
export function RangeControl({
  value,
  onChange,
  disabled = false,
}: {
  value: number;
  onChange: (days: RangeDays) => void;
  disabled?: boolean;
}) {
  const captionId = useId();
  const radios = useRef<(HTMLButtonElement | null)[]>([]);
  return (
    <div className="control-group">
      <span className="control-label" id={captionId}>
        Range
      </span>
      <div className="range-control" role="radiogroup" aria-labelledby={captionId}>
        {RANGE_OPTIONS.map((d, i) => (
          <button
            key={d}
            ref={(el) => {
              radios.current[i] = el;
            }}
            type="button"
            role="radio"
            className="range-opt"
            aria-checked={value === d}
            tabIndex={value === d ? 0 : -1}
            disabled={disabled}
            onClick={() => onChange(d)}
            onKeyDown={(e) => {
              const next = nextRadioIndex(e.key, i, RANGE_OPTIONS.length);
              if (next === null) return;
              e.preventDefault();
              onChange(RANGE_OPTIONS[next]);
              radios.current[next]?.focus();
            }}
          >
            {d}d
          </button>
        ))}
      </div>
    </div>
  );
}
