"""A failed cache publication preserves both completed generations."""

import errno
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from publish_wheelhouse import publish


class WheelhousePublicationTests(unittest.TestCase):
    def directories(self, root):
        stage, destination = root / "stage", root / "published"
        stage.mkdir()
        destination.mkdir()
        (stage / ".complete").write_text("new")
        (destination / ".complete").write_text("old")
        return stage, destination

    def test_actual_exchange_keeps_old_generation_until_caller_cleanup(self):
        with TemporaryDirectory() as temporary:
            stage, destination = self.directories(Path(temporary))
            publish(stage, destination)
            self.assertEqual((destination / ".complete").read_text(), "new")
            self.assertEqual((stage / ".complete").read_text(), "old")

    def test_exchange_failure_never_deletes_the_published_cache(self):
        with TemporaryDirectory() as temporary:
            stage, destination = self.directories(Path(temporary))
            original = OSError(errno.EIO, "fixture exchange failure")
            with patch("publish_wheelhouse._exchange", side_effect=original):
                with self.assertRaises(OSError) as caught:
                    publish(stage, destination)
            self.assertIs(caught.exception, original)
            self.assertEqual((destination / ".complete").read_text(), "old")
            self.assertEqual((stage / ".complete").read_text(), "new")

    def test_first_publication_moves_the_completed_stage(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir()
            (stage / ".complete").write_text("new")
            publish(stage, root / "published")
            self.assertFalse(stage.exists())
            self.assertEqual((root / "published/.complete").read_text(), "new")

    def test_aliases_of_one_directory_cannot_be_published_then_deleted_as_stage(self):
        with TemporaryDirectory() as temporary:
            stage, _destination = self.directories(Path(temporary))
            with self.assertRaisesRegex(ValueError, "same directory"):
                publish(stage / "../stage", stage)
            self.assertEqual((stage / ".complete").read_text(), "new")
