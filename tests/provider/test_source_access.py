"""Archive members are source identities, never extraction paths."""

from pathlib import Path
import errno
from io import BytesIO
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
from zipfile import ZipFile, ZipInfo, ZipExtFile, ZIP_BZIP2, ZIP_LZMA

from secs_inference.provider.input_operations import SourceRef
from secs_inference.provider.source_access import InputReadError, SourceAccess, _read_chunk


class SourceAccessTests(unittest.TestCase):
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
                access.inspect(SourceRef("upload:chosen"))
            with access.materialize({"spectrum.jdx": SourceRef("upload:chosen", member)}) as materialized:
                self.assertEqual((materialized / "spectrum.jdx").read_text(), "spectrum")

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
                    access.inspect(source)
                with self.assertRaises(InputReadError):
                    with access.materialize({"spectrum.jdx": source}):
                        self.fail("Corrupt bytes reached the reader")

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
                    access.inspect(SourceRef("upload:chosen", "é.jdx"))

    def test_inspection_limit_does_not_block_an_exact_member_selection(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "upload"
            with ZipFile(path, "w") as archive:
                for index in range(4097):
                    archive.writestr(str(index), "spectrum")
            access = SourceAccess({"upload:chosen": path}, root)
            with self.assertRaisesRegex(InputReadError, "inspection limit"):
                access.inspect(SourceRef("upload:chosen"))
            with access.materialize({"spectrum.jdx": SourceRef("upload:chosen", "4096")}) as materialized:
                self.assertEqual((materialized / "spectrum.jdx").read_text(), "spectrum")

    def test_inspection_keeps_multiple_experiments_and_materializes_only_the_choice(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "upload"
            with ZipFile(archive_path, "w") as archive:
                archive.writestr("first/pdata/1/procs", "carbon")
                archive.writestr("second/pdata/1/procs", "proton")
            access = SourceAccess({"upload:chosen": archive_path}, root)
            facts = access.inspect(SourceRef("upload:chosen"))
            self.assertEqual([item["name"] for item in facts["members"]], [
                "first/pdata/1/procs", "second/pdata/1/procs",
            ])
            with access.materialize({"procs": SourceRef("upload:chosen", "second/pdata/1/procs")}) as materialized:
                self.assertEqual((materialized / "procs").read_text(), "proton")
                self.assertEqual([p.name for p in materialized.iterdir()], ["procs"])
            self.assertFalse(materialized.exists())

    def test_source_member_names_cannot_control_output_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "upload"
            with ZipFile(path, "w") as archive:
                archive.writestr("../../escape", "selected bytes")
            access = SourceAccess({"upload:chosen": path}, root)
            with access.materialize({"spectrum.jdx": SourceRef("upload:chosen", "../../escape")}) as materialized:
                self.assertEqual((materialized / "spectrum.jdx").read_text(), "selected bytes")
                self.assertEqual(set(root.iterdir()), {path, materialized})

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
                    access.inspect(SourceRef("upload:chosen", member))
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
