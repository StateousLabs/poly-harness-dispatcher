"""The local stack store: <root>/<name>/<version>/config + index.json mapping refs to identity hashes."""

from __future__ import annotations

import builtins
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .._util.filelock import FileLock
from .._util.fs import robust_rmtree, write_json
from ..errors import StackConflict, StackNotFound
from ..spec import StoreEntry, hash_tree

DEFAULT_STORE_ROOT = Path.home() / ".phd" / "stacks"
IDENTITY_IGNORE = ("name", "version", "description")


def store_root(root: str | Path | None = None) -> Path:
    """`root`, else `$PHD_STACKS_ROOT`, else `~/.phd/stacks` (absolute)."""
    return Path(root or os.environ.get("PHD_STACKS_ROOT") or DEFAULT_STORE_ROOT).expanduser().resolve()


def identity_hash(config: Path) -> str:
    """The content hash of an assembled stack without `stack.json`'s name, version and description."""
    return hash_tree(config, ignore_meta=IDENTITY_IGNORE)


class StackStore:
    """Read and write the stack store. All index updates happen under `<root>/.store.lock`."""

    def __init__(self, root: str | Path | None = None):
        self.root = store_root(root)

    @property
    def _index_path(self) -> Path:
        return self.root / "index.json"

    def new_staging(self) -> Path:
        """A fresh staging folder under the store root (same volume, so the final move is atomic)."""
        path = self.root / ".staging" / uuid.uuid4().hex
        path.mkdir(parents=True)
        return path

    def put(self, staged: Path, *, name: str, version: str, ident: str, manifest: str | None,
            allow_duplicates: bool, replace: bool) -> StoreEntry:
        """Store `staged` (holding config/ and stack.lock.json) under name@version, or reuse an equal stack."""
        ref = f"{name}@{version}"
        with FileLock(self.root / ".store.lock"):
            index = self._read_index()
            record = index.get(ref)
            if record is not None and record["hash"] == ident:
                return self._entry(ref, record, ref)
            if record is not None and not replace:
                raise StackConflict(f"{ref} is already stored with different content ({record['hash']}); "
                                    "pass replace=True to overwrite it")
            if record is None and not allow_duplicates:
                same = sorted((r for r, v in index.items() if v["hash"] == ident), key=lambda r: index[r]["created_at"])
                if same:
                    return self._entry(same[0], index[same[0]], ref)
            target = self.root / name / version
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                old = self.root / ".staging" / f"replaced-{uuid.uuid4().hex}"
                os.replace(target, old)
                os.replace(staged, target)
                robust_rmtree(old)
            else:
                os.replace(staged, target)
            index[ref] = {"hash": ident, "created_at": datetime.now(timezone.utc).isoformat(), "manifest": manifest}
            self._write_index(index)
            return self._entry(ref, index[ref], ref)

    def get(self, ref: str) -> StoreEntry:
        """`"name@version"`, or `"name"` for its most recently stored version."""
        index = self._read_index()
        if "@" in ref:
            if ref not in index:
                raise StackNotFound(f"no stack {ref!r} in {self.root}")
            return self._entry(ref, index[ref], ref)
        refs = [r for r in index if r.rpartition("@")[0] == ref]
        if not refs:
            raise StackNotFound(f"no stack named {ref!r} in {self.root}")
        latest = max(refs, key=lambda r: index[r]["created_at"])
        return self._entry(latest, index[latest], latest)

    def list(self) -> builtins.list[StoreEntry]:
        index = self._read_index()
        return [self._entry(r, index[r], r) for r in sorted(index, key=lambda r: (r.rpartition("@")[0], index[r]["created_at"]))]

    def remove(self, *refs: str) -> None:
        """Remove stored stacks; nothing is removed if any ref is unknown."""
        with FileLock(self.root / ".store.lock"):
            index = self._read_index()
            missing = [r for r in refs if r not in index]
            if missing:
                raise StackNotFound(f"no stack(s) {missing} in {self.root}")
            for ref in refs:
                name, _, version = ref.rpartition("@")
                robust_rmtree(self.root / name / version)
                if (self.root / name).is_dir() and not any((self.root / name).iterdir()):
                    (self.root / name).rmdir()
                del index[ref]
            self._write_index(index)

    def prune(self) -> None:
        """Delete leftover staging folders and index entries whose folder is gone."""
        with FileLock(self.root / ".store.lock"):
            staging = self.root / ".staging"
            if staging.is_dir():
                for leftover in staging.iterdir():
                    robust_rmtree(leftover)
            index = self._read_index()
            kept = {r: v for r, v in index.items()
                    if (self.root / r.rpartition("@")[0] / r.rpartition("@")[2] / "config").is_dir()}
            if kept != index:
                self._write_index(kept)

    def _entry(self, ref: str, record: dict, requested: str) -> StoreEntry:
        name, _, version = ref.rpartition("@")
        return StoreEntry(ref=ref, name=name, version=version, hash=record["hash"], created_at=record["created_at"],
                          manifest=record.get("manifest"), path=self.root / name / version / "config",
                          requested=requested)

    def _read_index(self) -> dict:
        if not self._index_path.is_file():
            return {}
        data = json.loads(self._index_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def _write_index(self, index: dict) -> None:
        tmp = self._index_path.with_name("index.json.tmp")
        write_json(tmp, index)
        os.replace(tmp, self._index_path)
