/**
 * Navigation rail: conversations, schema reference, and service status.
 *
 * Collapses to an overlay below `lg` rather than disappearing, so the app stays
 * usable on a narrow screen instead of only looking right on a wide one.
 */

import { useState } from "react";

import { Badge, Button } from "@/components/primitives";
import { cn } from "@/lib/cn";
import { Icon } from "@/components/Icon";
import type { HealthResponse, SchemaResponse, Turn } from "@/types/api";

interface Props {
  turns: Turn[];
  health: HealthResponse | null;
  schema: SchemaResponse | null;
  onNewConversation: () => void;
  onSelectTurn: (id: string) => void;
  open: boolean;
  onClose: () => void;
}

export function Sidebar({
  turns,
  health,
  schema,
  onNewConversation,
  onSelectTurn,
  open,
  onClose,
}: Props) {
  const [showSchema, setShowSchema] = useState(false);
  const answered = turns.filter((turn) => turn.response !== null);

  return (
    <>
      {/* Scrim, mobile only. */}
      {open && (
        <div
          className="fixed inset-0 z-30 bg-canvas/70 lg:hidden"
          onClick={onClose}
          aria-hidden="true"
        />
      )}

      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-72 flex-col border-r border-subtle bg-surface",
          "transition-transform lg:static lg:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
        aria-label="Conversations and schema"
      >
        {/* Brand */}
        <div className="flex items-center gap-2.5 border-b border-subtle px-4 py-4">
          <div className="flex size-7 items-center justify-center rounded-lg bg-accent text-canvas">
            <Icon.Sparkle />
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold tracking-tight text-content">
              DataPilot AI
            </p>
            <p className="truncate text-[11px] text-faint">
              Agentic data analyst
            </p>
          </div>
        </div>

        <div className="px-3 py-3">
          <Button
            variant="subtle"
            onClick={onNewConversation}
            className="w-full justify-start"
          >
            <Icon.Plus />
            New conversation
          </Button>
        </div>

        {/* History */}
        <div className="min-h-0 flex-1 overflow-y-auto px-3">
          <p className="px-2 pb-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint">
            This conversation
          </p>
          {answered.length === 0 ? (
            <p className="px-2 py-2 text-xs leading-relaxed text-faint">
              Questions you ask will appear here.
            </p>
          ) : (
            <ul className="space-y-0.5">
              {answered.map((turn) => (
                <li key={turn.id}>
                  <button
                    type="button"
                    onClick={() => onSelectTurn(turn.id)}
                    className="w-full rounded-lg px-2 py-1.5 text-left text-xs text-muted hover:bg-raised hover:text-content"
                  >
                    <span className="line-clamp-2 leading-snug">
                      {turn.question}
                    </span>
                    {turn.response && (
                      <span className="mt-0.5 block text-[11px] tabular-nums text-faint">
                        {turn.response.execution.row_count.toLocaleString()} rows
                        · {Math.round(turn.response.execution.total_ms)} ms
                      </span>
                    )}
                  </button>
                </li>
              ))}
            </ul>
          )}

          {/* Schema reference — answers "what can I ask?" without guessing. */}
          <div className="mt-4">
            <button
              type="button"
              onClick={() => setShowSchema((value) => !value)}
              aria-expanded={showSchema}
              className="flex w-full items-center gap-1.5 rounded-lg px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wide text-faint hover:text-muted"
            >
              <Icon.Chevron open={showSchema} />
              Available data
            </button>
            {showSchema && schema && (
              <ul className="mt-1 space-y-2 px-2 pb-4">
                {schema.tables.map((table) => (
                  <li key={table.name}>
                    <p className="font-mono text-xs text-content">{table.name}</p>
                    <p className="mt-0.5 text-[11px] leading-snug text-faint">
                      {table.columns.length} columns
                    </p>
                  </li>
                ))}
                <li className="border-t border-subtle pt-2">
                  <p className="text-[11px] text-faint">
                    {schema.metrics.length} defined metrics
                  </p>
                </li>
              </ul>
            )}
          </div>
        </div>

        {/* Status */}
        <div className="border-t border-subtle px-4 py-3">
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-2 text-xs text-muted">
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  health?.status === "ok" ? "bg-positive" : "bg-danger",
                )}
                aria-hidden="true"
              />
              {health ? `v${health.version}` : "Connecting…"}
            </span>
            {health && !health.llm_configured && (
              <Badge
                tone="warning"
                title="No GEMINI_API_KEY is configured. The deterministic offline baseline is answering."
              >
                Offline mode
              </Badge>
            )}
          </div>
        </div>
      </aside>
    </>
  );
}
