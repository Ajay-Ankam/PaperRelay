"""Capacity control: request admission, provider-call quota gate, retry policy, fault injection.

Scope: one Python process and one asyncio event loop. All state here is in-memory except
the daily accounting file; running several API workers would give each worker its own
gate and is NOT coordinated (see docs/capacity.md).
"""
