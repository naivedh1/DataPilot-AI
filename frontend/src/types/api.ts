/**
 * Wire types, mirroring `backend/app/schemas/query.py`.
 *
 * Hand-written rather than generated: the surface is small, and a generator
 * would add a build step and a stale-artifact problem for a handful of
 * interfaces. The backend's OpenAPI schema at /docs is the source of truth if
 * these ever drift.
 */

export interface Insight {
  text: string;
  /** `recommendation` is an opinion; the others are observations. */
  kind: "finding" | "trend" | "concern" | "recommendation";
  supporting_values: string[];
}

export interface ChartSeries {
  name: string;
  values: (number | string | null)[];
}

export interface Chart {
  chart_type:
    | "line"
    | "bar"
    | "grouped_bar"
    | "scatter"
    | "histogram"
    | "pie"
    | "table";
  title: string;
  x_label: string;
  y_label: string;
  x_values: (string | number)[];
  series: ChartSeries[];
  /** Why this chart form was chosen. Shown so the choice is inspectable. */
  rationale: string;
  value_format: "currency" | "percent" | "number";
}

export interface ExecutionMeta {
  status: "success" | "no_query" | "error";
  row_count: number;
  truncated: boolean;
  execution_ms: number;
  total_ms: number;
  retries: number;
  llm_calls: number;
  /** True when answered by the deterministic offline baseline, not a model. */
  llm_simulated: boolean;
  tables_used: string[];
  intent: string;
}

export interface NodeTrace {
  node: string;
  duration_ms: number;
  status: string;
  detail: string;
}

export interface SQLAttempt {
  sql: string;
  valid: boolean;
  executed: boolean;
  error: string;
  stage: string;
}

export type Cell = string | number | boolean | null;

export type ConfidenceLevel = "high" | "medium" | "low" | "insufficient_data";

/** One input to the confidence assessment, and the ceiling it imposed. */
export interface ConfidenceSignal {
  name: string;
  ceiling: ConfidenceLevel;
  detail: string;
}

/**
 * Why the answer carries the confidence it does.
 *
 * Computed by the backend from what the run actually did — no part of it is
 * supplied by the language model. `level` is a ceiling: the worst signal wins.
 */
export interface Confidence {
  level: ConfidenceLevel;
  rationale: string;
  signals: ConfidenceSignal[];
}

export type CheckStatus = "passed" | "failed" | "skipped";

export interface ValidationCheck {
  name: string;
  status: CheckStatus;
  detail: string;
}

export interface Validation {
  /** `not_verified` means nothing applicable ran — it is not a pass. */
  status: "passed" | "failed" | "not_verified";
  checks: ValidationCheck[];
}

/** One query the diagnostic ran, and what it was for. */
export interface InvestigationStep {
  name: string;
  purpose: string;
  sql: string;
  columns: string[];
  rows: Cell[][];
  error: string;
}

/** One dimension value's share of the overall change. */
export interface Contributor {
  dimension: string;
  label: string;
  change: number;
  /** Signed against the headline: negative means it moved the other way. */
  share_of_change: number;
}

/**
 * A multi-step diagnostic. Present only for "why did X change" questions.
 *
 * Every figure was computed by the backend from templated SQL; the model chose
 * what to investigate but wrote none of the queries.
 */
export interface Investigation {
  plan: {
    intent: string;
    metric: string;
    current_period: string;
    comparison_period: string;
    current_period_display?: string;
    comparison_period_display?: string;
    dimensions_to_investigate: string[];
    checks: string[];
  };
  steps: InvestigationStep[];
  current_value: number;
  previous_value: number;
  change: number;
  percent_change: number | null;
  contributors: Contributor[];
  /** Null when there was no change to attribute. */
  reconciled: boolean | null;
  caveats: string[];
}

export interface QueryResponse {
  request_id: string;
  conversation_id: string;
  question: string;
  answer: string;
  insights: Insight[];
  caveats: string[];
  sql: string;
  columns: string[];
  rows: Cell[][];
  chart: Chart | null;
  reasoning_summary: string;
  validation: Validation | null;
  confidence: Confidence | null;
  investigation: Investigation | null;
  execution: ExecutionMeta;
  attempts: SQLAttempt[];
  trace: NodeTrace[];
  error: string | null;
}

export interface ConversationSummary {
  conversation_id: string;
  title: string;
  turn_count: number;
  created_at: string;
  updated_at: string;
}

export interface HistoryItem {
  request_id: string;
  question: string;
  answer: string;
  status: string;
  row_count: number;
  total_ms: number;
  sql: string;
}

export interface HistoryResponse {
  conversations: ConversationSummary[];
  queries: HistoryItem[];
}

export interface SchemaColumn {
  name: string;
  type: string;
  nullable: boolean;
  primary_key: boolean;
  foreign_key: string | null;
  description: string;
  allowed_values: string[];
}

export interface SchemaTable {
  name: string;
  description: string;
  columns: SchemaColumn[];
  notes: string[];
}

export interface SchemaMetric {
  name: string;
  description: string;
  expression: string;
  required_tables: string[];
  caveat: string | null;
}

export interface SchemaResponse {
  tables: SchemaTable[];
  metrics: SchemaMetric[];
  join_paths: string[];
}

export interface HealthResponse {
  status: "ok" | "degraded";
  version: string;
  environment: string;
  llm_configured: boolean;
  database_configured: boolean;
}

/** A turn in the on-screen conversation. */
export interface Turn {
  id: string;
  question: string;
  /** Null while the request is in flight. */
  response: QueryResponse | null;
  error: string | null;
}
