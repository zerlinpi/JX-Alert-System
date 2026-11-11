from sqlalchemy import create_engine, inspect, text

from jx_alert.storage.migrations import upgrade_schema


def test_upgrade_schema_adds_missing_anomaly_columns_and_index():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE anomaly_events (
                id INTEGER PRIMARY KEY,
                dedupe_key VARCHAR(512) NOT NULL,
                store_id VARCHAR(100) NOT NULL,
                rule VARCHAR(80) NOT NULL,
                severity VARCHAR(8) NOT NULL,
                status VARCHAR(20) NOT NULL,
                first_seen DATETIME NOT NULL,
                last_seen DATETIME NOT NULL,
                metrics JSON NOT NULL,
                reason TEXT NOT NULL
            )
        """))
        connection.execute(text("""
            INSERT INTO anomaly_events
            (id, dedupe_key, store_id, rule, severity, status, first_seen, last_seen, metrics, reason)
            VALUES (1, 'legacy-key', 'store-1', 'orders_decrease', 'P1', 'ONGOING',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '{}', 'legacy row')
        """))

    changes = upgrade_schema(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("anomaly_events")}
    indexes = {index["name"] for index in inspect(engine).get_indexes("anomaly_events")}

    assert {"entity_id", "evaluation_scope", "recovered_at"} <= columns
    assert "ix_anomaly_events_evaluation_scope" in indexes
    assert "anomaly_events.entity_id" in changes
    assert "anomaly_events.evaluation_scope" in changes
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT entity_id, evaluation_scope, recovered_at FROM anomaly_events WHERE id=1")
        ).one()
        assert row.entity_id == ""
        assert row.evaluation_scope == ""
        assert row.recovered_at is None

    assert upgrade_schema(engine) == []


def test_upgrade_schema_is_safe_when_anomaly_table_does_not_exist():
    engine = create_engine("sqlite:///:memory:")
    assert upgrade_schema(engine) == []



def test_upgrade_schema_adds_timezone_and_currency_to_legacy_store_table():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE stores (
                sid VARCHAR(100) PRIMARY KEY,
                name VARCHAR(200) NOT NULL
            )
        """))
        connection.execute(text(
            "INSERT INTO stores (sid, name) VALUES ('101', 'Store A')"
        ))

    changes = upgrade_schema(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("stores")}
    assert {"timezone", "currency_code"} <= columns
    assert "stores.timezone" in changes
    assert "stores.currency_code" in changes
