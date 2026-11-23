"""Idempotent additive schema upgrades.

New tables are created from ORM metadata. Existing tables are never dropped or
altered destructively; retired Lingxing tables are left exactly as they are.
Add nullable/defaulted columns here when a model gains one.
"""
from sqlalchemy import inspect, text


def _add_missing_columns(connection, inspector, table, definitions, changes):
    columns = {column["name"] for column in inspector.get_columns(table)}
    for name, definition in definitions.items():
        if name not in columns:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
            changes.append(f"{table}.{name}")


# table -> {column: DDL}; empty while the current models match every deployment.
ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {}


def upgrade_schema(engine) -> list[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    changes: list[str] = []
    with engine.begin() as connection:
        for table, definitions in ADDITIVE_COLUMNS.items():
            if table in tables:
                _add_missing_columns(connection, inspector, table, definitions, changes)
    return changes
