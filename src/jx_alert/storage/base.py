"""Shared ORM metadata, independent of deployment settings and DB engines."""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
