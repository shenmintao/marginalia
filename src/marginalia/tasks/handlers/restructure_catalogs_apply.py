"""Apply restructure_catalogs operations in a single transaction.

The handler hands us the validated ops list; we run them in order against
the catalogs / file_entries tables, with cycle checks and FK resolution.
Each op gets one task_outcomes row; failures are 'rejected', successes
'applied'. The whole batch is a single SQL transaction — if any unexpected
DB error escapes, the caller's session_scope rolls back.

temp_id resolution: a `create` op assigns a real catalog id; we map
`temp_id → real_id` for use in subsequent ops' parent_id / merge_into /
target_catalog_id fields.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select, update

from marginalia.db.models import Catalog, FileEntry
from marginalia.db.session import session_scope
from marginalia.services.audit import write_event
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import KIND_RESTRUCTURE_CATALOGS
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)


async def apply_operations(*, operations: list[dict[str, Any]], now: datetime) -> None:
    """Validate + apply each op; record outcome per op + summary."""
    applied = 0
    rejected = 0
    temp_id_map: dict[str, str] = {}

    async with session_scope() as session:
        for op_data in operations:
            kind = op_data.get("op")
            try:
                if kind == "rename":
                    await _op_rename(session, op_data, now)
                elif kind == "move":
                    await _op_move(session, op_data, temp_id_map, now)
                elif kind == "update_extra":
                    await _op_update_extra(session, op_data, now)
                elif kind == "create":
                    await _op_create(session, op_data, temp_id_map, now)
                elif kind == "soft_delete":
                    await _op_soft_delete(session, op_data, temp_id_map, now)
                elif kind == "move_entries":
                    await _op_move_entries(session, op_data, temp_id_map, now)
                else:
                    raise _RejectedOp(f"unknown op kind: {kind!r}")
                applied += 1
            except _RejectedOp as e:
                log.warning("restructure_catalogs: rejected %s — %s", kind, e)
                await record_outcome(
                    session,
                    task_kind=KIND_RESTRUCTURE_CATALOGS,
                    object_kind="catalog_op",
                    object_id=str(op_data.get("catalog_id") or op_data.get("temp_id") or "global"),
                    outcome="rejected",
                    detail={"op": op_data, "error": str(e)},
                )
                rejected += 1

        await record_outcome(
            session,
            task_kind=KIND_RESTRUCTURE_CATALOGS,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if applied else ("rejected" if rejected else "noop"),
            detail={
                "applied": applied,
                "rejected": rejected,
                "operations_received": len(operations),
            },
        )
        await session.commit()


class _RejectedOp(Exception):
    """A single op fails validation; sibling ops in the batch continue."""


def _resolve(ref: str | None, temp_map: dict[str, str]) -> str | None:
    if ref is None:
        return None
    if isinstance(ref, str) and ref.startswith("tmp_"):
        if ref not in temp_map:
            raise _RejectedOp(f"unresolved temp_id: {ref}")
        return temp_map[ref]
    return ref


async def _load_live(session, catalog_id: str) -> Catalog:
    cat = await session.get(Catalog, catalog_id)
    if cat is None or cat.deleted_at is not None:
        raise _RejectedOp(f"catalog {catalog_id} not found or soft-deleted")
    return cat


async def _would_cycle(session, *, child_id: str, new_parent_id: str | None) -> bool:
    """True if setting child.parent_id = new_parent_id would create a cycle."""
    if new_parent_id is None:
        return False
    if new_parent_id == child_id:
        return True
    seen: set[str] = {child_id}
    cur: str | None = new_parent_id
    while cur is not None:
        if cur in seen:
            return True
        seen.add(cur)
        row = await session.get(Catalog, cur)
        if row is None:
            return False
        cur = row.parent_id
    return False


# ----- ops -------------------------------------------------------------------

async def _op_rename(session, op: dict, now: datetime) -> None:
    cat = await _load_live(session, op["catalog_id"])
    new_name = (op.get("new_name") or "").strip()
    if not new_name:
        raise _RejectedOp("rename: new_name is empty")
    if new_name == cat.name:
        raise _RejectedOp("rename: new_name unchanged")
    old_name = cat.name
    cat.name = new_name
    cat.updated_at = now
    await write_event(session, kind="catalog_updated", payload={
        "catalog_id": cat.id, "field": "name",
        "old": old_name, "new": new_name,
    })
    await record_outcome(
        session, task_kind=KIND_RESTRUCTURE_CATALOGS,
        object_kind="catalog", object_id=cat.id,
        outcome="applied",
        detail={"op": "rename", "old": old_name, "new": new_name},
    )


async def _op_move(session, op: dict, temp_map: dict, now: datetime) -> None:
    cat = await _load_live(session, op["catalog_id"])
    new_parent_id = _resolve(op.get("new_parent_id"), temp_map)
    if new_parent_id is not None:
        await _load_live(session, new_parent_id)  # parent must exist + be live
        if await _would_cycle(session, child_id=cat.id, new_parent_id=new_parent_id):
            raise _RejectedOp(
                f"move would create cycle (child={cat.id} -> parent={new_parent_id})"
            )
    if cat.parent_id == new_parent_id:
        raise _RejectedOp("move: parent_id unchanged")
    old_parent = cat.parent_id
    cat.parent_id = new_parent_id
    cat.updated_at = now
    await write_event(session, kind="catalog_moved", payload={
        "catalog_id": cat.id, "old_parent": old_parent, "new_parent": new_parent_id,
    })
    await record_outcome(
        session, task_kind=KIND_RESTRUCTURE_CATALOGS,
        object_kind="catalog", object_id=cat.id,
        outcome="applied",
        detail={"op": "move", "old_parent": old_parent, "new_parent": new_parent_id},
    )


async def _op_update_extra(session, op: dict, now: datetime) -> None:
    cat = await _load_live(session, op["catalog_id"])
    new_extra = op.get("extra") or None
    cat.extra = new_extra
    cat.updated_at = now
    await write_event(session, kind="catalog_updated", payload={
        "catalog_id": cat.id, "field": "extra",
    })
    await record_outcome(
        session, task_kind=KIND_RESTRUCTURE_CATALOGS,
        object_kind="catalog", object_id=cat.id,
        outcome="applied",
        detail={"op": "update_extra"},
    )


async def _op_create(session, op: dict, temp_map: dict, now: datetime) -> None:
    temp_id = op.get("temp_id")
    if not temp_id or not str(temp_id).startswith("tmp_"):
        raise _RejectedOp("create: temp_id missing or not 'tmp_*' prefixed")
    if temp_id in temp_map:
        raise _RejectedOp(f"create: temp_id {temp_id} already used")
    name = (op.get("name") or "").strip()
    if not name:
        raise _RejectedOp("create: name empty")
    parent_id = _resolve(op.get("parent_id"), temp_map)
    if parent_id is not None:
        await _load_live(session, parent_id)

    real_id = new_id()
    cat = Catalog(
        id=real_id, parent_id=parent_id, name=name,
        summary=op.get("summary"), description=op.get("description"),
        tags=op.get("tags"), extra=None,
        created_at=now, updated_at=now,
    )
    session.add(cat)
    await session.flush()
    temp_map[temp_id] = real_id
    await write_event(session, kind="catalog_updated", payload={
        "catalog_id": real_id, "field": "create",
        "name": name, "parent_id": parent_id,
    })
    await record_outcome(
        session, task_kind=KIND_RESTRUCTURE_CATALOGS,
        object_kind="catalog", object_id=real_id,
        outcome="applied",
        detail={"op": "create", "temp_id": temp_id, "name": name, "parent_id": parent_id},
    )


async def _op_soft_delete(session, op: dict, temp_map: dict, now: datetime) -> None:
    cat = await _load_live(session, op["catalog_id"])
    merge_into = _resolve(op.get("merge_into"), temp_map)
    if merge_into is not None:
        if merge_into == cat.id:
            raise _RejectedOp("soft_delete: merge_into cannot be self")
        await _load_live(session, merge_into)

    # Reassign children
    children = (
        await session.execute(
            select(Catalog).where(Catalog.parent_id == cat.id, Catalog.deleted_at.is_(None))
        )
    ).scalars().all()
    for child in children:
        child.parent_id = merge_into
        child.updated_at = now
        await write_event(session, kind="catalog_moved", payload={
            "catalog_id": child.id, "old_parent": cat.id, "new_parent": merge_into,
            "reason": "parent_soft_deleted",
        })

    # Reassign entries
    target = merge_into  # may be None → uncategorised
    n_entries = (
        await session.execute(
            update(FileEntry)
            .where(FileEntry.catalog_id == cat.id)
            .values(catalog_id=target, updated_at=now)
        )
    ).rowcount or 0

    cat.deleted_at = now
    cat.updated_at = now
    await write_event(session, kind="catalog_updated", payload={
        "catalog_id": cat.id, "field": "deleted_at", "merge_into": merge_into,
        "entries_reassigned": n_entries,
        "children_reassigned": len(children),
    })
    await record_outcome(
        session, task_kind=KIND_RESTRUCTURE_CATALOGS,
        object_kind="catalog", object_id=cat.id,
        outcome="applied",
        detail={
            "op": "soft_delete",
            "merge_into": merge_into,
            "children_reassigned": len(children),
            "entries_reassigned": n_entries,
        },
    )


async def _op_move_entries(session, op: dict, temp_map: dict, now: datetime) -> None:
    target_id = _resolve(op.get("target_catalog_id"), temp_map)
    if target_id is not None:
        await _load_live(session, target_id)
    entry_ids = list(op.get("entry_ids") or [])
    if not entry_ids:
        raise _RejectedOp("move_entries: entry_ids empty")

    # Validate entries exist + live
    existing_rows = (
        await session.execute(
            select(FileEntry.id).where(
                FileEntry.id.in_(entry_ids),
                FileEntry.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    valid = set(existing_rows)
    moved = 0
    for eid in entry_ids:
        if eid not in valid:
            continue
        result = await session.execute(
            update(FileEntry)
            .where(FileEntry.id == eid, FileEntry.deleted_at.is_(None))
            .values(catalog_id=target_id, updated_at=now)
        )
        if result.rowcount:
            moved += 1

    if moved == 0:
        raise _RejectedOp("move_entries: no live entries matched the given ids")

    await record_outcome(
        session, task_kind=KIND_RESTRUCTURE_CATALOGS,
        object_kind="catalog", object_id=target_id or "uncategorised",
        outcome="applied",
        detail={
            "op": "move_entries",
            "target_catalog_id": target_id,
            "entries_moved": moved,
            "entries_skipped": len(entry_ids) - moved,
        },
    )
