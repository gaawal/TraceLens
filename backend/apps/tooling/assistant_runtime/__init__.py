"""TracePilot assistant runtime internals.

The public API remains ``apps.tooling.assistant``.  This package owns the
stateful execution engine so orchestration can evolve independently from the
Tool registry and HTTP views.
"""
