"""Schema intelligence: structure, meaning, and relevance-based retrieval.

`introspect` reads structure from the SQLAlchemy models; `catalog` supplies the
business meaning the database cannot hold; `retrieval` selects the subset
relevant to a given question so the model is never handed the whole warehouse.
"""

from app.services.schema.catalog import METRICS, TABLES, MetricDoc, join_clause
from app.services.schema.introspect import (
    ColumnInfo,
    TableInfo,
    get_schema,
    stale_catalogue_entries,
    undocumented_columns,
)
from app.services.schema.retrieval import (
    RetrievedSchema,
    match_metrics,
    render_full_schema,
    retrieve,
    score_tables,
    tokenize,
)

__all__ = [
    "METRICS",
    "TABLES",
    "ColumnInfo",
    "MetricDoc",
    "RetrievedSchema",
    "TableInfo",
    "get_schema",
    "join_clause",
    "match_metrics",
    "render_full_schema",
    "retrieve",
    "score_tables",
    "stale_catalogue_entries",
    "tokenize",
    "undocumented_columns",
]
