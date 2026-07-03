"""`marginalia storage migrate` — switch backends in place.

Usage:
    marginalia storage migrate --from local --to mirror
    marginalia storage migrate --from mirror --to local

Plan: walk files (excluding deleted), per row:
  1. Read bytes from current backend at file.storage_key.
  2. Determine new key shape for target backend.
  3. Write to target backend at the new key.
  4. UPDATE files SET storage_key=new_key.
  5. Verify new read works.
  6. Delete old object.

Resumable: if storage_key is already in the target shape, skip the row.
Crash-safe: each row commits independently. A killed migration can be
resumed by re-running the command — only un-migrated rows will move.

S3 paths are deferred — would need explicit credentials for both
endpoints, plus presigned-url considerations. Local↔mirror only.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys


_VALID = ("local", "mirror")


def _is_uuid_flat(key: str) -> bool:
    """LocalStorage shape: '<2hex>/<2hex>/<uuid>'."""
    parts = key.split("/")
    if len(parts) != 3:
        return False
    p0, p1, uuid = parts
    if len(p0) != 2 or len(p1) != 2:
        return False
    if not (all(c in "0123456789abcdef" for c in p0)
            and all(c in "0123456789abcdef" for c in p1)):
        return False
    return len(uuid) == 36 and uuid.count("-") == 4


def _is_mirror_shape(key: str) -> bool:
    """Mirror shape: relative path ending in something with an extension
    OR at least not matching the UUID-flat pattern."""
    return not _is_uuid_flat(key)


def cmd_storage_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="marginalia storage",
        description="Storage backend operations.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    mig = sub.add_parser("migrate", help="Migrate file bytes between backends.")
    mig.add_argument("--from", dest="src", choices=_VALID, required=True)
    mig.add_argument("--to", dest="dst", choices=_VALID, required=True)
    mig.add_argument(
        "--dry-run", action="store_true",
        help="Print migration plan without writing.",
    )
    args = parser.parse_args(argv)
    if args.cmd != "migrate":
        parser.print_help()
        return 2
    if args.src == args.dst:
        print(f"--from and --to are both {args.src!r}; nothing to do.")
        return 0
    return asyncio.run(_run_migrate(args.src, args.dst, dry_run=args.dry_run))


async def _run_migrate(
    src: str, dst: str, *, dry_run: bool,
) -> int:
    from marginalia.config import get_settings
    from marginalia.db.engine import get_session_factory
    from marginalia.repositories import files as files_repo
    from marginalia.storage.local import LocalStorage
    from marginalia.storage.mirror import MirrorStorage

    settings = get_settings()
    if settings.storage_backend != dst:
        print(
            f"WARNING: STORAGE_BACKEND env is {settings.storage_backend!r}, "
            f"but you asked to migrate TO {dst!r}. Proceeding under the\n"
            f"assumption that you'll set STORAGE_BACKEND={dst} after this "
            f"runs. If you don't, the next launch will fail the consistency\n"
            f"check.\n"
        )

    src_storage = (
        LocalStorage(settings.local_storage_root) if src == "local"
        else MirrorStorage(settings.mirror_vault_root)
    )
    dst_storage = (
        LocalStorage(settings.local_storage_root) if dst == "local"
        else MirrorStorage(settings.mirror_vault_root)
    )

    factory = get_session_factory()
    moved = 0
    skipped = 0
    failed = 0

    async with factory() as session:
        rows = await files_repo.list_live_storage_keys(session)

    print(f"[migrate] {len(rows)} files to consider ({src} → {dst})")
    for file_id, storage_key, sha256 in rows:
        # Skip rows already in the target shape (resumability). Local
        # UUID-flat keys have the form 'aa/bb/<uuid>'; mirror keys end
        # with a real filename including an extension and don't begin
        # with the 2-char prefix that storage_prefix() produces.
        in_target_shape = (
            (dst == "local" and _is_uuid_flat(storage_key))
            or (dst == "mirror" and _is_mirror_shape(storage_key))
        )
        if in_target_shape:
            skipped += 1
            continue

        try:
            new_key = await _migrate_one(
                session_factory=factory,
                file_id=file_id,
                old_key=storage_key,
                sha256=sha256,
                src_storage=src_storage,
                dst_storage=dst_storage,
                dst_kind=dst,
                dry_run=dry_run,
            )
            print(f"  [ok] {storage_key!r:48s} → {new_key!r}")
            moved += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  [fail] file_id={file_id} {storage_key!r}: {exc}",
                  file=sys.stderr)
            failed += 1

    print(f"\n[migrate] done. moved={moved} skipped={skipped} failed={failed}")
    if not dry_run:
        print("Now set STORAGE_BACKEND=" + dst + " in your env and restart.")
    return 0 if failed == 0 else 1


async def _migrate_one(
    *,
    session_factory,
    file_id: str,
    old_key: str,
    sha256: str | None,
    src_storage,
    dst_storage,
    dst_kind: str,
    dry_run: bool,
) -> str:
    from marginalia.repositories import entries as entries_repo
    from marginalia.repositories import files as files_repo

    # Pick a reasonable display_name + folder_path hint for the mirror
    # side from the FIRST live file_entry pointing at this file.
    async with session_factory() as session:
        entry = await entries_repo.find_first_live_for_file(session, file_id)
        display_name = entry.display_name if entry else None
        folder_id = entry.folder_id if entry else None
        folder_path: str | None = None
        if folder_id is not None:
            from marginalia.services.entries import _build_folder_display_path
            folder_path = await _build_folder_display_path(session, folder_id)

    # Stream-read from src, tallying bytes so the verify step can
    # compare sizes (0-byte files are legitimate).
    copied = 0

    async def _stream():
        nonlocal copied
        async for chunk in src_storage.get(old_key):
            copied += len(chunk)
            yield chunk

    if dry_run:
        return f"<dry-run {dst_kind}>"

    # Write to dst
    if dst_kind == "local":
        # Use a fresh UUID-flat key derived from sha256-ish prefix.
        from marginalia.utils.ids import new_id, storage_prefix
        new_id_str = new_id()
        prefix = storage_prefix(new_id_str)
        if isinstance(prefix, tuple):
            prefix = "/".join(prefix)
        new_key = f"{prefix}/{new_id_str}"
        result_key = await dst_storage.put(
            new_key, _stream(),
            display_name=display_name, folder_path=folder_path,
        )
    else:
        # Mirror: dst_storage will compute the path from hints.
        result_key = await dst_storage.put(
            "ignored", _stream(),
            display_name=display_name or "unnamed",
            folder_path=folder_path,
        )

    # Verify read back: byte count must match what we copied, and
    # sha256 must match the files row when one is recorded.
    hasher = hashlib.sha256()
    read_back = 0
    async for chunk in dst_storage.get(result_key):
        read_back += len(chunk)
        hasher.update(chunk)
    if read_back != copied:
        raise RuntimeError(
            f"post-migrate size mismatch: wrote {copied} bytes, "
            f"read back {read_back}"
        )
    if sha256 and hasher.hexdigest() != sha256:
        raise RuntimeError(
            f"post-migrate sha256 mismatch: expected {sha256}, "
            f"got {hasher.hexdigest()}"
        )

    # Update db
    async with session_factory() as session:
        await files_repo.update_storage_key(
            session, file_id=file_id, storage_key=result_key,
        )
        await session.commit()

    # Delete old object only after db is committed
    try:
        await src_storage.delete(old_key)
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] could not remove old object {old_key!r}: {exc}",
              file=sys.stderr)

    return result_key
