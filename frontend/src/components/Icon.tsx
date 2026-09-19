/**
 * Inline SVG icons.
 *
 * Their own module so `primitives.tsx` exports only components — a file that
 * mixes components with other values breaks React Fast Refresh.
 *
 * Inline rather than an icon library: a dozen 16px glyphs do not justify a
 * dependency, and these ship no runtime cost at all.
 */

import { cn } from "@/lib/cn";

export const Icon = {
  Sparkle: () => (
    <svg viewBox="0 0 16 16" fill="currentColor" className="size-4" aria-hidden="true">
      <path d="M8 1l1.6 4.4L14 7l-4.4 1.6L8 13l-1.6-4.4L2 7l4.4-1.6L8 1Z" />
    </svg>
  ),
  Chart: () => (
    <svg viewBox="0 0 16 16" fill="currentColor" className="size-4" aria-hidden="true">
      <path d="M2 13h12v1.5H2V13Zm1.5-5h2v4h-2V8Zm3.25-4h2v8h-2V4Zm3.25 2h2v6h-2V6Z" />
    </svg>
  ),
  Table: () => (
    <svg viewBox="0 0 16 16" fill="currentColor" className="size-4" aria-hidden="true">
      <path d="M2 3h12v10H2V3Zm1.5 1.5v2h9v-2h-9Zm0 3.5v1.5h4V8h-4Zm5.5 0v1.5h3.5V8H9Zm-5.5 3v1.5h4V11h-4Zm5.5 0v1.5h3.5V11H9Z" />
    </svg>
  ),
  Code: () => (
    <svg viewBox="0 0 16 16" fill="currentColor" className="size-4" aria-hidden="true">
      <path d="M5.4 3.6 1 8l4.4 4.4 1.1-1.1L3.2 8l3.3-3.3L5.4 3.6Zm5.2 0L9.5 4.7 12.8 8l-3.3 3.3 1.1 1.1L15 8l-4.4-4.4Z" />
    </svg>
  ),
  Clock: () => (
    <svg viewBox="0 0 16 16" fill="currentColor" className="size-4" aria-hidden="true">
      <path d="M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM8.75 4v3.7l2.5 1.5-.75 1.25-3.25-2V4h1.5Z" />
    </svg>
  ),
  Plus: () => (
    <svg viewBox="0 0 16 16" fill="currentColor" className="size-4" aria-hidden="true">
      <path d="M7.25 2.5h1.5v4.75h4.75v1.5H8.75v4.75h-1.5V8.75H2.5v-1.5h4.75V2.5Z" />
    </svg>
  ),
  Send: () => (
    <svg viewBox="0 0 16 16" fill="currentColor" className="size-4" aria-hidden="true">
      <path d="M1.5 14.5 15 8 1.5 1.5 1.5 6.5 10 8l-8.5 1.5v5Z" />
    </svg>
  ),
  Database: () => (
    <svg viewBox="0 0 16 16" fill="currentColor" className="size-4" aria-hidden="true">
      <path d="M8 1.5c3 0 5.5.9 5.5 2v9c0 1.1-2.5 2-5.5 2s-5.5-.9-5.5-2v-9c0-1.1 2.5-2 5.5-2Zm0 1.5c-2.5 0-4 .6-4 .8s1.5.7 4 .7 4-.5 4-.7-1.5-.8-4-.8Z" />
    </svg>
  ),
  Chevron: ({ open }: { open: boolean }) => (
    <svg
      viewBox="0 0 16 16"
      fill="currentColor"
      className={cn("size-3.5 transition-transform", open && "rotate-90")}
      aria-hidden="true"
    >
      <path d="M6 3.5 10.5 8 6 12.5V3.5Z" />
    </svg>
  ),
};
