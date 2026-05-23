"""Marginalia CLI — interactive REPL.

  marginalia                 # connect to localhost:8000
  python -m marginalia.cli   # same

Slash commands match Claude Code's idiom: `/help`, `/upload`, `/ls`, `/quit`.
Anything not starting with `/` is forwarded to the agent as chat.
"""
from marginalia.cli.client import CliHttpError, MarginaliaClient
from marginalia.cli.commands import (
    COMMANDS,
    CliContext,
    chat,
    dispatch,
    list_commands,
)

__all__ = [
    "CliContext",
    "CliHttpError",
    "MarginaliaClient",
    "COMMANDS",
    "chat",
    "dispatch",
    "list_commands",
]
