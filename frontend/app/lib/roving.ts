/**
 * Roving-tabindex target for a horizontal radio group (WAI-ARIA radiogroup
 * pattern). Given the pressed key, the current option index, and the option
 * count, returns the index to move focus/selection to — Arrow keys wrap, Home/End
 * jump to the ends — or `null` for any key the group shouldn't handle.
 */
export function nextRadioIndex(key: string, current: number, count: number): number | null {
  if (count <= 0) return null;
  switch (key) {
    case "ArrowRight":
    case "ArrowDown":
      return (current + 1) % count;
    case "ArrowLeft":
    case "ArrowUp":
      return (current - 1 + count) % count;
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return null;
  }
}
