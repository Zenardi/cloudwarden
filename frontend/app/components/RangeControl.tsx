export const RANGE_OPTIONS = [7, 30, 90] as const;
export type RangeDays = (typeof RANGE_OPTIONS)[number];

/**
 * A 7 / 30 / 90-day segmented control that sets the cost-trend window. Native
 * `<button>`s (keyboard-operable for free); the active option is marked with
 * `aria-pressed`. `disabled` locks the group while a trend fetch is in flight.
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
  return (
    <div className="range-control" role="group" aria-label="Cost trend window">
      {RANGE_OPTIONS.map((d) => (
        <button
          key={d}
          type="button"
          className="range-opt"
          aria-pressed={value === d}
          disabled={disabled}
          onClick={() => onChange(d)}
        >
          {d}d
        </button>
      ))}
    </div>
  );
}
