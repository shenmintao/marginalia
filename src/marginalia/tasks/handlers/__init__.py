"""Task handler registry.

Importing this package registers all built-in handlers via their decorators.
The miner / lifecycle / prune / tag_quality submodules don't register on
their own — they're imported only as helper functions by their parent
dispatcher kinds (mine_relations, suggest_lifecycle, prune, tag_quality).
"""
from marginalia.tasks.handlers import ingest_file  # noqa: F401
from marginalia.tasks.handlers import mine_relations  # noqa: F401
from marginalia.tasks.handlers import periodic_tick  # noqa: F401
from marginalia.tasks.handlers import propose_views  # noqa: F401
from marginalia.tasks.handlers import prune  # noqa: F401
from marginalia.tasks.handlers import purge_deleted_files  # noqa: F401
from marginalia.tasks.handlers import recover_stuck_tasks  # noqa: F401
from marginalia.tasks.handlers import rebuild_semantic_index  # noqa: F401
from marginalia.tasks.handlers import reflect_turn  # noqa: F401
from marginalia.tasks.handlers import refresh_entry_extra  # noqa: F401
from marginalia.tasks.handlers import restructure_catalogs  # noqa: F401
from marginalia.tasks.handlers import suggest_lifecycle  # noqa: F401
from marginalia.tasks.handlers import summarize_session  # noqa: F401
from marginalia.tasks.handlers import tag_quality  # noqa: F401
from marginalia.tasks.handlers import vet_relations  # noqa: F401
from marginalia.tasks.handlers import webdav_publish  # noqa: F401
