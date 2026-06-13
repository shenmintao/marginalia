"""pytest hook: e2e modules historically were script-style (run with
`python tests/test_xxx.py`). Their `if __name__ == "__main__":` block
calls `_create_schema()` before driving the test functions. When pytest
collects them instead, that bootstrap never runs and DB-touching tests
fail with `no such table: sessions`.

This autouse module-scoped fixture invokes the module's own
`_create_schema()` if it defines one, so the same files work under both
runners. Modules that don't need a DB (e.g. CLI-only tests) simply omit
the helper and this is a no-op.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest


_ENV_PREFIXES = (
    "AGENT_",
    "ANTHROPIC_",
    "APP_",
    "AUTO_",
    "DB_",
    "DEFAULT_",
    "LLM_",
    "MARGINALIA_",
    "OPENAI_",
    "POSTGRES_",
    "S3_",
    "STORAGE_",
    "WORKER_",
)

_MISSING = object()
_PATCH_TARGETS: dict[str, tuple[str, ...]] = {
    "marginalia.agent.runtime": ("all_tool_defs", "get_chat_client", "get_tool"),
    "marginalia.llm": ("get_audio_client", "get_chat_client", "reset_clients_cache"),
    "marginalia.llm.factory": ("get_audio_client", "get_chat_client", "reset_clients_cache"),
    "marginalia.pipelines._text_indexer": ("get_chat_client",),
    "marginalia.pipelines.archive": ("get_chat_client",),
    "marginalia.pipelines.image": ("get_chat_client",),
    "marginalia.pipelines.pdf": (
        "downscale_for_vlm",
        "get_chat_client",
        "has_vision_profile",
    ),
    "marginalia.pipelines.text": ("get_chat_client",),
    "marginalia.tasks.handlers.enrich_tags": ("get_chat_client",),
    "marginalia.tasks.handlers.normalize_tags": ("get_chat_client",),
    "marginalia.tasks.handlers.periodic_tick": ("bootstrap_periodic_tick",),
    "marginalia.tasks.handlers.propose_views": ("get_chat_client",),
    "marginalia.tasks.handlers.reflect_turn": ("get_chat_client",),
    "marginalia.tasks.handlers.refresh_entry_extra": ("get_chat_client",),
    "marginalia.tasks.handlers.restructure_catalogs": ("get_chat_client",),
    "marginalia.tasks.handlers.summarize_session": ("get_chat_client",),
    "marginalia.tasks.handlers.vet_relations": ("get_chat_client",),
}
_PATCH_BASELINE: dict[tuple[str, str], object] = {}
_SETTINGS_MODEL_CONFIG_BASELINE: dict[str, Any] | None = None
_TESTS_ROOT = Path(__file__).resolve().parent
_EXTERNAL_TESTS_ROOT = (
    Path(os.environ["MARGINALIA_TEST_TMP"]).resolve()
    if os.environ.get("MARGINALIA_TEST_TMP")
    else None
)
_RETRY_DELAYS_SECONDS = (0.05, 0.15, 0.35)
_ORIGINAL_RMTREE = shutil.rmtree


@pytest.fixture(autouse=True, scope="module")
def _bootstrap_e2e_schema(request: pytest.FixtureRequest) -> Iterator[None]:
    _restore_module_test_state(request.module)
    module_home = _module_test_home(request.module)
    fn = getattr(request.module, "_create_schema", None)
    if fn is not None:
        asyncio.run(fn())
    try:
        yield
    finally:
        _restore_module_test_state(request.module)
        if module_home is not None:
            remove_test_tree(module_home)


def remove_test_tree(path: str | os.PathLike[str]) -> None:
    """Remove an e2e temp tree with retries for Windows file-handle lag."""
    target = Path(path).resolve()
    if not _is_managed_test_tree(target) or not target.exists():
        return
    try:
        Path.cwd().resolve().relative_to(target)
    except ValueError:
        pass
    else:
        os.chdir(_TESTS_ROOT.parent)
    last_error: OSError | None = None
    for delay in (0.0, *_RETRY_DELAYS_SECONDS):
        if delay:
            time.sleep(delay)
        _relax_tree_permissions(target)
        try:
            _ORIGINAL_RMTREE(target)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


def _module_test_home(module: Any) -> Path | None:
    raw = getattr(module, "_TEST_ROOT", None)
    if raw is None:
        raw = os.environ.get("MARGINALIA_HOME")
    if raw is None:
        return None
    try:
        target = Path(raw).resolve()
    except OSError:
        return None
    return target if _is_managed_test_tree(target) else None


def _is_managed_test_tree(path: Path) -> bool:
    roots = [_TESTS_ROOT]
    if _EXTERNAL_TESTS_ROOT is not None:
        roots.append(_EXTERNAL_TESTS_ROOT)
    for root in roots:
        try:
            path.relative_to(root)
            break
        except ValueError:
            continue
    else:
        return False
    name = path.name
    return (
        name == "_e2e_data"
        or name.endswith("_e2e_data")
        or "_e2e_" in name
    )


def _retrying_test_rmtree(
    path,
    ignore_errors: bool = False,
    onerror=None,
    *,
    onexc=None,
    dir_fd=None,
):
    try:
        target = Path(path).resolve()
    except OSError:
        return _ORIGINAL_RMTREE(
            path,
            ignore_errors=ignore_errors,
            onerror=onerror,
            onexc=onexc,
            dir_fd=dir_fd,
        )
    if dir_fd is not None or not _is_managed_test_tree(target):
        return _ORIGINAL_RMTREE(
            path,
            ignore_errors=ignore_errors,
            onerror=onerror,
            onexc=onexc,
            dir_fd=dir_fd,
        )
    try:
        Path.cwd().resolve().relative_to(target)
    except ValueError:
        pass
    else:
        os.chdir(_TESTS_ROOT.parent)
    last_error: OSError | None = None
    for delay in (0.0, *_RETRY_DELAYS_SECONDS):
        if delay:
            time.sleep(delay)
        _relax_tree_permissions(target)
        try:
            return _ORIGINAL_RMTREE(
                target,
                ignore_errors=ignore_errors,
                onerror=onerror,
                onexc=onexc,
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            last_error = exc
    if ignore_errors:
        return None
    if last_error is not None:
        raise last_error
    return None


shutil.rmtree = _retrying_test_rmtree


def _relax_tree_permissions(target: Path) -> None:
    if not target.exists():
        return
    for p in (target, *target.rglob("*")):
        try:
            p.chmod(0o700)
        except OSError:
            pass


def pytest_pycollect_makeitem(collector: pytest.Collector, name: str, obj: Any):
    """Collect legacy script-style e2e modules as one pytest test.

    Many early e2e files were written as `python tests/test_x.py` scripts with
    an async `main()` / `_main()` entry point and no `test_*` functions. Pytest
    imports those modules but collects zero tests. For modules that still have
    that shape, expose the script entry point as `test_script_main` while
    leaving already-pytest-native modules alone.
    """
    module = getattr(collector, "obj", None)
    if module is not None:
        _remember_module_env(module)
    if name not in {"main", "_main"} or not callable(obj):
        return None

    if module is None or not _is_legacy_script_module(module):
        return None

    def test_script_main() -> None:
        result = obj()
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        if isinstance(result, int):
            assert result == 0

    return pytest.Function.from_parent(
        collector,
        name="test_script_main",
        callobj=test_script_main,
    )


def _is_legacy_script_module(module: Any) -> bool:
    path = getattr(module, "__file__", "") or ""
    if "test_" not in path.replace("\\", "/").rsplit("/", 1)[-1]:
        return False
    return not any(
        attr_name.startswith("test_") and _is_collectable_test_callable(value)
        for attr_name, value in vars(module).items()
    )


def _is_collectable_test_callable(value: Any) -> bool:
    return callable(value) and not isinstance(value, type)


def _remember_module_env(module: Any) -> None:
    if not inspect.ismodule(module):
        return
    if hasattr(module, "__pytest_env_snapshot__"):
        return
    snapshot = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(_ENV_PREFIXES)
    }
    module.__pytest_env_snapshot__ = snapshot


def _restore_module_test_state(module: Any) -> None:
    _restore_global_patches()
    snapshot: dict[str, str] | None = getattr(
        module, "__pytest_env_snapshot__", None,
    )
    if snapshot is not None:
        managed = {
            key for key in os.environ if key.startswith(_ENV_PREFIXES)
        } | set(snapshot)
        for key in managed:
            if key in snapshot:
                os.environ[key] = snapshot[key]
            else:
                os.environ.pop(key, None)

    from marginalia.config import get_settings
    from marginalia.db.engine import dispose_engine
    from marginalia.llm.factory import reset_clients_cache
    from marginalia.storage import reset_storage_cache

    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_clients_cache()
    reset_storage_cache()
    asyncio.run(dispose_engine())


def _capture_global_patch_baseline() -> None:
    global _SETTINGS_MODEL_CONFIG_BASELINE
    for module_name, attr_names in _PATCH_TARGETS.items():
        try:
            imported = importlib.import_module(module_name)
        except Exception:
            continue
        for attr_name in attr_names:
            _PATCH_BASELINE.setdefault(
                (module_name, attr_name),
                getattr(imported, attr_name, _MISSING),
            )

    try:
        from marginalia.config import Settings
    except Exception:
        return
    _SETTINGS_MODEL_CONFIG_BASELINE = dict(Settings.model_config)


def _restore_global_patches() -> None:
    if not _PATCH_BASELINE:
        _capture_global_patch_baseline()

    for (module_name, attr_name), original in _PATCH_BASELINE.items():
        try:
            imported = importlib.import_module(module_name)
        except Exception:
            continue
        if original is _MISSING:
            if hasattr(imported, attr_name):
                delattr(imported, attr_name)
        else:
            setattr(imported, attr_name, original)

    if _SETTINGS_MODEL_CONFIG_BASELINE is not None:
        from marginalia.config import Settings

        Settings.model_config.clear()
        Settings.model_config.update(_SETTINGS_MODEL_CONFIG_BASELINE)


_capture_global_patch_baseline()
