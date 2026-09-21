"""Bound member access to acquired files without trusting archive output paths."""

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from lzma import LZMAError
import json
from pathlib import Path
import stat
import struct
from zipfile import BadZipFile, ZipExtFile, ZipFile, ZipInfo, is_zipfile
import zlib

from secs_inference.provider.input_operations import SourceRef


class InputReadError(ValueError):
    """The requested source cannot be read; its safe reason may guide correction."""


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """One admitted logical object without an archive-controlled host path."""

    source: SourceRef
    byte_length: int


@dataclass(frozen=True, slots=True)
class ReadSource:
    """Bytes and identity established by SourceAccess."""

    source: SourceRef
    contents: bytes
    digest: str


class ScopeLimitError(InputReadError):
    """One attributed source would exceed the aggregate inspection budget."""

    def __init__(self, source: SourceRef, limit: int):
        super().__init__(f"Cannot inspect this source: its expanded members exceed the {limit}-byte inspection limit")
        self.source = source


class ScopeReader:
    """Materialize one discovery scope under a shared expanded-byte limit."""

    def __init__(self, access: "SourceAccess"):
        self._access = access
        self._remaining = access.max_scope_bytes

    def read(self, source: SourceRef) -> ReadSource:
        entry = self._access.scope(source)[0]
        if entry.byte_length > self._remaining:
            raise ScopeLimitError(source, self._access.max_scope_bytes)
        read = self._access.read(source)
        if len(read.contents) > self._remaining:
            raise ScopeLimitError(source, self._access.max_scope_bytes)
        self._remaining -= len(read.contents)
        return read


class SourceAccess:
    """Attempt-owned acquired files; all materialized paths remain private.

    The execution owner must stop active readers before releasing this object
    or its acquired files. This class does not acquire network authority.
    """

    def __init__(self, files: dict[str, Path], directory: Path, *,
                 max_member_bytes: int = 128 * 1024 * 1024,
                 max_scope_bytes: int = 256 * 1024 * 1024):
        self.files = files
        self.directory = directory
        self.max_member_bytes = max_member_bytes
        self.max_scope_bytes = max_scope_bytes

    def scope_reader(self) -> ScopeReader:
        """Create one aggregate budget for an inspection and its companions."""
        return ScopeReader(self)

    def scope(self, source: SourceRef) -> tuple[SourceEntry, ...]:
        """Enumerate a root scope or admit one exact member without format policy."""
        path = self._upload(source.upload_ref)
        if source.member is not None:
            with self._archive(path) as archive:
                info = self._member(archive, source.member)
            return (SourceEntry(source, info.file_size),)
        if not is_zipfile(path):
            return (SourceEntry(source, path.stat().st_size),)
        with self._archive(path) as archive:
            infos = archive.infolist()
            if len(infos) > 4096:
                raise InputReadError("Cannot inspect this ZIP: it exceeds the 4096-member inspection limit")
            entries = tuple(
                SourceEntry(SourceRef(source.upload_ref, info.filename), info.file_size)
                for info in infos if not info.is_dir()
            )
            if sum(entry.byte_length for entry in entries) > self.max_scope_bytes:
                raise InputReadError(
                    f"Cannot inspect this ZIP: its members exceed the {self.max_scope_bytes}-byte inspection limit"
                )
            listing = {"members": [
                {"name": entry.source.member, "byte_length": entry.byte_length}
                for entry in entries
            ]}
            if len(json.dumps(listing, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
                raise InputReadError(
                    "Cannot inspect this ZIP: its member listing exceeds the 262144-byte inspection limit; "
                    "an exact member can still be inspected if known"
                )
            return entries

    def read(self, source: SourceRef) -> ReadSource:
        """Read one admitted object once, enforcing the decoder byte limit."""
        chunks = []
        total = 0
        with self.open(source) as stream:
            while chunk := _read_chunk(stream, 64 * 1024):
                total += len(chunk)
                if total > self.max_member_bytes:
                    raise InputReadError(
                        f"Cannot read the selected source: it exceeds the {self.max_member_bytes}-byte member limit"
                    )
                chunks.append(chunk)
        contents = b"".join(chunks)
        return ReadSource(source, contents, "sha256:" + sha256(contents).hexdigest())

    @contextmanager
    def open(self, source: SourceRef):
        """Open selected bytes, rejecting ambiguous or nonregular ZIP members."""
        path = self._upload(source.upload_ref)
        if source.member is None:
            with path.open("rb") as stream:
                yield stream
            return
        with self._archive(path) as archive:
            info = self._member(archive, source.member)
            try:
                stream = archive.open(info)
            except (BadZipFile, NotImplementedError, RuntimeError, UnicodeDecodeError) as error:
                raise InputReadError("Cannot read the selected ZIP member: decoding or integrity checking failed") from error
            with stream:
                yield stream

    def _upload(self, ref: str) -> Path:
        try:
            return self.files[ref]
        except KeyError:
            raise InputReadError("Cannot read the selected source: choose an Upload listed for this Job") from None

    @contextmanager
    def _archive(self, path: Path):
        self._preflight_archive(path)
        try:
            archive = ZipFile(path)
        except (BadZipFile, UnicodeDecodeError) as error:
            raise InputReadError("Cannot inspect archive members: the selected Upload is not a readable ZIP") from error
        with archive:
            yield archive

    def _preflight_archive(self, path: Path) -> None:
        """Reject excessive entry counts before Python loads the central directory."""
        with path.open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - 65_557))
            tail = stream.read()
            eocd = tail.rfind(b"PK\x05\x06")
            if eocd < 0 or len(tail) - eocd < 22:
                return
            disk, central_disk, disk_entries, total_entries = struct.unpack_from("<4H", tail, eocd + 4)
            if disk or central_disk or disk_entries != total_entries:
                raise InputReadError("Cannot inspect this ZIP: multi-disk archives are unsupported")
            if total_entries == 0xFFFF:
                locator = tail.rfind(b"PK\x06\x07", 0, eocd)
                if locator < 0 or len(tail) - locator < 20:
                    raise InputReadError("Cannot inspect this ZIP: its ZIP64 entry count is unavailable")
                zip64_disk, zip64_offset, total_disks = struct.unpack_from("<IQI", tail, locator + 4)
                if zip64_disk or total_disks != 1:
                    raise InputReadError("Cannot inspect this ZIP: multi-disk archives are unsupported")
                stream.seek(zip64_offset)
                record = stream.read(56)
                if len(record) < 56 or not record.startswith(b"PK\x06\x06"):
                    raise InputReadError("Cannot inspect this ZIP: its ZIP64 entry count is unavailable")
                total_entries = struct.unpack_from("<Q", record, 32)[0]
            if total_entries > 4096:
                raise InputReadError("Cannot inspect this ZIP: it exceeds the 4096-member inspection limit")

    def _member(self, archive: ZipFile, name: str) -> ZipInfo:
        matches = [info for info in archive.infolist() if info.filename == name]
        if len(matches) != 1:
            raise InputReadError("Cannot read the selected ZIP member: its name is missing or repeated")
        info = matches[0]
        mode = info.external_attr >> 16
        if info.is_dir() or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
            raise InputReadError("Cannot read the selected ZIP member: it is not a regular file")
        if info.flag_bits & 1:
            raise InputReadError("Cannot read the selected ZIP member: encrypted members are unsupported")
        if info.file_size > self.max_member_bytes:
            raise InputReadError(f"Cannot read the selected ZIP member: it exceeds the {self.max_member_bytes}-byte member limit")
        return info


def _read_chunk(stream, size: int) -> bytes:
    """Translate failures only while decoding bytes, not around the consumer."""
    try:
        return stream.read(size)
    except (BadZipFile, zlib.error, LZMAError, EOFError, OSError) as error:
        # bz2 reports corrupt compressed data as an errno-less OSError.
        # Translate that form only for ZIP decoding; filesystem errors and
        # unclassified errors from direct-file reads remain operational.
        if isinstance(error, OSError) and (not isinstance(stream, ZipExtFile) or error.errno is not None):
            raise
        raise InputReadError("Cannot read the selected ZIP member: decoding or integrity checking failed") from error
