"""Pure domain logic: no database, network or clock access.

Everything here is deterministic given its inputs (randomness is injected),
so it is covered by unit and property-based tests.
"""
