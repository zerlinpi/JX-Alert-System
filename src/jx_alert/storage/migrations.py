"""Small idempotent schema upgrades for installations created before Alembic.

New installations are created from ORM metadata. These additive upgrades keep
existing MariaDB/SQLite deployments compatible when new nullable/defaulted
columns are introduced. Destructive changes require a versioned migration.
"""
from sqlalchemy import inspect, text


def upgrade_schema(engine) -> list[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "anomaly_events" not in tables:
        return []

    columns = {column["name"] for column in inspector.get_columns("anomaly_events")}
    indexes = {index["name"] for index in inspector.get_indexes("anomaly_events")}
    changes: list[str] = []

    with engine.begin() as connection:
        if "entity_id" not in columns:
            connection.execute(text(
                "ALTER TABLE anomaly_events "
                "ADD COLUMN entity_id VARCHAR(200) NOT NULL DEFAULT ''"
            ))
            changes.append("anomaly_events.entity_id")
        if "evaluation_scope" not in columns:
            connection.execute(text(
                "ALTER TABLE anomaly_events "
                "ADD COLUMN evaluation_scope VARCHAR(200) NOT NULL DEFAULT ''"
            ))
            changes.append("anomaly_events.evaluation_scope")
        if "recovered_at" not in columns:
            connection.execute(text(
                "ALTER TABLE anomaly_events ADD COLUMN recovered_at DATETIME NULL"
            ))
            changes.append("anomaly_events.recovered_at")
        if "ix_anomaly_events_evaluation_scope" not in indexes:
            connection.execute(text(
                "CREATE INDEX ix_anomaly_events_evaluation_scope "
                "ON anomaly_events (evaluation_scope)"
            ))
            changes.append("ix_anomaly_events_evaluation_scope")

    return changes
