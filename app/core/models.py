"""SQLAlchemy declarative base.

Tables arrive in M1 (IMPLEMENTATION.md §6). The naming convention is fixed here
and now on purpose: changing it later renames every constraint in the database
and churns migrations that have already been applied.

Nothing in this package may import fastapi, aiogram, jinja2, aiosmtplib, or nh3
(§3, enforced by tests/core/test_architecture.py).
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
