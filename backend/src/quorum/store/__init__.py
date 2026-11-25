"""Data access for the engine: hand-written SQL over SQLAlchemy Core.

Every function takes an ``AsyncConnection`` that the caller has opened
inside a transaction (``async with engine.begin() as conn``), so related
writes (a status change, its event, a dead-letter row, a NOTIFY) commit or
roll back together. Correctness never depends on in-process state: the
database is the source of truth for leases, attempts and fencing tokens.
"""
