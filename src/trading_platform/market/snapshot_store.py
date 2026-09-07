"""Compatibility exports for the Ledger campaign snapshot reader.

The implementation lives in :mod:`trading_platform.market.live_snapshot`;
this module keeps the API's lazy import stable while the market service uses
the more descriptive implementation name.
"""

from .live_snapshot import (
    LiveSnapshotManager,
    LiveSnapshotStore,
    SnapshotCapture,
    SnapshotManifest,
    read_campaign_snapshot_candles,
)

__all__ = [
    "LiveSnapshotManager",
    "LiveSnapshotStore",
    "SnapshotCapture",
    "SnapshotManifest",
    "read_campaign_snapshot_candles",
]
