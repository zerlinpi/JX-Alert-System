from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from jx_alert.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine():
    database_url = get_settings().resolved_database_url()
    kwargs = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(database_url, **kwargs)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db():
    # Import every model module before create_all so all tables are registered.
    from jx_alert.storage import models  # noqa: F401
    from jx_alert.notifications import outbox  # noqa: F401
    from jx_alert.storage import sp  # noqa: F401
    Base.metadata.create_all(bind=engine)
    from jx_alert.storage.migrations import upgrade_schema
    upgrade_schema(engine)
