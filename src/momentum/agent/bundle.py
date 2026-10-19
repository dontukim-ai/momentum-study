"""Validate immutable research inputs before atomically publishing a version."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from trading_agent.contracts import AgentError, canonical, digest, timestamp

from .contracts import ResearchInput

_REQUIRED = {"prices.csv", "securities.jsonl"}
_ALLOWED = _REQUIRED | {"actions.json", "sessions.json", "calendar.json"}
_ID_FIELDS = ("schema_version", "files", "basis", "sources")
_FAILURE = "Input bundle integrity failure"


def _file_hash(path: Path) -> dict[str, Any]:
    content = path.read_bytes()
    return {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def _load(directory: Path, manifest: dict[str, Any]) -> ResearchInput:
    # Import lazily so the existing CLI parser remains the single input contract.
    from .cli import read_input

    def optional(name: str) -> Path | None:
        return directory / name if name in manifest["files"] else None

    return read_input(
        directory / "prices.csv",
        directory / "securities.jsonl",
        optional("actions.json"),
        optional("sessions.json"),
        manifest["basis"],
        optional("calendar.json"),
    )


def open_bundle(
    root: Path,
    bundle_id: str | None = None,
) -> tuple[ResearchInput, dict[str, Any]]:
    """Resolve CURRENT once; all subsequent reads stay on that immutable version."""
    try:
        if bundle_id is None:
            bundle_id = (root / "CURRENT").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{64}", bundle_id) is None:
            raise ValueError("Invalid bundle ID")
        directory = root / "bundles" / bundle_id
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if (
            set(manifest) != {*_ID_FIELDS, "created_at", "bundle_id"}
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
            or not isinstance(manifest["files"], dict)
            or not _REQUIRED <= set(manifest["files"]) <= _ALLOWED
            or not isinstance(manifest["basis"], str)
            or not isinstance(manifest["sources"], dict)
            or manifest["bundle_id"] != bundle_id
            or digest({key: manifest[key] for key in _ID_FIELDS}) != bundle_id
            or {p.name for p in directory.iterdir()}
            != {*manifest["files"], "manifest.json"}
        ):
            raise ValueError("Invalid manifest")
        timestamp(manifest["created_at"])
        for name, expected in manifest["files"].items():
            path = directory / name
            if path.is_symlink() or not path.is_file() or _file_hash(path) != expected:
                raise ValueError("File mismatch")
        return _load(directory, manifest), manifest
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        AgentError,
    ) as exc:
        raise AgentError(_FAILURE) from exc


def publish_bundle(
    root: Path,
    files: dict[str, Path],
    *,
    basis: str,
    sources: dict[str, Any],
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    """Publish only parsed inputs; never rewrite an existing bundle directory."""
    if not isinstance(sources, dict):
        raise AgentError("Bundle sources must be a metadata object")
    if not _REQUIRED <= set(files) <= _ALLOWED:
        raise AgentError(
            "Bundle requires prices.csv and securities.jsonl; unknown file"
        )
    staging_root = root / "staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=staging_root))
    pointer: Path | None = None
    try:
        for name, source in files.items():
            shutil.copyfile(source, staging / name)
        identity = {
            "schema_version": 1,
            "files": {name: _file_hash(staging / name) for name in sorted(files)},
            "basis": basis,
            "sources": sources,
        }
        bundle_id = digest(identity)
        manifest = {
            **identity,
            "created_at": clock().isoformat(),
            "bundle_id": bundle_id,
        }
        # Validate the publication clock before installing an immutable directory;
        # failed validation must leave both CURRENT and existing versions intact.
        timestamp(manifest["created_at"])
        (staging / "manifest.json").write_text(
            canonical(manifest) + "\n", encoding="utf-8"
        )
        _load(staging, manifest)
        bundles = root / "bundles"
        bundles.mkdir(exist_ok=True)
        target = bundles / bundle_id
        if not target.exists():
            try:
                staging.rename(target)
            except OSError:
                # A concurrent publisher may have installed this same ID.
                if not target.exists():
                    raise
        # Also verify pre-existing directories, including incomplete ones, before
        # publishing their ID. An empty existing directory must never be replaced.
        _, existing = open_bundle(root, bundle_id)
        if any(existing[key] != manifest[key] for key in _ID_FIELDS):
            raise AgentError(_FAILURE)
        manifest = existing
        descriptor, name = tempfile.mkstemp(dir=root, prefix=".CURRENT-")
        pointer = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(bundle_id + "\n")
        os.replace(pointer, root / "CURRENT")
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if pointer is not None:
            pointer.unlink(missing_ok=True)
