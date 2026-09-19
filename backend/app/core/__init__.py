"""Cross-cutting foundations: settings, logging setup, typed exceptions.

`core` may not import from `api`, `agents` or `services` — it is the bottom
of the dependency graph.
"""
