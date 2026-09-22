"""Archive members are source identities, never extraction paths."""

from pathlib import Path
import errno
from io import BytesIO
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile, ZipInfo, ZipExtFile, ZIP_BZIP2, ZIP_LZMA

from secs_inference.provider.input_operations import SourceRef
from secs_inference.provider.source_access import InputReadError, ScopeLimitError, SourceAccess, _read_chunk


class SourceAccessTests(unittest.TestCase):
    def test_scope_reader_enforces_growth_during_streaming_and_exhausts_the_scope(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "upload"
            path.write_bytes(b"x")
            access = SourceAccess({"upload:chosen": path}, root,
                                  max_member_bytes=16, max_scope_bytes=2)
            actual_scope = access.scope

            def measure_then_grow(source):
                entries = actual_scope(source)
                path.write_bytes(b"grown")
                return entries

            reader = access.scope_reader()
            with patch.object(access, "scope", side_effect=measure_then_grow):
                with self.assertRaises(ScopeLimitError):
                    reader.read(SourceRef("upload:chosen"))
            path.write_bytes(b"x")
            with self.assertRaises(ScopeLimitError):
                reader.read(SourceRef("upload:chosen"))

    def test_oversized_inventory_is_rejected_without_disabling_exact_member_access(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "upload"
            with ZipFile(path, "w") as archive:
                for index in range(33):
                    member = "x" * 64000 + str(index)
                    archive.writestr(member, "spectrum")
            access = SourceAccess({"upload:chosen": path}, root)
            with self.assertRaisesRegex(InputReadError, "262144-byte inspection limit"):
                access.scope(SourceRef("upload:chosen"))
            self.assertEqual(access.read(SourceRef("upload:chosen", member)).contents, b"spectrum")

    def test_corrupt_compressed_members_are_input_rejections(self):
        for compression, offset in ((ZIP_BZIP2, 0), (ZIP_LZMA, 4)):
            with self.subTest(compression=compression), TemporaryDirectory() as directory:
                root = Path(directory)
                data = BytesIO()
                with ZipFile(data, "w", compression=compression) as archive:
                    archive.writestr("spectrum.jdx", "spectrum " * 300)
                raw = bytearray(data.getvalue())
                # Damage the codec header, leaving the ZIP directory readable.
                payload = 30 + int.from_bytes(raw[26:28], "little") + int.from_bytes(raw[28:30], "little")
                raw[payload + offset] ^= 255
                path = root / "upload"
                path.write_bytes(raw)
                access = SourceAccess({"upload:chosen": path}, root)
                source = SourceRef("upload:chosen", "spectrum.jdx")
                with self.assertRaises(InputReadError):
                    access.read(source)

    def test_filesystem_read_errors_are_not_bad_input(self):
        for stream, error in ((Mock(spec=ZipExtFile), OSError(errno.EIO, "disk read failed")),
                              (Mock(), OSError("unclassified read failure"))):
            with self.subTest(error=error):
                stream.read.side_effect = error
                with self.assertRaises(OSError) as caught:
                    _read_chunk(stream, 1024)
                self.assertIs(caught.exception, error)

    def test_malformed_zip_name_encoding_is_a_source_rejection(self):
        for damaged_header in (b"PK\x01\x02", b"PK\x03\x04"):
            with self.subTest(header=damaged_header), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "upload"
                with ZipFile(path, "w") as archive:
                    archive.writestr("é.jdx", "spectrum")
                raw = bytearray(path.read_bytes())
                name_start = raw.index("é.jdx".encode(), raw.index(damaged_header))
                raw[name_start] = 0xff
                path.write_bytes(raw)
                access = SourceAccess({"upload:chosen": path}, root)
                with self.assertRaises(InputReadError):
                    access.read(SourceRef("upload:chosen", "é.jdx"))

    def test_member_limit_applies_before_exact_member_central_directory_loading(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "upload"
            with ZipFile(path, "w") as archive:
                for index in range(4097):
                    archive.writestr(str(index), "spectrum")
            access = SourceAccess({"upload:chosen": path}, root)
            with self.assertRaisesRegex(InputReadError, "inspection limit"):
                access.scope(SourceRef("upload:chosen"))
            with self.assertRaisesRegex(InputReadError, "inspection limit"):
                access.read(SourceRef("upload:chosen", "4096"))

    def test_inspection_keeps_multiple_experiments_and_materializes_only_the_choice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "upload"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("first/pdata/1/procs", "carbon")
                archive.writestr("second/pdata/1/procs", "proton")
            access = SourceAccess({"upload:chosen": archive_path}, root)
            entries = access.scope(SourceRef("upload:chosen"))
            self.assertEqual([item.source.member for item in entries], [
                "first/pdata/1/procs", "second/pdata/1/procs",
            ])
            self.assertEqual(access.read(SourceRef("upload:chosen", "second/pdata/1/procs")).contents, b"proton")

    def test_source_member_names_cannot_control_output_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "upload"
            with ZipFile(path, "w") as archive:
                archive.writestr("../../escape", "selected bytes")
            access = SourceAccess({"upload:chosen": path}, root)
            self.assertEqual(access.read(SourceRef("upload:chosen", "../../escape")).contents, b"selected bytes")
            self.assertEqual(set(root.iterdir()), {path})

    def test_symlink_and_overlarge_members_are_not_readable_files(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "upload"
            with ZipFile(path, "w") as archive:
                link = ZipInfo("link")
                link.external_attr = 0o120777 << 16
                archive.writestr(link, "/etc/passwd")
                archive.writestr("large", "x" * 20)
            access = SourceAccess({"upload:chosen": path}, root, max_member_bytes=10)
            for member in ("link", "large", "missing"):
                with self.subTest(member=member), self.assertRaises(InputReadError) as caught:
                    access.read(SourceRef("upload:chosen", member))
                if member == "large":
                    self.assertIn("10-byte member limit", str(caught.exception))

    def test_consumer_bug_is_not_translated_to_a_reading_rejection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "upload"
            with ZipFile(path, "w") as archive:
                archive.writestr("member", "text")
            access = SourceAccess({"upload:chosen": path}, root)
            with self.assertRaisesRegex(RuntimeError, "consumer bug"):
                with access.open(SourceRef("upload:chosen", "member")):
                    raise RuntimeError("consumer bug")
