/**
 * Plotly renderer for a backend chart specification.
 *
 * A thin locally-owned wrapper rather than `react-plotly.js`: the wrapper is
 * about sixty lines, and owning it avoids a lightly-maintained dependency
 * sitting directly in the render path.
 *
 * **Plotly is loaded dynamically.** It is roughly 4.5 MB minified — larger than
 * the entire rest of the application — and a user who never renders a chart
 * should never download it. The `import()` puts it in its own chunk that
 * arrives when the first chart mounts.
 *
 * The backend decides *which* chart to draw (from the result's shape); this
 * decides *how* it looks. Keeping the choice server-side makes it testable
 * without a browser; keeping the styling here keeps design tokens in one place.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import type { Chart } from "@/types/api";

type PlotlyModule = typeof import("plotly.js-dist-min");

/** Module-level cache, so the chunk is fetched once per session, not per chart. */
let plotlyPromise: Promise<PlotlyModule> | null = null;

function loadPlotly(): Promise<PlotlyModule> {
  plotlyPromise ??= import("plotly.js-dist-min");
  return plotlyPromise;
}

/** Resolved from the CSS custom properties so charts match the app palette. */
function seriesColors(): string[] {
  const styles = getComputedStyle(document.documentElement);
  return Array.from({ length: 8 }, (_, index) =>
    styles.getPropertyValue(`--color-series-${index + 1}`).trim(),
  ).filter(Boolean);
}

function cssVar(name: string, fallback: string): string {
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return value || fallback;
}

interface Props {
  chart: Chart;
  height?: number;
}

export function ChartView({ chart, height = 340 }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);

  const { traces, layout } = useMemo(() => {
    const colors = seriesColors();
    const text = cssVar("--color-muted", "#9aa4b2");
    const grid = cssVar("--color-subtle", "#2c3340");

    const traces = chart.series.map((series, index) => {
      const color = colors[index % colors.length];
      const base = {
        name: series.name,
        marker: { color },
        hovertemplate: `<b>%{x}</b><br>${series.name}: %{y:,.2f}<extra></extra>`,
      };

      switch (chart.chart_type) {
        case "line":
          return {
            ...base,
            type: "scatter" as const,
            mode: "lines+markers" as const,
            x: chart.x_values,
            y: series.values,
            line: { color, width: 2.5, shape: "spline" as const, smoothing: 0.4 },
            marker: { color, size: 5 },
          };
        case "scatter":
          return {
            ...base,
            type: "scatter" as const,
            mode: "markers" as const,
            x: chart.x_values,
            y: series.values,
            marker: { color, size: 8, opacity: 0.75 },
          };
        case "histogram":
          return {
            ...base,
            type: "histogram" as const,
            x: chart.x_values,
            marker: { color, opacity: 0.85 },
            hovertemplate: "%{x}<br>count: %{y}<extra></extra>",
          };
        case "pie":
          return {
            type: "pie" as const,
            labels: chart.x_values,
            values: series.values,
            marker: { colors },
            textinfo: "label+percent" as const,
            hovertemplate:
              "<b>%{label}</b><br>%{value:,.2f} (%{percent})<extra></extra>",
            hole: 0.45,
          };
        default:
          return {
            ...base,
            type: "bar" as const,
            x: chart.x_values,
            y: series.values,
          };
      }
    });

    const layout: Record<string, unknown> = {
      // Transparent so the card background shows through rather than the chart
      // sitting on a mismatched rectangle.
      paper_bgcolor: "rgba(0,0,0,0)",
      plot_bgcolor: "rgba(0,0,0,0)",
      font: {
        family: cssVar("--font-sans", "Inter, sans-serif"),
        size: 12,
        color: text,
      },
      margin: { l: 64, r: 20, t: 12, b: 56 },
      height,
      showlegend: chart.series.length > 1 || chart.chart_type === "pie",
      legend: {
        orientation: "h",
        y: -0.22,
        x: 0,
        font: { size: 11 },
        bgcolor: "rgba(0,0,0,0)",
      },
      xaxis: {
        title: { text: chart.x_label, font: { size: 11 }, standoff: 12 },
        gridcolor: grid,
        zerolinecolor: grid,
        linecolor: grid,
        tickfont: { size: 11 },
        automargin: true,
      },
      yaxis: {
        title: { text: chart.y_label, font: { size: 11 }, standoff: 12 },
        gridcolor: grid,
        zerolinecolor: grid,
        linecolor: grid,
        tickfont: { size: 11 },
        automargin: true,
        // Compact ticks: full currency values overlap at any realistic width.
        ...(chart.value_format === "currency" ? { tickformat: "~s" } : {}),
        ...(chart.value_format === "percent" ? { ticksuffix: "%" } : {}),
      },
      ...(chart.chart_type === "grouped_bar" ? { barmode: "group" } : {}),
      hoverlabel: {
        bgcolor: cssVar("--color-overlay", "#323a48"),
        bordercolor: grid,
        font: { size: 12, color: cssVar("--color-content", "#f5f6f8") },
      },
    };

    return { traces, layout };
  }, [chart, height]);

  useEffect(() => {
    const node = container.current;
    if (!node) return;

    let disposed = false;
    let plotly: PlotlyModule | null = null;

    loadPlotly()
      .then((module) => {
        // The component may have unmounted while the chunk was in flight.
        if (disposed || !container.current) return;
        plotly = module;
        return module.newPlot(
          node,
          traces as Parameters<PlotlyModule["newPlot"]>[1],
          layout,
          { displayModeBar: false, responsive: true },
        );
      })
      .catch(() => {
        // A failed chunk fetch must degrade to a readable message, not a blank
        // card. The data table below still shows every figure.
        if (!disposed) setFailed(true);
      });

    return () => {
      disposed = true;
      plotly?.purge(node);
    };
  }, [traces, layout]);

  if (failed) {
    return (
      <p className="px-2 py-8 text-center text-sm text-muted">
        The chart could not be loaded. The figures are in the data table below.
      </p>
    );
  }

  return (
    <div
      ref={container}
      role="img"
      aria-label={`${chart.chart_type} chart: ${chart.title}. ${chart.x_label} against ${chart.y_label}.`}
      className="w-full"
      style={{ minHeight: height }}
    />
  );
}
