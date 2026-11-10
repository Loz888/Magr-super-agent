"""Versioned source contracts; importing this package starts no listener or worker."""

from mgar_v3.sources.domain import SOURCE_CONTRACT_VERSION, SOURCE_EVENT_VERSION

__all__ = ["SOURCE_CONTRACT_VERSION", "SOURCE_EVENT_VERSION"]
