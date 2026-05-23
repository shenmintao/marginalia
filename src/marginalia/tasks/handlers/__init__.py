"""Task handler registry.

Importing this package registers all built-in handlers via their decorators.
"""
from marginalia.tasks.handlers import enrich_tags  # noqa: F401
from marginalia.tasks.handlers import ingest_file  # noqa: F401
from marginalia.tasks.handlers import lifecycle  # noqa: F401
from marginalia.tasks.handlers import mine_corpus_evidence  # noqa: F401
from marginalia.tasks.handlers import mine_session_cooccurrence  # noqa: F401
from marginalia.tasks.handlers import normalize_tags  # noqa: F401
from marginalia.tasks.handlers import periodic_tick  # noqa: F401
from marginalia.tasks.handlers import propose_views  # noqa: F401
from marginalia.tasks.handlers import prune_audit_events  # noqa: F401
from marginalia.tasks.handlers import prune_task_outcomes  # noqa: F401
from marginalia.tasks.handlers import purge_deleted_files  # noqa: F401
from marginalia.tasks.handlers import recover_stuck_tasks  # noqa: F401
from marginalia.tasks.handlers import reflect_turn  # noqa: F401
from marginalia.tasks.handlers import refresh_entry_extra  # noqa: F401
from marginalia.tasks.handlers import restructure_catalogs  # noqa: F401
