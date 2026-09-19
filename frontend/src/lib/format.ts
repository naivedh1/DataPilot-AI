/**
 * Value formatting.
 *
 * The backend tags each result with a `value_format` inferred from its column
 * name, because PostgreSQL reports NUMERIC for revenue, a percentage and a
 * count alike. Formatting is presentation only — it never changes a figure, and
 * the underlying value stays visible in the data table.
 */

export type ValueFormat = "currency" | "percent" | "number";

const currency = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const currencyPrecise = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const compact = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const plain = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

export function formatValue(
  value: unknown,
  format: ValueFormat = "number",
): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";

  const numeric = typeof value === "number" ? value : Number(value);
  if (typeof value === "string" && Number.isNaN(numeric)) return value;
  if (Number.isNaN(numeric)) return String(value);

  switch (format) {
    case "currency":
      // Large sums are unreadable in full; small ones lose meaning if rounded.
      return Math.abs(numeric) >= 100_000
        ? currency.format(numeric)
        : currencyPrecise.format(numeric);
    case "percent":
      return `${plain.format(numeric)}%`;
    default:
      return Math.abs(numeric) >= 1_000_000
        ? compact.format(numeric)
        : plain.format(numeric);
  }
}

/** Axis ticks: always compact, so labels never overlap. */
export function formatAxis(value: number, format: ValueFormat): string {
  if (format === "percent") return `${plain.format(value)}%`;
  if (format === "currency") return `$${compact.format(value)}`;
  return compact.format(value);
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

/** Detects an ISO date so the data table can render it readably. */
export function looksLikeDate(value: unknown): boolean {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value);
}

export function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (looksLikeDate(value)) {
    const date = new Date(value as string);
    if (!Number.isNaN(date.getTime())) {
      return date.toLocaleDateString("en-US", {
        year: "numeric",
        month: "short",
        day: "numeric",
      });
    }
  }
  if (typeof value === "number") return plain.format(value);
  return String(value);
}
