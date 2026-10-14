"""Forge child trees: copy and authenticate ``subharnesses/`` under a parent artifact.

A Forge parent binds each child by digest in ``artifact.forge.subharnesses`` and
play time reloads it from ``<parent>/subharnesses/<name>/`` (``runtime/subharness.py``).
Any code that copies a Forge artifact to a new directory (the library snapshot,
the registry install) must carry that tree with it and prove the copy still binds.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from architect.spec import HarnessArtifact

__all__ = ["check_subharness_tree", "copy_subharness_tree"]

_SUBHARNESSES = "subharnesses"


def copy_subharness_tree(source: Path, destination: Path) -> None:
    """Copy ``subharnesses/`` recursively; links anywhere in the tree are refused."""
    tree = source / _SUBHARNESSES
    if not tree.exists() and not tree.is_symlink():
        return
    for path in (tree, *tree.rglob("*")):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError("invalid_subharness_tree")
    shutil.copytree(tree, destination / _SUBHARNESSES, symlinks=False)


def check_subharness_tree(artifact: HarnessArtifact) -> None:
    """Every bound child is present, licensed and still matches its binding digest."""
    if artifact.forge is None:
        return
    tree = artifact.directory / _SUBHARNESSES
    if tree.is_symlink():
        raise ValueError("invalid subharness tree")
    for name, ref in artifact.forge.subharnesses.items():
        directory = tree / name
        if directory.is_symlink():
            raise ValueError("invalid subharness path")
        child = HarnessArtifact.load(directory)
        if (
            not child.licensed
            or child.artifact_digest != ref.artifact_digest
            or child.spec.spec_hash != ref.spec_hash
        ):
            raise ValueError("invalid subharness")
        check_subharness_tree(child)
