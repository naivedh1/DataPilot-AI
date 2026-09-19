"""Structural schema, read from SQLAlchemy metadata.

Structure comes from the models rather than from hand-written text, so it can
never describe a column that no longer exists. Meaning comes from
`catalog.py`. This module joins the two.

Reading from `Base.metadata` rather than querying `information_schema` means
schema description needs no database connection at all — which keeps the
retrieval layer unit-testable and keeps application startup independent of
database availability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import CheckConstraint, Table

from app.models import Base
from app.services.schema.catalog import TABLES, ColumnDoc, TableDoc


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """One column: structure plus meaning."""

    name: str
    type_name: str
    nullable: bool
    is_primary_key: bool
    description: str
    #: Values permitted by a CHECK constraint, when the column has one.
    allowed_values: tuple[str, ...] = ()
    foreign_key: str | None = None
    synonyms: tuple[str, ...] = ()

    def render(self) -> str:
        """One line, as shown to the language model."""
        parts = [f"  {self.name} ({self.type_name})"]
        if self.is_primary_key:
            parts.append("PK")
        if self.foreign_key:
            parts.append(f"FK -> {self.foreign_key}")
        if not self.nullable and not self.is_primary_key:
            parts.append("NOT NULL")
        header = " ".join(parts)

        line = f"{header} - {self.description}"
        if self.allowed_values:
            values = ", ".join(f"'{value}'" for value in self.allowed_values)
            line += f" Allowed values: {values}."
        return line


@dataclass(frozen=True, slots=True)
class TableInfo:
    """One table: structure plus meaning."""

    name: str
    description: str
    columns: tuple[ColumnInfo, ...]
    notes: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    row_estimate: int | None = None
    _by_name: dict[str, ColumnInfo] = field(default_factory=dict, repr=False)

    def column(self, name: str) -> ColumnInfo | None:
        return self._by_name.get(name)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    def render(self) -> str:
        """The block of text describing this table to the model."""
        lines = [f"TABLE {self.name} - {self.description}"]
        lines.extend(column.render() for column in self.columns)
        lines.extend(f"  NOTE: {note}" for note in self.notes)
        return "\n".join(lines)


def _check_constraint_values(table: Table, column_name: str) -> tuple[str, ...]:
    """Extract the literals from a `col IN ('a','b')` CHECK constraint.

    Showing the model the exact permitted values is the single most effective
    way to stop it inventing `'ENTERPRISE'` when the data says `'Enterprise'`.
    Parsed from the constraint rather than duplicated in the catalogue, so the
    two cannot disagree.
    """
    for constraint in table.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        text = str(constraint.sqltext)
        if not text.startswith(f"{column_name} IN ("):
            continue
        inner = text[len(f"{column_name} IN (") :].rstrip(")")
        return tuple(part.strip().strip("'") for part in inner.split(",") if part.strip())
    return ()


def _foreign_key_target(table: Table, column_name: str) -> str | None:
    for foreign_key in table.foreign_keys:
        if foreign_key.parent.name == column_name:
            return f"{foreign_key.column.table.name}.{foreign_key.column.name}"
    return None


def _build_table(table: Table, doc: TableDoc) -> TableInfo:
    columns: list[ColumnInfo] = []
    for column in table.columns:
        column_doc: ColumnDoc = doc.columns.get(
            column.name, ColumnDoc(description="(undocumented)")
        )
        columns.append(
            ColumnInfo(
                name=column.name,
                type_name=str(column.type),
                nullable=column.nullable if column.nullable is not None else True,
                is_primary_key=column.primary_key,
                description=column_doc.description,
                allowed_values=_check_constraint_values(table, column.name),
                foreign_key=_foreign_key_target(table, column.name),
                synonyms=column_doc.synonyms,
            )
        )

    return TableInfo(
        name=table.name,
        description=doc.description,
        columns=tuple(columns),
        notes=doc.notes,
        synonyms=doc.synonyms,
        _by_name={column.name: column for column in columns},
    )


@lru_cache(maxsize=1)
def get_schema() -> dict[str, TableInfo]:
    """The full schema: every table, structure joined to meaning.

    Cached — the models do not change at runtime.
    """
    return {
        name: _build_table(table, TABLES[name])
        for name, table in Base.metadata.tables.items()
        if name in TABLES
    }


def undocumented_columns() -> list[str]:
    """Columns present in the models but missing from the catalogue.

    Used by a test: a column the model is told nothing about is a column it will
    either ignore or guess at.
    """
    missing: list[str] = []
    for name, table in Base.metadata.tables.items():
        doc = TABLES.get(name)
        if doc is None:
            missing.append(f"{name} (whole table)")
            continue
        missing.extend(
            f"{name}.{column.name}" for column in table.columns if column.name not in doc.columns
        )
    return missing


def stale_catalogue_entries() -> list[str]:
    """Catalogue entries describing something the models no longer have.

    The other direction of the same check: documentation that has outlived its
    column would be actively misleading to the model.
    """
    stale: list[str] = []
    for table_name, doc in TABLES.items():
        table = Base.metadata.tables.get(table_name)
        if table is None:
            stale.append(f"{table_name} (whole table)")
            continue
        actual = {column.name for column in table.columns}
        stale.extend(
            f"{table_name}.{column_name}"
            for column_name in doc.columns
            if column_name not in actual
        )
    return stale
