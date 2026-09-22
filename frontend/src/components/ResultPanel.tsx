/**
 * One answered question: headline, confidence, chart, insights, validation,
 * SQL, data and metadata.
 *
 * The ordering is deliberate — answer first, then how much weight it carries,
 * then the evidence for it. A business user reads the top; anyone who wants to
 * audit the number can expand the checks, the SQL and the rows underneath.
 */

import { useMemo, useState } from "react";

import { ChartView } from "@/components/ChartView";
import {
  ConfidencePanel,
  InvestigationPanel,
  ValidationPanel,
} from "@/components/EvidencePanel";
import { Badge, Button, Card, CardHeader } from "@/components/primitives";
import { cn } from "@/lib/cn";
import { Icon } from "@/components/Icon";
import { formatCell, formatDuration, formatValue } from "@/lib/format";
import type { Insight, QueryResponse } from "@/types/api";

/* -------------------------------------------------------------------------- */
/* KPI row                                                                     */
/* -------------------------------------------------------------------------- */

/**
 * Surfaces the headline figures when a result is small enough to read at a
 * glance. A tile for every row of a 200-row result would be noise, so this only
 * fires for the single-value and few-category shapes where it genuinely helps.
 */
function KpiRow({ response }: { response: QueryResponse }) {
  const tiles = useMemo(() => {
    const { columns, rows, chart } = response;
    if (rows.length === 0 || rows.length > 4) return [];

    const numericIndex = columns.findIndex((_, index) =>
      rows.every((row) => row[index] === null || typeof row[index] === "number"),
    );
    if (numericIndex === -1) return [];

    const labelIndex = columns.findIndex(
      (_, index) => index !== numericIndex && typeof rows[0]?.[index] === "string",
    );

    return rows.map((row) => ({
      label:
        labelIndex >= 0
          ? String(row[labelIndex])
          : (columns[numericIndex] ?? "Value").replace(/_/g, " "),
      value: formatValue(row[numericIndex], chart?.value_format ?? "number"),
    }));
  }, [response]);

  if (tiles.length === 0) return null;

  return (
    <div
      className={cn(
        "grid gap-3",
        tiles.length === 1 ? "grid-cols-1" : "grid-cols-2 lg:grid-cols-4",
      )}
    >
      {tiles.map((tile) => (
        <div
          key={tile.label}
          className="rounded-card border border-subtle bg-surface px-4 py-3.5"
        >
          <p className="truncate text-xs font-medium uppercase tracking-wide text-faint">
            {tile.label}
          </p>
          <p className="mt-1.5 text-2xl font-semibold tabular-nums tracking-tight text-content">
            {tile.value}
          </p>
        </div>
      ))}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Insights                                                                    */
/* -------------------------------------------------------------------------- */

const INSIGHT_TONE = {
  finding: { tone: "neutral" as const, label: "Finding" },
  trend: { tone: "accent" as const, label: "Trend" },
  concern: { tone: "warning" as const, label: "Concern" },
  recommendation: { tone: "positive" as const, label: "Suggestion" },
};

function InsightList({
  insights,
  caveats,
}: {
  insights: Insight[];
  caveats: string[];
}) {
  if (insights.length === 0 && caveats.length === 0) return null;

  return (
    <Card>
      <CardHeader title="Insights" icon={<Icon.Sparkle />} />
      <div className="space-y-3 px-5 py-4">
        {insights.map((insight, index) => {
          const meta = INSIGHT_TONE[insight.kind];
          return (
            <div key={index} className="flex gap-3">
              {/* Recommendations are labelled as opinions, findings as
                  observations. Collapsing the distinction is how an analysis
                  tool starts asserting things the data does not say. */}
              <Badge tone={meta.tone} className="mt-0.5 shrink-0">
                {meta.label}
              </Badge>
              <p className="text-sm leading-relaxed text-content">
                {insight.text}
              </p>
            </div>
          );
        })}

        {caveats.length > 0 && (
          <div className="mt-4 border-t border-subtle pt-3">
            <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-faint">
              Caveats
            </p>
            <ul className="space-y-1">
              {caveats.map((caveat, index) => (
                <li key={index} className="text-xs leading-relaxed text-muted">
                  • {caveat}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Data table                                                                  */
/* -------------------------------------------------------------------------- */

const PAGE_SIZE = 25;

function DataTable({ response }: { response: QueryResponse }) {
  const [showAll, setShowAll] = useState(false);
  const rows = showAll ? response.rows : response.rows.slice(0, PAGE_SIZE);

  if (response.columns.length === 0) return null;

  return (
    <Card>
      <CardHeader
        title="Data"
        subtitle={`${response.execution.row_count.toLocaleString()} row${
          response.execution.row_count === 1 ? "" : "s"
        }${response.execution.truncated ? " (truncated by the row cap)" : ""}`}
        icon={<Icon.Table />}
        action={
          response.rows.length > PAGE_SIZE && (
            <Button variant="subtle" onClick={() => setShowAll((value) => !value)}>
              {showAll ? "Show less" : `Show all ${response.rows.length}`}
            </Button>
          )
        }
      />
      <div className="max-h-96 overflow-auto">
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0 z-10 bg-raised">
            <tr>
              {response.columns.map((column) => (
                <th
                  key={column}
                  scope="col"
                  className="border-b border-subtle px-4 py-2.5 text-left text-xs font-semibold uppercase tracking-wide text-muted whitespace-nowrap"
                >
                  {column.replace(/_/g, " ")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr
                key={rowIndex}
                className="border-b border-subtle/50 last:border-0 hover:bg-raised/60"
              >
                {row.map((cell, cellIndex) => (
                  <td
                    key={cellIndex}
                    className={cn(
                      "px-4 py-2 whitespace-nowrap",
                      typeof cell === "number"
                        ? "text-right tabular-nums text-content"
                        : "text-muted",
                    )}
                  >
                    {formatCell(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* SQL and trace                                                               */
/* -------------------------------------------------------------------------- */

function SqlPanel({ response }: { response: QueryResponse }) {
  const [copied, setCopied] = useState(false);
  const rejected = response.attempts.filter((attempt) => !attempt.valid);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(response.sql);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable; the SQL is selectable either way */
    }
  };

  if (!response.sql) return null;

  return (
    <Card>
      <CardHeader
        title="SQL"
        subtitle={response.reasoning_summary || undefined}
        icon={<Icon.Code />}
        action={
          <Button variant="subtle" onClick={copy}>
            {copied ? "Copied" : "Copy"}
          </Button>
        }
      />
      <pre className="overflow-x-auto px-5 py-4 font-mono text-xs leading-relaxed text-muted">
        <code>{response.sql}</code>
      </pre>

      {rejected.length > 0 && (
        /* Rejected attempts are shown, not hidden. They are the clearest
           evidence that validation is real rather than decorative. */
        <div className="border-t border-subtle px-5 py-3">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-faint">
            Rejected before execution ({rejected.length})
          </p>
          {rejected.map((attempt, index) => (
            <div key={index} className="mb-2 last:mb-0">
              <code className="block truncate font-mono text-xs text-faint">
                {attempt.sql.replace(/\s+/g, " ").slice(0, 110)}
              </code>
              <p className="mt-0.5 text-xs text-danger">{attempt.error}</p>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

function ExecutionPanel({ response }: { response: QueryResponse }) {
  const [open, setOpen] = useState(false);
  const { execution, trace } = response;

  return (
    <Card>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center justify-between px-5 py-3 text-left hover:bg-raised/50"
      >
        <span className="flex items-center gap-2.5 text-sm text-muted">
          <Icon.Clock />
          <span className="tabular-nums">
            {formatDuration(execution.total_ms)} total ·{" "}
            {formatDuration(execution.execution_ms)} in database ·{" "}
            {execution.row_count.toLocaleString()} rows
          </span>
        </span>
        <span className="flex items-center gap-2">
          {execution.retries > 0 && (
            <Badge tone="warning" title="SQL was repaired and retried">
              {execution.retries} {execution.retries === 1 ? "retry" : "retries"}
            </Badge>
          )}
          <Icon.Chevron open={open} />
        </span>
      </button>

      {open && (
        <div className="border-t border-subtle px-5 py-4">
          <dl className="mb-4 grid grid-cols-2 gap-x-6 gap-y-2 text-xs sm:grid-cols-3">
            <div>
              <dt className="text-faint">Intent</dt>
              <dd className="text-content">{execution.intent || "—"}</dd>
            </div>
            <div>
              <dt className="text-faint">Tables</dt>
              <dd className="text-content">
                {execution.tables_used.join(", ") || "—"}
              </dd>
            </div>
            <div>
              <dt className="text-faint">Model calls</dt>
              <dd className="text-content tabular-nums">{execution.llm_calls}</dd>
            </div>
          </dl>

          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-faint">
            Agent pipeline
          </p>
          <ol className="space-y-1">
            {trace.map((node, index) => (
              <li
                key={index}
                className="flex items-center justify-between gap-3 text-xs"
              >
                <span className="flex items-center gap-2">
                  <span
                    className={cn(
                      "size-1.5 rounded-full",
                      node.status === "ok" ? "bg-positive" : "bg-danger",
                    )}
                    aria-hidden="true"
                  />
                  <span className="text-content">{node.node.replace(/_/g, " ")}</span>
                  {node.detail && (
                    <span className="truncate text-faint">— {node.detail}</span>
                  )}
                </span>
                <span className="shrink-0 tabular-nums text-faint">
                  {node.duration_ms.toFixed(0)} ms
                </span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Panel                                                                       */
/* -------------------------------------------------------------------------- */

export function ResultPanel({ response }: { response: QueryResponse }) {
  const hasChart =
    response.chart !== null &&
    response.chart.chart_type !== "table" &&
    response.rows.length > 0;

  return (
    <div className="animate-in space-y-4">
      {/* The answer, first. */}
      <div className="rounded-card border border-accent-soft/30 bg-accent-soft/10 px-5 py-4">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 shrink-0 text-accent-text">
            <Icon.Sparkle />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-[15px] leading-relaxed font-medium text-content">
              {response.answer || response.error || "No answer was produced."}
            </p>
            {response.execution.llm_simulated && (
              /* Stated plainly rather than buried: a rule-based answer must
                 never be mistaken for a model-generated one. */
              <p className="mt-2 text-xs text-warning">
                Generated by the deterministic offline baseline — no language
                model is configured. SQL and figures are real; narrative insight
                is not generated.
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Confidence sits directly under the answer, before the evidence for
          it. A reader who stops here has still seen how much weight the
          number carries. */}
      {response.confidence && <ConfidencePanel confidence={response.confidence} />}

      <KpiRow response={response} />

      {response.investigation && (
        <InvestigationPanel investigation={response.investigation} />
      )}

      {hasChart && response.chart && (
        <Card>
          <CardHeader
            title={response.chart.title}
            subtitle={response.chart.rationale}
            icon={<Icon.Chart />}
          />
          <div className="px-3 py-4">
            <ChartView chart={response.chart} />
          </div>
        </Card>
      )}

      <InsightList insights={response.insights} caveats={response.caveats} />
      {response.validation && <ValidationPanel validation={response.validation} />}
      <SqlPanel response={response} />
      <DataTable response={response} />
      <ExecutionPanel response={response} />
    </div>
  );
}
