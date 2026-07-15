import { sparklinePath } from "../lib/trend";

/**
 * A compact, inline SVG sparkline of a daily cost series. Purely decorative
 * (`aria-hidden`) — the delta badge carries the meaning for assistive tech.
 * Renders nothing for an empty series so the caller never shows an empty box.
 * Static (no draw animation) → nothing to suppress for reduced motion.
 */
export function Sparkline({
  values,
  width = 100,
  height = 24,
  className = "sparkline",
}: {
  values: number[];
  width?: number;
  height?: number;
  className?: string;
}) {
  const points = sparklinePath(values, width, height);
  if (!points) return null;
  return (
    <svg
      className={className}
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
    >
      <polyline
        points={points}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
