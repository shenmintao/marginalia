"""Repository layer — DESIGN.md §7.

Each module here exposes module-level async functions that own all
SQLAlchemy access for a single domain. Services orchestrate; repositories
persist. Hard rule:

  - imports from `sqlalchemy*` are allowed ONLY inside this package
  - everything else (services, api, agent, tasks) goes through these modules

Functions take an `AsyncSession` as the first argument; the *caller* owns
the transaction (commit/rollback). This matches the existing convention
from services/* and lets services compose multiple repositories in one
unit of work.
"""
