"""Experiment tooling: open-loop load runner, orchestrator, analysis, evaluation, probes.

Nothing in this package calls a hosted provider directly except `limit_probe`, which
requires an explicit opt-in flag, its own run id and a hard call ceiling.
"""
