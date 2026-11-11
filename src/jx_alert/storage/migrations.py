"""Small idempotent schema upgrades for installations created before Alembic.

New installations are created from ORM metadata. These additive upgrades keep
existing MariaDB/SQLite deployments compatible when nullable/defaulted columns
are introduced. Destructive changes require a versioned migration.
"""
from sqlalchemy import inspect, text


def upgrade_schema(engine) -> list[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    changes: list[str] = []

    with engine.begin() as connection:
        if "anomaly_events" in tables:
            columns = {
                column["name"] for column in inspector.get_columns("anomaly_events")
            }
            indexes = {
                index["name"] for index in inspector.get_indexes("anomaly_events")
            }
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
            if "escalated_at" not in columns:
                connection.execute(text(
                    "ALTER TABLE anomaly_events ADD COLUMN escalated_at DATETIME NULL"
                ))
                changes.append("anomaly_events.escalated_at")
            if "ix_anomaly_events_evaluation_scope" not in indexes:
                connection.execute(text(
                    "CREATE INDEX ix_anomaly_events_evaluation_scope "
                    "ON anomaly_events (evaluation_scope)"
                ))
                changes.append("ix_anomaly_events_evaluation_scope")

        if "stores" in tables:
            store_columns = {
                column["name"] for column in inspector.get_columns("stores")
            }
            if "timezone" not in store_columns:
                connection.execute(text(
                    "ALTER TABLE stores ADD COLUMN timezone VARCHAR(64) NULL"
                ))
                changes.append("stores.timezone")
            if "currency_code" not in store_columns:
                connection.execute(text(
                    "ALTER TABLE stores ADD COLUMN currency_code VARCHAR(8) NULL"
                ))
                changes.append("stores.currency_code")

    return changes
