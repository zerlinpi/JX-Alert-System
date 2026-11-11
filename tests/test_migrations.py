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

    assert {"entity_id", "evaluation_scope", "recovered_at", "escalated_at"} <= columns
    assert "ix_anomaly_events_evaluation_scope" in indexes
    assert "anomaly_events.entity_id" in changes
    assert "anomaly_events.evaluation_scope" in changes
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT entity_id, evaluation_scope, recovered_at, escalated_at FROM anomaly_events WHERE id=1")
        ).one()
        assert row.entity_id == ""
        assert row.evaluation_scope == ""
        assert row.recovered_at is None
        assert row.escalated_at is None

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
    assert {
        "mid", "seller_id", "account_name", "region", "country",
        "marketplace_id", "timezone", "currency_code", "status",
        "has_ads_setting", "updated_at",
    } <= columns
    assert "stores.timezone" in changes
    assert "stores.currency_code" in changes
    assert "stores.status" in changes
    with engine.connect() as connection:
        updated_at = connection.execute(
            text("SELECT updated_at FROM stores WHERE sid='101'")
        ).scalar_one()
        assert updated_at is not None



def test_upgrade_schema_adds_owner_and_ad_metadata_columns_to_legacy_tables():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY,
                store_id VARCHAR(100) NOT NULL,
                seller_sku VARCHAR(200) NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE campaigns (
                id INTEGER PRIMARY KEY,
                store_id VARCHAR(100) NOT NULL,
                campaign_type VARCHAR(8) NOT NULL,
                campaign_id VARCHAR(100) NOT NULL
            )
        """))
        connection.execute(text("""
            CREATE TABLE ad_products (
                id INTEGER PRIMARY KEY,
                store_id VARCHAR(100) NOT NULL,
                campaign_type VARCHAR(8) NOT NULL,
                ad_id VARCHAR(100) NOT NULL,
                campaign_id VARCHAR(100) NOT NULL
            )
        """))
        connection.execute(text(
            "INSERT INTO products (id, store_id, seller_sku) VALUES (1, '101', 'SKU-1')"
        ))
        connection.execute(text(
            "INSERT INTO campaigns (id, store_id, campaign_type, campaign_id) "
            "VALUES (1, '101', 'sp', 'campaign-1')"
        ))
        connection.execute(text(
            "INSERT INTO ad_products (id, store_id, campaign_type, ad_id, campaign_id) "
            "VALUES (1, '101', 'sp', 'ad-1', 'campaign-1')"
        ))

    changes = upgrade_schema(engine)
    inspector = inspect(engine)
    product_columns = {column["name"] for column in inspector.get_columns("products")}
    campaign_columns = {column["name"] for column in inspector.get_columns("campaigns")}
    ad_columns = {column["name"] for column in inspector.get_columns("ad_products")}
    assert {"asin", "local_sku", "product_name", "currency_code",
            "owner_open_ids", "updated_at"} <= product_columns
    assert {"profile_id", "name", "state", "serving_status", "budget",
            "budget_type", "start_date", "end_date", "last_over_budget_at",
            "updated_at"} <= campaign_columns
    assert {"ad_group_id", "asin", "seller_sku", "state",
            "serving_status", "updated_at"} <= ad_columns
    assert "products.owner_open_ids" in changes
    assert "campaigns.last_over_budget_at" in changes
    assert "ad_products.ad_group_id" in changes

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT updated_at FROM products WHERE id=1")
        ).scalar_one() is not None
        assert connection.execute(
            text("SELECT updated_at FROM campaigns WHERE id=1")
        ).scalar_one() is not None
        assert connection.execute(
            text("SELECT updated_at FROM ad_products WHERE id=1")
        ).scalar_one() is not None
    assert upgrade_schema(engine) == []
