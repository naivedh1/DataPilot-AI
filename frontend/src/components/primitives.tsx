/**
 * Shared UI primitives.
 *
 * Small and unopinionated on purpose. Every colour comes from a design token,
 * so the palette changes in one place, and every interactive element keeps a
 * visible focus ring.
 */

import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/* -------------------------------------------------------------------------- */
/* Card                                                                        */
/* -------------------------------------------------------------------------- */

interface CardProps {
  children: ReactNode;
  className?: string | undefined;
}

export function Card({ children, className }: CardProps) {
  return (
    <section
      className={cn(
        "rounded-card border border-subtle bg-surface",
        className,
      )}
    >
      {children}
    </section>
  );
}

interface CardHeaderProps {
  title: string;
  // `| undefined` for exactOptionalPropertyTypes — these are passed through
  // from optional values rather than conditionally omitted.
  subtitle?: string | undefined;
  icon?: ReactNode | undefined;
  action?: ReactNode | undefined;
}

export function CardHeader({ title, subtitle, icon, action }: CardHeaderProps) {
  return (
    <header className="flex items-start justify-between gap-3 border-b border-subtle px-5 py-3.5">
      <div className="flex min-w-0 items-center gap-2.5">
        {icon && <span className="shrink-0 text-faint">{icon}</span>}
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold tracking-tight text-content">
            {title}
          </h3>
          {subtitle && (
            <p className="mt-0.5 truncate text-xs text-faint">{subtitle}</p>
          )}
        </div>
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </header>
  );
}

/* -------------------------------------------------------------------------- */
/* Badge                                                                       */
/* -------------------------------------------------------------------------- */

type Tone = "neutral" | "accent" | "positive" | "warning" | "danger";

const TONES: Record<Tone, string> = {
  neutral: "bg-overlay text-muted border-subtle",
  accent: "bg-accent-soft/25 text-accent-text border-accent-soft/40",
  positive: "bg-positive/15 text-positive border-positive/30",
  warning: "bg-warning/15 text-warning border-warning/30",
  danger: "bg-danger/15 text-danger border-danger/30",
};

export function Badge({
  children,
  tone = "neutral",
  className,
  title,
}: {
  children: ReactNode;
  tone?: Tone | undefined;
  className?: string | undefined;
  title?: string | undefined;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Button                                                                      */
/* -------------------------------------------------------------------------- */

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "ghost" | "subtle";
  children: ReactNode;
}

export function Button({
  variant = "ghost",
  className,
  children,
  ...rest
}: ButtonProps) {
  const variants = {
    primary:
      "bg-accent text-canvas hover:brightness-110 font-medium disabled:opacity-40",
    subtle:
      "bg-raised text-content border border-subtle hover:bg-overlay disabled:opacity-40",
    ghost: "text-muted hover:text-content hover:bg-raised disabled:opacity-40",
  };
  return (
    <button
      type="button"
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-lg px-3 py-1.5",
        "text-sm transition-colors disabled:cursor-not-allowed",
        variants[variant],
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* Empty and error states                                                      */
/* -------------------------------------------------------------------------- */

export function EmptyState({
  title,
  description,
  icon,
  children,
}: {
  title: string;
  description: string;
  icon?: ReactNode | undefined;
  children?: ReactNode | undefined;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
      {icon && <div className="mb-4 text-faint">{icon}</div>}
      <h3 className="text-base font-semibold text-content">{title}</h3>
      <p className="mt-1.5 max-w-md text-sm leading-relaxed text-muted">
        {description}
      </p>
      {children && <div className="mt-5">{children}</div>}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: (() => void) | undefined;
}) {
  return (
    <div
      role="alert"
      className="rounded-card border border-danger/30 bg-danger/10 px-5 py-4"
    >
      <div className="flex items-start gap-3">
        <svg
          className="mt-0.5 size-4 shrink-0 text-danger"
          viewBox="0 0 16 16"
          fill="currentColor"
          aria-hidden="true"
        >
          <path d="M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM7.25 4.5h1.5v5h-1.5v-5Zm0 6.25h1.5v1.5h-1.5v-1.5Z" />
        </svg>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-content">
            That question could not be answered
          </p>
          <p className="mt-1 text-sm leading-relaxed text-muted">{message}</p>
        </div>
        {onRetry && (
          <Button variant="subtle" onClick={onRetry} className="shrink-0">
            Retry
          </Button>
        )}
      </div>
    </div>
  );
}
