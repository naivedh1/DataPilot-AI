"""Schema endpoint.

Exposes the warehouse as the assistant understands it: structure joined to
business meaning, plus the metric definitions. Useful to a user wondering what
they can ask, and useful when debugging why a question was answered the way it
was.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.query import ColumnSchema, MetricSchema, SchemaResponse, TableSchema
from app.services.schema.catalog import JOIN_PATHS, METRICS
from app.services.schema.introspect import get_schema

router = APIRouter(tags=["schema"])


@router.get(
    "/schema",
    response_model=SchemaResponse,
    summary="The analytics schema and metric definitions",
)
def get_schema_endpoint() -> SchemaResponse:
    schema = get_schema()
    return SchemaResponse(
        tables=[
            TableSchema(
                name=table.name,
                description=table.description,
                columns=[
                    ColumnSchema(
                        name=column.name,
                        type=column.type_name,
                        nullable=column.nullable,
                        primary_key=column.is_primary_key,
                        foreign_key=column.foreign_key,
                        description=column.description,
                        allowed_values=list(column.allowed_values),
                    )
                    for column in table.columns
                ],
                notes=list(table.notes),
            )
            for table in (schema[name] for name in sorted(schema))
        ],
        metrics=[
            MetricSchema(
                name=metric.name,
                description=metric.description,
                expression=metric.expression,
                required_tables=list(metric.required_tables),
                caveat=metric.caveat,
            )
            for metric in METRICS
        ],
        join_paths=sorted(JOIN_PATHS.values()),
    )
