/**
 * Application shell.
 *
 * Holds the conversation in component state. That is the right scope here: the
 * server owns the durable history and the thread is a single linear list, so a
 * state library would add indirection without removing any.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { QueryComposer, ThinkingIndicator } from "@/components/QueryComposer";
import { ResultPanel } from "@/components/ResultPanel";
import { Sidebar } from "@/components/Sidebar";
import { Button, EmptyState, ErrorState } from "@/components/primitives";
import { cn } from "@/lib/cn";
import { Icon } from "@/components/Icon";
import { ApiError, api } from "@/lib/api";
import type { HealthResponse, SchemaResponse, Turn } from "@/types/api";

export function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [pending, setPending] = useState(false);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [schema, setSchema] = useState<SchemaResponse | null>(null);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  const endOfThread = useRef<HTMLDivElement>(null);

  // Reference data, fetched once. Failures are non-fatal: the app still works
  // without the schema panel or the suggestion chips.
  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
    api.schema().then(setSchema).catch(() => setSchema(null));
    api
      .suggestions()
      .then((response) => setSuggestions(response.suggestions))
      .catch(() => setSuggestions([]));
  }, []);

  useEffect(() => {
    endOfThread.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, pending]);

  const ask = useCallback(
    async (question: string) => {
      const id = crypto.randomUUID();
      setTurns((current) => [
        ...current,
        { id, question, response: null, error: null },
      ]);
      setPending(true);
      setSidebarOpen(false);

      try {
        const response = await api.query(question, conversationId);
        setConversationId(response.conversation_id);
        setTurns((current) =>
          current.map((turn) => (turn.id === id ? { ...turn, response } : turn)),
        );
      } catch (error) {
        const message =
          error instanceof ApiError
            ? error.message
            : "Something went wrong. Please try again.";
        setTurns((current) =>
          current.map((turn) => (turn.id === id ? { ...turn, error: message } : turn)),
        );
      } finally {
        setPending(false);
      }
    },
    [conversationId],
  );

  const startNewConversation = useCallback(() => {
    setTurns([]);
    setConversationId(undefined);
    setSidebarOpen(false);
  }, []);

  const scrollToTurn = useCallback((id: string) => {
    document.getElementById(`turn-${id}`)?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
    setSidebarOpen(false);
  }, []);

  const isEmpty = turns.length === 0;

  return (
    <div className="flex h-full overflow-hidden bg-canvas">
      <Sidebar
        turns={turns}
        health={health}
        schema={schema}
        onNewConversation={startNewConversation}
        onSelectTurn={scrollToTurn}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />

      <main className="flex min-w-0 flex-1 flex-col">
        {/* Top bar */}
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-subtle px-4 py-3 lg:px-8">
          <div className="flex min-w-0 items-center gap-3">
            <Button
              onClick={() => setSidebarOpen(true)}
              className="lg:hidden"
              aria-label="Open navigation"
            >
              <svg viewBox="0 0 16 16" fill="currentColor" className="size-4">
                <path d="M2 4h12v1.5H2V4Zm0 3.25h12v1.5H2v-1.5ZM2 10.5h12V12H2v-1.5Z" />
              </svg>
            </Button>
            <div className="min-w-0">
              <h1 className="truncate text-sm font-semibold tracking-tight text-content">
                Query workspace
              </h1>
              <p className="truncate text-xs text-faint">
                Ask in plain English — every answer ships with its SQL
              </p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            <span className="hidden items-center gap-1.5 text-xs text-faint sm:flex">
              <Icon.Database />
              {health?.database_configured ? "Warehouse connected" : "No warehouse"}
            </span>
            <span
              className={cn(
                "size-2 rounded-full",
                health?.status === "ok" ? "bg-positive" : "bg-danger",
              )}
              title={health?.status === "ok" ? "Service healthy" : "Service unavailable"}
            />
          </div>
        </header>

        {/* Thread */}
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-4xl px-4 py-6 lg:px-8">
            {isEmpty && !pending ? (
              <EmptyState
                icon={
                  <div className="flex size-12 items-center justify-center rounded-2xl bg-accent-soft/20 text-accent-text">
                    <Icon.Sparkle />
                  </div>
                }
                title="Ask your data anything"
                description="DataPilot AI plans the analysis, writes read-only SQL, validates it before it runs, then explains the result. Start with one of these, or type your own."
              >
                <div className="grid gap-2 sm:grid-cols-2">
                  {suggestions.slice(0, 6).map((suggestion) => (
                    <button
                      key={suggestion}
                      type="button"
                      onClick={() => ask(suggestion)}
                      className="rounded-lg border border-subtle bg-surface px-3.5 py-2.5 text-left text-sm text-muted transition-colors hover:border-accent-soft/50 hover:bg-raised hover:text-content"
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </EmptyState>
            ) : (
              <div className="space-y-8">
                {turns.map((turn) => (
                  <article key={turn.id} id={`turn-${turn.id}`} className="space-y-4">
                    <div className="flex justify-end">
                      <p className="max-w-2xl rounded-2xl rounded-br-sm bg-raised px-4 py-2.5 text-sm leading-relaxed text-content">
                        {turn.question}
                      </p>
                    </div>

                    {turn.response && <ResultPanel response={turn.response} />}
                    {turn.error && (
                      <ErrorState
                        message={turn.error}
                        onRetry={() => ask(turn.question)}
                      />
                    )}
                  </article>
                ))}

                {pending && <ThinkingIndicator />}
              </div>
            )}
            <div ref={endOfThread} />
          </div>
        </div>

        {/* Composer */}
        <div className="shrink-0 border-t border-subtle bg-canvas px-4 py-4 lg:px-8">
          <div className="mx-auto w-full max-w-4xl">
            <QueryComposer
              onSubmit={ask}
              disabled={pending}
              placeholder={
                turns.length > 0
                  ? "Ask a follow-up — for example, only the last 6 months"
                  : undefined
              }
            />
          </div>
        </div>
      </main>
    </div>
  );
}
