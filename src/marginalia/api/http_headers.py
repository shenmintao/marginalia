"""HTTP header helpers — UTF-8-safe values.

The system contract is: all user-supplied names (file names, folder
names, archive names) are UTF-8. HTTP header values, however, must be
Latin-1 encodable, so a CJK display_name in `Content-Disposition`
crashes Starlette's `init_headers`. RFC 5987's `filename*=UTF-8''…` is
percent-encoded ASCII — Latin-1-safe on the wire, decoded back to UTF-8
by every modern browser.

Use `content_disposition("inline" | "attachment", name)` everywhere a
user-supplied name lands in a header. ASCII-only names round-trip
identically; non-ASCII names finally render correctly in download
prompts."""
from __future__ import annotations

from urllib.parse import quote


def content_disposition(disposition: str, name: str) -> str:
    """Return a Content-Disposition value with a UTF-8 filename.

    `disposition` is `"inline"` or `"attachment"`. `name` may contain
    any Unicode; it is percent-encoded into the RFC 5987 `filename*`
    parameter."""
    return f"{disposition}; filename*=UTF-8''{quote(name, safe='')}"
