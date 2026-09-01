/** Alert delay-type keys (analysis/alert_classifier.py) as display text. */
export function delayTypeLabel(type: string): string {
  const words = type.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}
