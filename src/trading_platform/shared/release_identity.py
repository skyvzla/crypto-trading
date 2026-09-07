"""Deterministic source identity for deployed trading-platform code.

The identity deliberately uses source bytes rather than Git metadata.  This
keeps a dirty checkout and a container with no ``.git`` directory observable
and makes the value describe the code that can actually be imported there.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


RELEASE_HASH_ALGORITHM = "sha256-source-tree-v1"
_FRAME_SEPARATOR = b"\0"


def _python_files(source_root: Path) -> tuple[Path, ...]:
    """Return Python files below ``source_root`` in stable relative order."""

    if not source_root.is_dir():
        raise ValueError(f"source root is not a directory: {source_root}")
    return tuple(
        sorted(
            (path for path in source_root.rglob("*.py") if path.is_file()),
            key=lambda path: path.relative_to(source_root).as_posix(),
        )
    )


def source_tree_sha256(source_root: str | Path) -> str:
    """Hash all Python source under ``source_root`` using path and raw bytes.

    Each file contributes a length-delimited relative path and byte payload.
    Length framing avoids ambiguous concatenations (for example ``a`` + ``bc``
    versus ``ab`` + ``c``), while sorting makes the result independent of
    filesystem traversal order.  File contents are intentionally not decoded
    or normalized: the digest identifies the exact bytes deployed.
    """

    root = Path(source_root).resolve()
    paths = _python_files(root)
    if not paths:
        raise ValueError(f"source root contains no Python files: {root}")
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(16, "big"))
        digest.update(content)
        digest.update(_FRAME_SEPARATOR)
    return digest.hexdigest()


def source_tree_identity(source_root: str | Path) -> dict[str, str]:
    """Return the stable release hash metadata stored in runtime events."""

    return {
        "strategy_release_hash": source_tree_sha256(source_root),
        "strategy_release_hash_algorithm": RELEASE_HASH_ALGORITHM,
    }
