from __future__ import annotations


ENTRY_METADATA_FTS_TABLE = "entry_metadata_fts"
ENTRY_METADATA_FTS_TRIGGERS: tuple[str, ...] = (
    "entry_metadata_fts_file_entries_ai",
    "entry_metadata_fts_file_entries_au",
    "entry_metadata_fts_file_entries_ad",
    "entry_metadata_fts_files_au",
    "entry_metadata_fts_files_ad",
)
