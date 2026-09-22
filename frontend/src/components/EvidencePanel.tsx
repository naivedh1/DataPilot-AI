/**
 * The evidence behind an answer: confidence, the checks that ran, and the
 * diagnostic trail when there was one.
 *
 * This is the part of the product that distinguishes it from a chat box over
 * a database. The number is the easy half; showing why it can be trusted, and
 * saying plainly when it cannot, is the half that matters.
 *
 * Two rules the components below follow:
 *
 * - A skipped check renders as skipped, never as a tick. "Passed" for
 *   something that never ran is the exact overstatement this panel exists to
 *   prevent.
 * - Low confidence is shown at the same weight as high. Burying it would
 *   defeat the purpose of computing it.
 */

import { useState } from "react";

import { Badge, Button, Card, CardHeader } from "@/components/primitives";
import { Icon } from "@/components/Icon";
import { cn } from "@/lib/cn";
import { formatValue } from "@/lib/format";
import type {
  CheckStatus,
  Confidence,
  ConfidenceLevel,
  Investigation,
  Validation,
} from "@/types/api";

/* -------------------------------------------------------------------------- */
/* Confidence                                                                  */
/* -------------------------------------------------------------------------- */

const LEVEL_LABEL: Record<ConfidenceLevel, string> = {
  high: "High confidence",
  medium: "Medium confidence",
  low: "Low confidence",
  insufficient_data: "Insufficient data",
};

const LEVEL_TONE: Record<ConfidenceLevel, "positive" | "warning" | "danger" | "neutral"> = {
  high: "positive",
  medium: "warning",
  low: "danger",
  insufficient_data: "neutral",
};

/** A short, human label for a signal name from the backend. */
const SIGNAL_LABEL: Record<string, string> = {
  run_completed: "Run completed",
  answerable_from_warehouse: "Answerable from the data",
  result_has_rows: "Result has rows",
  question_unambiguous: "Question unambiguous",
  metric_defined: "Metric is defined",
  sql_correct_first_time: "SQL correct first time",
  result_checks_passed: "Result checks passed",
  figures_reconcile: "Figures reconcile",
};

function humanise(name: string): string {
  return (
    SIGNAL_LABEL[name] ??
    name.replace(/_/g, " ").replace(/^./, (character) => character.toUpperCase())
  );
}

export function ConfidencePanel({ confidence }: { confidence: Confidence }) {
  const [open, setOpen] = useState(false);

  // The signals that actually set the level — the reason, not the full list.
  const limiting = confidence.signals.filter(
    (signal) => signal.ceiling === confidence.level,
  );

  return (
    <div className="rounded-card border border-subtle bg-surface px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <Badge tone={LEVEL_TONE[confidence.level]}>
            {LEVEL_LABEL[confidence.level]}
          </Badge>
          <span className="text-xs text-faint">
            assessed from the run, not reported by the model
          </span>
        </div>
        <Button
          variant="ghost"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
        >
          {open ? "Hide" : "Show"} signals
          <Icon.Chevron open={open} />
        </Button>
      </div>

      <p className="mt-2.5 text-sm leading-relaxed text-muted">
        {confidence.rationale}
      </p>

      {open && (
        <ul className="mt-3 space-y-1.5 border-t border-subtle pt-3">
          {confidence.signals.map((signal) => {
            const isLimiting = limiting.includes(signal);
            return (
              <li key={signal.name} className="flex items-start gap-2.5 text-xs">
                <span
                  className={cn(
                    "mt-0.5 shrink-0 font-medium",
                    isLimiting ? "text-warning" : "text-positive",
                  )}
                  aria-hidden
                >
                  {isLimiting ? "!" : "+"}
                </span>
                <span className="min-w-0">
                  <span
                    className={cn(
                      "font-medium",
                      isLimiting ? "text-content" : "text-muted",
                    )}
                  >
                    {humanise(signal.name)}
                  </span>
                  <span className="text-faint"> — {signal.detail}</span>
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Validation                                                                  */
/* -------------------------------------------------------------------------- */

const CHECK_MARK: Record<CheckStatus, string> = {
  passed: "✓",
  failed: "✕",
  skipped: "–",
};

const CHECK_CLASS: Record<CheckStatus, string> = {
  passed: "text-positive",
  failed: "text-danger",
  skipped: "text-faint",
};

const CHECK_LABEL: Record<string, string> = {
  result_not_empty: "Result is not empty",
  non_negative_measures: "No negative counts or amounts",
  percentages_within_bounds: "Percentages within 0–100",
  measures_not_all_null: "No entirely empty measure",
  complete_result_set: "Result not truncated",
  group_reconciliation: "Parts reconcile to the total",
};

export function ValidationPanel({ validation }: { validation: Validation }) {
  const failed = validation.checks.filter((check) => check.status === "failed");
  const skipped = validation.checks.filter((check) => check.status === "skipped");
  const passed = validation.checks.filter((check) => check.status === "passed");

  return (
    <Card>
      <CardHeader
        title="Validation"
        subtitle={
          failed.length > 0
            ? `${failed.length} check${failed.length === 1 ? "" : "s"} failed`
            : `${passed.length} passed, ${skipped.length} not applicable`
        }
        icon={<Icon.Database />}
        action={
          <Badge
            tone={
              validation.status === "failed"
                ? "danger"
                : validation.status === "passed"
                  ? "positive"
                  : "neutral"
            }
          >
            {validation.status === "not_verified" ? "not verified" : validation.status}
          </Badge>
        }
      />
      <ul className="space-y-1.5 px-5 py-4">
        {validation.checks.map((check) => (
          <li key={check.name} className="flex items-start gap-2.5 text-sm">
            <span
              className={cn("mt-px shrink-0 font-semibold", CHECK_CLASS[check.status])}
              aria-hidden
            >
              {CHECK_MARK[check.status]}
            </span>
            <span className="min-w-0">
              <span
                className={cn(
                  check.status === "skipped" ? "text-faint" : "text-content",
                )}
              >
                {CHECK_LABEL[check.name] ?? humanise(check.name)}
              </span>
              {check.detail && (
                <span className="text-faint"> — {check.detail}</span>
              )}
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

/* -------------------------------------------------------------------------- */
/* Diagnostic investigation                                                    */
/* -------------------------------------------------------------------------- */

function ContributionBar({ share }: { share: number }) {
  // Shares can exceed 100% when one group moves further than the net change,
  // so the bar is clamped while the printed figure is not.
  const width = Math.min(Math.abs(share) * 100, 100);
  const opposing = share < 0;
  return (
    <span className="block h-1.5 w-full overflow-hidden rounded-full bg-overlay">
      <span
        className={cn("block h-full rounded-full", opposing ? "bg-warning" : "bg-accent")}
        style={{ width: `${width}%` }}
      />
    </span>
  );
}

export function InvestigationPanel({
  investigation,
}: {
  investigation: Investigation;
}) {
  const [openStep, setOpenStep] = useState<string | null>(null);
  const { plan, contributors, change, percent_change: percentChange } = investigation;

  const current = plan.current_period_display ?? plan.current_period;
  const comparison = plan.comparison_period_display ?? plan.comparison_period;
  const failedSteps = investigation.steps.filter((step) => step.error);

  return (
    <Card>
      <CardHeader
        title="How this was investigated"
        subtitle={`${plan.metric} · ${current} vs ${comparison} · ${investigation.steps.length} queries`}
        icon={<Icon.Sparkle />}
        action={
          investigation.reconciled === true ? (
            <Badge tone="positive">reconciled</Badge>
          ) : investigation.reconciled === false ? (
            <Badge tone="danger">does not reconcile</Badge>
          ) : (
            <Badge tone="neutral">no change to attribute</Badge>
          )
        }
      />

      <div className="space-y-4 px-5 py-4">
        {/* The headline movement. */}
        <div className="grid grid-cols-3 gap-3 text-sm">
          {[
            { label: comparison, value: investigation.previous_value },
            { label: current, value: investigation.current_value },
            { label: "Change", value: change },
          ].map((item, index) => (
            <div key={item.label}>
              <p className="truncate text-xs uppercase tracking-wide text-faint">
                {item.label}
              </p>
              <p
                className={cn(
                  "mt-1 font-semibold tabular-nums",
                  index === 2 && (change < 0 ? "text-danger" : "text-positive"),
                )}
              >
                {index === 2 && change > 0 ? "+" : ""}
                {formatValue(item.value, "currency")}
                {index === 2 && percentChange !== null && (
                  <span className="ml-1.5 text-xs font-normal text-faint">
                    ({percentChange > 0 ? "+" : ""}
                    {percentChange.toFixed(1)}%)
                  </span>
                )}
              </p>
            </div>
          ))}
        </div>

        {/* Who moved. */}
        {contributors.length > 0 && (
          <div className="space-y-2.5 border-t border-subtle pt-4">
            <p className="text-xs font-medium uppercase tracking-wide text-faint">
              Contribution to the change
            </p>
            {contributors.map((contributor) => (
              <div key={`${contributor.dimension}-${contributor.label}`}>
                <div className="flex items-baseline justify-between gap-3 text-sm">
                  <span className="min-w-0 truncate text-content">
                    {contributor.label}
                    <span className="ml-1.5 text-xs text-faint">
                      {contributor.dimension}
                    </span>
                  </span>
                  <span className="shrink-0 tabular-nums text-muted">
                    {formatValue(contributor.change, "currency")}
                    <span className="ml-1.5 text-xs text-faint">
                      {(contributor.share_of_change * 100).toFixed(1)}%
                    </span>
                  </span>
                </div>
                <div className="mt-1">
                  <ContributionBar share={contributor.share_of_change} />
                </div>
              </div>
            ))}
            <p className="text-xs text-faint">
              Amber bars moved against the overall change, partly offsetting it.
            </p>
          </div>
        )}

        {/* Every query that ran, inspectable. */}
        <div className="space-y-1 border-t border-subtle pt-4">
          <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-faint">
            Queries run
          </p>
          {investigation.steps.map((step) => (
            <div key={step.name}>
              <button
                type="button"
                onClick={() =>
                  setOpenStep((current) => (current === step.name ? null : step.name))
                }
                aria-expanded={openStep === step.name}
                className={cn(
                  "flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left",
                  "text-sm transition-colors hover:bg-raised",
                )}
              >
                <span className="shrink-0">
                  <Icon.Chevron open={openStep === step.name} />
                </span>
                <span className="min-w-0 flex-1 truncate">
                  <span className="text-content">{step.purpose}</span>
                </span>
                {step.error ? (
                  <Badge tone="danger">failed</Badge>
                ) : (
                  <span className="shrink-0 text-xs tabular-nums text-faint">
                    {step.rows.length} row{step.rows.length === 1 ? "" : "s"}
                  </span>
                )}
              </button>
              {openStep === step.name && (
                <pre className="mx-2 mt-1 overflow-x-auto rounded-lg border border-subtle bg-canvas px-3 py-2.5 text-xs leading-relaxed text-muted">
                  <code>{step.error || step.sql}</code>
                </pre>
              )}
            </div>
          ))}
        </div>

        {failedSteps.length > 0 && (
          <p className="text-xs text-warning">
            {failedSteps.length} step{failedSteps.length === 1 ? "" : "s"} did not
            run, so the attribution above may be incomplete.
          </p>
        )}

        {investigation.caveats.length > 0 && (
          <ul className="space-y-1.5 border-t border-subtle pt-4">
            {investigation.caveats.map((caveat) => (
              <li key={caveat} className="flex items-start gap-2 text-xs text-warning">
                <span className="mt-px shrink-0" aria-hidden>
                  !
                </span>
                <span>{caveat}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Card>
  );
}
