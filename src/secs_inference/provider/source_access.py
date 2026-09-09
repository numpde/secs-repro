"""Bound member access to acquired files without trusting archive output paths."""

from contextlib import contextmanager
from lzma import LZMAError
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipExtFile, ZipFile, ZipInfo, is_zipfile
import zlib

from secs_inference.provider.input_operations import SourceRef


class InputReadError(ValueError):
    """The requested source cannot be read; its safe reason may guide correction."""


class SourceAccess:
    """Attempt-owned acquired files; all materialized paths remain private.

    The execution owner must stop active readers before releasing this object
    or its acquired files. This class does not acquire network authority.
    """

    def __init__(self, files: dict[str, Path], directory: Path, *, max_member_bytes: int = 128 * 1024 * 1024):
        self.files = files
        self.directory = directory
        self.max_member_bytes = max_member_bytes

    def inspect(self, source: SourceRef) -> dict:
        """Show all archive choices, or a bounded prefix; never rank datasets."""
        path = self._upload(source.upload_ref)
        if source.member is None and is_zipfile(path):
            with self._archive(path) as archive:
                if len(archive.infolist()) > 4096:
                    raise InputReadError("Cannot inspect this ZIP: it exceeds the 4096-member inspection limit")
                return {"members": [
                    {"name": info.filename, "byte_length": info.file_size}
                    for info in archive.infolist() if not info.is_dir()
                ]}
        with self.open(source) as stream:
            prefix = _read_chunk(stream, 16 * 1024 + 1)
        return {"text_prefix": prefix[:16 * 1024].decode("utf-8", errors="replace"),
                "prefix_truncated": len(prefix) > 16 * 1024}

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

    @contextmanager
    def materialize(self, sources: dict[str, SourceRef]):
        """Write reader-owned filenames, not archive-provided output paths."""
        with TemporaryDirectory(dir=self.directory, prefix="reader-") as directory:
            root = Path(directory)
            for filename, source in sources.items():
                with self.open(source) as incoming, (root / filename).open("xb") as output:
                    total = 0
                    while chunk := _read_chunk(incoming, 64 * 1024):
                        total += len(chunk)
                        if total > self.max_member_bytes:
                            raise InputReadError(f"Cannot read the selected source: it exceeds the {self.max_member_bytes}-byte member limit")
                        output.write(chunk)
            yield root

    def _upload(self, ref: str) -> Path:
        try:
            return self.files[ref]
        except KeyError:
            raise InputReadError("Cannot read the selected source: choose an Upload listed for this Job") from None

    @contextmanager
    def _archive(self, path: Path):
        try:
            archive = ZipFile(path)
        except (BadZipFile, UnicodeDecodeError) as error:
            raise InputReadError("Cannot inspect archive members: the selected Upload is not a readable ZIP") from error
        with archive:
            yield archive

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


def bruker_sources(upload_ref: str, pdata_directory: str) -> dict[str, SourceRef]:
    """Address the chosen processed pair; do not select another experiment."""
    prefix = pdata_directory + "/" if pdata_directory else ""
    return {name: SourceRef(upload_ref, prefix + name) for name in ("1r", "procs")}


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
