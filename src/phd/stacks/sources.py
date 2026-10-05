"""Fetch one component source (local path or http(s) URL) into a local file or folder; unpack archives safely."""

from __future__ import annotations

import hashlib
import http.client
import io
import os
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit

from ..errors import StackFetchError, StackInvalid
from ..spec import hash_tree
from .manifest import ENV_REF, ComponentSource

TIMEOUT_S = 60
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".zip")


@dataclass(frozen=True)
class Fetched:
    """A fetched source: a local file or folder, its natural name, and a digest of what was fetched."""

    path: Path
    name: str
    digest: str


def display_source(source: str) -> str:
    """A source for messages and provenance: URLs lose their query and fragment (pre-signed signatures)."""
    if "://" not in source:
        return source
    parts = urlsplit(source)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"{host}:{port}" if port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def expand_headers(headers: dict[str, str], what: str) -> dict[str, str]:
    """Expand `${env:NAME}` in header values from the environment."""
    out = {}
    for key, value in headers.items():
        def substitute(match, key=key):
            name = match.group(1)
            if name not in os.environ:
                raise StackInvalid(f"{what}: header {key!r} needs environment variable {name}")
            return os.environ[name]
        out[key] = ENV_REF.sub(substitute, value)
        if any(ch in out[key] for ch in "\r\n\0"):
            names = ", ".join(ENV_REF.findall(value)) or "(none; the literal value)"
            raise StackInvalid(f"{what}: header {key!r} has a line break or NUL in its value; check environment variable(s): {names}")
    return out


def fetch(src: ComponentSource, work: Path, *, what: str) -> Fetched:
    """Fetch `src` under `work` (created if needed). `what` names the manifest entry in errors."""
    work.mkdir(parents=True, exist_ok=True)
    return _fetch_url(src, work, what) if src.is_url else _fetch_local(src, work, what)


def _fetch_local(src: ComponentSource, work: Path, what: str) -> Fetched:
    path = Path(src.source)
    if not path.exists():
        raise StackFetchError(f"{what}: {path} does not exist")
    if path.is_file():
        data = path.read_bytes()
        if _is_archive_name(path.name):
            return _unpack(data, path.name, work, what)
        return Fetched(path, path.stem, _sha256(data))
    return Fetched(path, path.name, hash_tree(path))


def _fetch_url(src: ComponentSource, work: Path, what: str) -> Fetched:
    shown = display_source(src.source)
    request = urllib.request.Request(src.source, headers=expand_headers(src.headers, what))
    opener = urllib.request.build_opener(_SafeRedirect(src.headers))
    try:
        with opener.open(request, timeout=TIMEOUT_S) as response:
            data = response.read()
            final_url = response.geturl()
    except StackFetchError as e:
        raise StackFetchError(f"{what}: {shown}: {e}") from None
    except urllib.error.HTTPError as e:
        raise StackFetchError(f"{what}: {shown}: HTTP {e.code} {e.reason}") from None
    except urllib.error.URLError as e:
        reason = e.reason if isinstance(e.reason, str) else type(e.reason).__name__
        raise StackFetchError(f"{what}: {shown}: request failed ({reason})") from None
    except http.client.HTTPException as e:
        raise StackFetchError(f"{what}: {shown}: request failed ({type(e).__name__})") from None
    except (OSError, ValueError) as e:  # never echo str(e): it can carry the URL or header values
        raise StackFetchError(f"{what}: {shown}: request failed ({type(e).__name__})") from None
    filename = _safe_filename(urlsplit(final_url).path)
    if _is_archive_name(filename) or _looks_like_archive(data):
        return _unpack(data, filename, work, what)
    dest = Path(tempfile.mkdtemp(dir=work)) / filename
    dest.write_bytes(data)
    return Fetched(dest, Path(filename).stem, _sha256(data))


def _safe_filename(url_path: str) -> str:
    """Last path segment of a URL path, with backslashes treated as separators; never empty, never a traversal."""
    name = Path(url_path.replace("\\", "/")).name
    return name if name not in ("", ".", "..") else "download"


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port or {"http": 80, "https": 443}.get(scheme)
    except ValueError:
        port = None
    return scheme, (parts.hostname or "").lower(), port


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse https->http downgrades (except loopback); drop the user's headers when leaving the origin."""

    def __init__(self, user_headers: dict[str, str]) -> None:
        self._user_headers = {k.capitalize() for k in user_headers}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = _origin(req.full_url), _origin(newurl)
        if old[0] == "https" and new[0] == "http" and new[1] not in ("localhost", "127.0.0.1"):
            raise StackFetchError(f"refusing redirect from https to http ({display_source(newurl)})")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and old != new:
            for key in self._user_headers:
                redirected.headers.pop(key, None)
                redirected.unredirected_hdrs.pop(key, None)
        return redirected


def _unpack(data: bytes, filename: str, work: Path, what: str) -> Fetched:
    dest = Path(tempfile.mkdtemp(dir=work))
    try:
        if filename.lower().endswith(".zip") or (not _is_archive_name(filename) and zipfile.is_zipfile(io.BytesIO(data))):
            _unzip(data, dest, what)
        else:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
                tf.extractall(dest, filter="data")
    except tarfile.FilterError as e:
        raise StackFetchError(f"{what}: {filename}: archive entry would unpack outside its folder ({e})") from None
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as e:
        raise StackFetchError(f"{what}: {filename}: not a usable archive ({e})") from None
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():  # content inside one top-level folder
        return Fetched(entries[0], entries[0].name, _sha256(data))
    return Fetched(dest, _archive_stem(filename), _sha256(data))


def _unzip(data: bytes, dest: Path, what: str) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            target = (dest / name).resolve()
            if PurePosixPath(name).is_absolute() or not target.is_relative_to(root):
                raise StackFetchError(f"{what}: archive entry {name!r} would unpack outside its folder")
        zf.extractall(dest)


def _is_archive_name(name: str) -> bool:
    return name.lower().endswith(_ARCHIVE_SUFFIXES)


def _archive_stem(name: str) -> str:
    for suffix in _ARCHIVE_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def _looks_like_archive(data: bytes) -> bool:
    if zipfile.is_zipfile(io.BytesIO(data)):
        return True
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*"):
            return True
    except tarfile.TarError:
        return False


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
