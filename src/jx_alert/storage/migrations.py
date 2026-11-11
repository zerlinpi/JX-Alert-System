"""Idempotent additive schema upgrades for installations created before Alembic.

New installations are created from ORM metadata. These upgrades keep existing
MariaDB/SQLite deployments compatible when nullable/defaulted columns are added.
Destructive changes require a versioned migration.
"""
from sqlalchemy import inspect, text


def _add_missing_columns(connection, inspector, table: str,
                         definitions: dict[str, str], changes: list[str]) -> None:
    columns = {
        column["name"] for column in inspector.get_columns(table)
    }
    for name, definition in definitions.items():
        if name in columns:
            continue
        connection.execute(text(
            f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
        ))
        columns.add(name)
        changes.append(f"{table}.{name}")


def _backfill_updated_at(connection, table: str) -> None:
    connection.execute(text(
        f"UPDATE {table} SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL"
    ))


def upgrade_schema(engine) -> list[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    changes: list[str] = []

    with engine.begin() as connection:
        if "anomaly_events" in tables:
            _add_missing_columns(connection, inspector, "anomaly_events", {
                "entity_id": "VARCHAR(200) NOT NULL DEFAULT ''",
                "evaluation_scope": "VARCHAR(200) NOT NULL DEFAULT ''",
                "recovered_at": "DATETIME NULL",
                "escalated_at": "DATETIME NULL",
            }, changes)
            indexes = {
                index["name"] for index in inspector.get_indexes("anomaly_events")
            }
            if "ix_anomaly_events_evaluation_scope" not in indexes:
                connection.execute(text(
                    "CREATE INDEX ix_anomaly_events_evaluation_scope "
                    "ON anomaly_events (evaluation_scope)"
                ))
                changes.append("ix_anomaly_events_evaluation_scope")

        if "stores" in tables:
            _add_missing_columns(connection, inspector, "stores", {
                "mid": "VARCHAR(100) NULL",
                "seller_id": "VARCHAR(100) NULL",
                "account_name": "VARCHAR(200) NULL",
                "region": "VARCHAR(20) NULL",
                "country": "VARCHAR(80) NULL",
                "marketplace_id": "VARCHAR(100) NULL",
                "timezone": "VARCHAR(64) NULL",
                "currency_code": "VARCHAR(8) NULL",
                "status": "INTEGER NOT NULL DEFAULT 0",
                "has_ads_setting": "INTEGER NOT NULL DEFAULT 0",
                "updated_at": "DATETIME NULL",
            }, changes)
            if "stores.updated_at" in changes:
                _backfill_updated_at(connection, "stores")

        if "products" in tables:
            _add_missing_columns(connection, inspector, "products", {
                "asin": "VARCHAR(32) NULL",
                "local_sku": "VARCHAR(200) NULL",
                "product_name": "TEXT NULL",
                "currency_code": "VARCHAR(8) NULL",
                "owner_open_ids": "JSON NULL",
                "updated_at": "DATETIME NULL",
            }, changes)
            if "products.updated_at" in changes:
                _backfill_updated_at(connection, "products")

        if "campaigns" in tables:
            _add_missing_columns(connection, inspector, "campaigns", {
                "profile_id": "VARCHAR(100) NULL",
                "name": "VARCHAR(500) NULL",
                "state": "VARCHAR(40) NULL",
                "serving_status": "VARCHAR(80) NULL",
                "budget": "FLOAT NULL",
                "budget_type": "VARCHAR(40) NULL",
                "start_date": "VARCHAR(40) NULL",
                "end_date": "VARCHAR(40) NULL",
                "last_over_budget_at": "VARCHAR(40) NULL",
                "updated_at": "DATETIME NULL",
            }, changes)
            if "campaigns.updated_at" in changes:
                _backfill_updated_at(connection, "campaigns")

        if "ad_products" in tables:
            _add_missing_columns(connection, inspector, "ad_products", {
                "ad_group_id": "VARCHAR(100) NULL",
                "asin": "VARCHAR(32) NULL",
                "seller_sku": "VARCHAR(200) NULL",
                "state": "VARCHAR(40) NULL",
                "serving_status": "VARCHAR(80) NULL",
                "updated_at": "DATETIME NULL",
            }, changes)
            if "ad_products.updated_at" in changes:
                _backfill_updated_at(connection, "ad_products")

    return changes
