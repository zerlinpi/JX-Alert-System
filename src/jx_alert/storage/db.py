from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from jx_alert.config import get_settings

class Base(DeclarativeBase): pass

def make_engine():
    return create_engine(get_settings().database_url, pool_pre_ping=True)

engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

def init_db():
    from jx_alert.storage import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
