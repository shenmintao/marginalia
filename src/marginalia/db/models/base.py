from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, MetaData, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from marginalia.utils.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UtcDateTime(TypeDecorator):  # type: ignore[type-arg]
    """DateTime that always round-trips as UTC-aware.

    SQLite has no native timezone type: it stores ``DateTime(timezone=True)``
    columns as naive ISO strings, so values written tz-aware come back naive.
    The wire-side ``isoformat()`` then emits no offset and the browser
    interprets the timestamp as *local* time — every GUI clock was wrong by
    the user's UTC offset (the visible bug behind goal #25).

    On bind we coerce naive inputs to UTC and convert any other tz to UTC so
    storage is uniform. On result we stamp ``tzinfo=UTC`` if missing. Postgres
    keeps its native timestamptz semantics — the conversions are no-ops there.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: dict[Any, Any] = {}


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), default=utcnow, onupdate=utcnow, nullable=False
    )
