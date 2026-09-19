/**
 * The API client.
 *
 * Requests go to a relative `/api` path so the same build works behind a
 * reverse proxy in production and through the Vite dev proxy locally, with no
 * environment-specific URL baked into the bundle.
 */

import type {
  HealthResponse,
  HistoryResponse,
  QueryResponse,
  SchemaResponse,
} from "@/types/api";

const BASE = import.meta.env.VITE_API_BASE_URL ?? "";

/** A failed request, carrying the server's safe message where there is one. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/api${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    // A network-level failure, not an application error: the server never
    // replied, so there is no safe message to surface.
    throw new ApiError(
      "Could not reach the server. Check that the backend is running.",
      0,
    );
  }

  if (!response.ok) {
    // The backend returns {error: {code, message}} with a message written to be
    // shown. Anything else means an unexpected failure, so fall back to a
    // status-derived message rather than rendering whatever arrived.
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.error?.message === "string") {
        message = body.error.message;
      } else if (typeof body?.detail === "string") {
        message = body.detail;
      } else if (Array.isArray(body?.detail) && body.detail[0]?.msg) {
        message = String(body.detail[0].msg);
      }
    } catch {
      /* keep the status-derived fallback */
    }
    throw new ApiError(message, response.status);
  }

  return (await response.json()) as T;
}

export const api = {
  query: (question: string, conversationId?: string) =>
    request<QueryResponse>("/query", {
      method: "POST",
      body: JSON.stringify({
        question,
        conversation_id: conversationId ?? null,
      }),
    }),

  health: () => request<HealthResponse>("/health"),
  schema: () => request<SchemaResponse>("/schema"),
  history: () => request<HistoryResponse>("/history"),
  suggestions: () => request<{ suggestions: string[] }>("/suggestions"),
  newConversation: () =>
    request<{ conversation_id: string }>("/conversations", { method: "POST" }),
};
