from pathlib import Path

import pytest

from trading_platform.shared.release_identity import (
    RELEASE_HASH_ALGORITHM,
    source_tree_identity,
    source_tree_sha256,
)


def test_source_tree_hash_is_stable_and_uses_relative_paths(tmp_path: Path):
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "b.py").write_bytes(b"b\n")
    (root / "a.py").write_bytes(b"a\n")
    (root / "ignored.txt").write_bytes(b"ignored")

    first = source_tree_sha256(root)
    second = source_tree_sha256(root)

    assert first == second
    assert len(first) == 64
    (root / "ignored.txt").write_bytes(b"changed")
    assert source_tree_sha256(root) == first
    (root / "pkg" / "b.py").write_bytes(b"changed\n")
    assert source_tree_sha256(root) != first


def test_source_tree_hash_changes_when_a_relative_path_changes(tmp_path: Path):
    root = tmp_path / "src"
    (root / "one.py").parent.mkdir(parents=True)
    (root / "one.py").write_bytes(b"same")
    first = source_tree_sha256(root)
    (root / "one.py").rename(root / "two.py")
    assert source_tree_sha256(root) != first


def test_source_tree_identity_declares_sha256():
    identity = source_tree_identity(
        Path(__file__).parents[2] / "src" / "trading_platform"
    )
    assert identity["strategy_release_hash_algorithm"] == RELEASE_HASH_ALGORITHM
    assert len(identity["strategy_release_hash"]) == 64


def test_source_tree_hash_does_not_depend_on_current_working_directory(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "src"
    root.mkdir()
    (root / "module.py").write_bytes(b"module")
    monkeypatch.chdir(tmp_path / "..")
    first = source_tree_sha256(root)
    monkeypatch.chdir(tmp_path)
    assert source_tree_sha256(root) == first


def test_source_tree_hash_rejects_empty_source_root(tmp_path: Path):
    with pytest.raises(ValueError, match="no Python files"):
        source_tree_sha256(tmp_path)
