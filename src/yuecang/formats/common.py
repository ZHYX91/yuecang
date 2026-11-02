"""Extraction contract and bounded local resource helpers."""
from __future__ import annotations

import hashlib
import contextlib
import contextvars
import mimetypes
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from ..core.hashing import sha256_json

MAX_INPUT_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 20_000


class ExtractionError(ValueError):
    pass


class ResourceBudgetExceeded(RuntimeError):
    """A hard write limit; callers must stop, not turn this into a partial success."""


class _ResourceBudget:
    def __init__(self, directory, reserve, initial_bytes, maximum):
        self.directory = Path(directory).resolve()
        self.reserve = reserve or (lambda size: None)
        self.initial_bytes, self.maximum = initial_bytes, maximum
        self.resources = self.temporary = 0

    def update(self, *, resources=0, temporary=0):
        if self.resources + resources > self.maximum:
            raise ResourceBudgetExceeded("resources_total_limit")
        self.reserve(self.initial_bytes + self.resources + resources + self.temporary + temporary)
        self.resources += resources
        self.temporary += temporary


_resource_budget = contextvars.ContextVar("yuecang_resource_budget", default=None)


@contextlib.contextmanager
def resource_budget(directory, *, reserve=None, initial_bytes=0, maximum=MAX_ARCHIVE_BYTES):
    """One cumulative claim for an extraction, shared by all format adapters."""
    current = _resource_budget.get()
    if current is not None:
        if current.directory != Path(directory).resolve():
            raise ResourceBudgetExceeded("resource_budget_directory")
        yield current
        return
    budget = _ResourceBudget(directory, reserve, initial_bytes, maximum)
    budget.update()
    token = _resource_budget.set(budget)
    try:
        yield budget
    finally:
        _resource_budget.reset(token)


@contextlib.contextmanager
def temporary_workspace(directory, *, maximum, prefix):
    """Reserve a bounded page/OCR peak and remove it before releasing the claim."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    budget = _resource_budget.get()
    if budget:
        if root.resolve() != budget.directory:
            raise ResourceBudgetExceeded("resource_budget_directory")
        budget.update(temporary=maximum)
    try:
        with tempfile.TemporaryDirectory(prefix=prefix, dir=root) as task:
            yield Path(task)
    finally:
        if budget:
            budget.update(temporary=-maximum)


@dataclass
class ExtractionResult:
    title: str
    blocks: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    parser: str = "unknown"

    def issue(self, code: str, message: str, *, severity: str = "warning", **location) -> None:
        self.issues.append({"code": code, "message": message, "severity": severity, **location})

    def add(self, kind: str, text: str, locator: dict, *, level: int | None = None,
            attributes: dict | None = None, resources: list[dict] | None = None) -> dict:
        ordinal = len(self.blocks)
        block = {"id": "b-" + sha256_json([ordinal, kind, text, locator])[:24],
                 "type": kind, "text": text, "level": level, "locator": locator,
                 "attributes": attributes or {}, "resources": resources or []}
        self.blocks.append(block)
        return block


def checked_archive(path: Path) -> zipfile.ZipFile:
    archive = zipfile.ZipFile(path)
    try:
        entries = archive.infolist()
        if len(entries) > MAX_MEMBERS or sum(x.file_size for x in entries) > MAX_ARCHIVE_BYTES:
            raise ExtractionError("archive_size_limit")
        seen = set()
        for item in entries:
            member = PurePosixPath(item.filename)
            if (member.is_absolute() or ".." in member.parts or "\\" in item.filename
                    or ":" in item.filename or item.filename in seen):
                raise ExtractionError("unsafe_or_duplicate_archive_member")
            seen.add(item.filename)
            if item.file_size > MAX_MEMBER_BYTES:
                raise ExtractionError("archive_member_size_limit")
            if item.flag_bits & 1:
                raise ExtractionError("encrypted_archive_unsupported")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ExtractionError("archive_symlink_unsupported")
        return archive
    except Exception:
        archive.close()
        raise


def resolve_member(base: str, href: str) -> str | None:
    """Resolve archive paths without ever following an external URL."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return None
    raw = unquote(parts.path)
    if raw.startswith(("/", "\\")) or "\\" in raw or ":" in raw:
        raise ExtractionError("unsafe_resource_path")
    pieces = list(PurePosixPath(base).parent.parts)
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not pieces:
                raise ExtractionError("resource_escapes_archive")
            pieces.pop()
        else:
            pieces.append(part)
    return "/".join(pieces)


def resource_bytes(data: bytes, name: str, directory: Path | None, *, mime: str | None = None, max_bytes=MAX_MEMBER_BYTES) -> dict:
    if len(data) > max_bytes:
        raise ExtractionError("resource_size_limit")
    digest = hashlib.sha256(data).hexdigest()
    root = directory or Path(tempfile.mkdtemp(prefix="yuecang-extraction-"))
    root.mkdir(parents=True, exist_ok=True)
    suffix = Path(name).suffix[:12].lower()
    if not suffix or not suffix[1:].isalnum():
        suffix = ".bin"
    target = root / (digest + suffix)
    budget = _resource_budget.get()
    if budget and root.resolve() != budget.directory:
        raise ResourceBudgetExceeded("resource_budget_directory")
    if target.exists():
        if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ExtractionError("resource_content_collision")
    else:
        if budget:
            budget.update(resources=len(data))
        # On write failure keep the claim until the owning scratch is removed.
        with target.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    return {"path": str(target.resolve()), "name": PurePosixPath(name).name,
            "mime": mime or mimetypes.guess_type(name)[0] or "application/octet-stream",
            "sha256": digest, "bytes": len(data)}


def xml_root(data: bytes):
    from lxml import etree
    # DTDs/entities are never expanded; external resources are not loaded.
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ExtractionError("xml_doctype_unsupported")
    try:
        return etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True,
                                                           huge_tree=False, recover=False))
    except etree.XMLSyntaxError as exc:
        raise ExtractionError("xml_structure_invalid") from exc


def local_name(element) -> str:
    return str(element.tag).rsplit("}", 1)[-1]
