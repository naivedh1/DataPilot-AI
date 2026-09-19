/**
 * The question input.
 *
 * A textarea rather than an input: analytical questions run long, and a
 * single-line field that scrolls sideways hides what the user typed. Enter
 * submits, Shift+Enter adds a line — the convention people already expect.
 */

import { useEffect, useRef, useState } from "react";

import { cn } from "@/lib/cn";
import { Icon } from "@/components/Icon";

const MAX_LENGTH = 1000;

interface Props {
  onSubmit: (question: string) => void;
  disabled: boolean;
  // `| undefined` is required by exactOptionalPropertyTypes: the caller passes
  // the prop explicitly, sometimes with an undefined value.
  placeholder?: string | undefined;
}

export function QueryComposer({ onSubmit, disabled, placeholder }: Props) {
  const [value, setValue] = useState("");
  const textarea = useRef<HTMLTextAreaElement>(null);

  // Grow with the content up to a ceiling, so long questions stay visible
  // without the box taking over the screen.
  useEffect(() => {
    const node = textarea.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 180)}px`;
  }, [value]);

  const submit = () => {
    const question = value.trim();
    if (!question || disabled) return;
    onSubmit(question);
    setValue("");
  };

  const tooLong = value.length > MAX_LENGTH;

  return (
    <div className="rounded-card border border-subtle bg-surface focus-within:border-accent-soft">
      <textarea
        ref={textarea}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            submit();
          }
        }}
        disabled={disabled}
        rows={1}
        aria-label="Ask a question about your data"
        placeholder={
          placeholder ?? "Ask a business question — for example, revenue by region last quarter"
        }
        className={cn(
          "w-full resize-none bg-transparent px-4 pt-3.5 pb-2 text-sm leading-relaxed",
          "text-content placeholder:text-faint focus:outline-none disabled:opacity-60",
        )}
      />
      <div className="flex items-center justify-between gap-3 px-4 pb-3">
        <p className="text-[11px] text-faint">
          {tooLong ? (
            <span className="text-danger">
              {value.length} / {MAX_LENGTH} characters
            </span>
          ) : (
            <>
              <kbd className="rounded border border-subtle bg-raised px-1 py-0.5 font-sans">
                Enter
              </kbd>{" "}
              to ask ·{" "}
              <kbd className="rounded border border-subtle bg-raised px-1 py-0.5 font-sans">
                Shift+Enter
              </kbd>{" "}
              for a new line
            </>
          )}
        </p>
        <button
          type="button"
          onClick={submit}
          disabled={disabled || !value.trim() || tooLong}
          aria-label="Submit question"
          className={cn(
            "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium",
            "bg-accent text-canvas transition hover:brightness-110",
            "disabled:cursor-not-allowed disabled:opacity-35",
          )}
        >
          <Icon.Send />
          Ask
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Loading state                                                               */
/* -------------------------------------------------------------------------- */

/** The pipeline stages, shown so the wait is legible rather than a blank spinner. */
const STAGES = [
  "Understanding the question",
  "Selecting relevant tables",
  "Writing SQL",
  "Validating for safety",
  "Querying the warehouse",
  "Analysing the result",
];

export function ThinkingIndicator() {
  const [stage, setStage] = useState(0);

  useEffect(() => {
    // Advances on a timer rather than from real node events: the backend is
    // request/response, not streaming. The labels name the stages that genuinely
    // run, and the timing is indicative — it never claims a stage has finished.
    const timer = setInterval(() => {
      setStage((current) => Math.min(current + 1, STAGES.length - 1));
    }, 700);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="animate-in rounded-card border border-subtle bg-surface px-5 py-4">
      <div className="flex items-center gap-3">
        <span className="relative flex size-4 shrink-0 items-center justify-center">
          <span className="absolute size-4 animate-ping rounded-full bg-accent/40" />
          <span className="size-2 rounded-full bg-accent" />
        </span>
        <p className="text-sm text-muted" aria-live="polite">
          {STAGES[stage]}
          <span className="animate-pulse-soft">…</span>
        </p>
      </div>
      <div className="mt-3 flex gap-1" aria-hidden="true">
        {STAGES.map((label, index) => (
          <span
            key={label}
            className={cn(
              "h-0.5 flex-1 rounded-full transition-colors",
              index <= stage ? "bg-accent" : "bg-subtle",
            )}
          />
        ))}
      </div>
    </div>
  );
}
