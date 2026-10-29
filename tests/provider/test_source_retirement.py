"""Uncertain close never retries a descriptor number that the kernel may reuse."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from secs_inference.provider.sources import AttemptSources, SourceAcquisitionFailed
from secs_inference.provider.session import SecsSession


class SourceRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_grouped_api_fatal_failure_does_not_skip_store_retirement(self):
        failure = BaseExceptionGroup("contained fixture exit", [SystemExit("private fixture detail")])
        api = SimpleNamespace(retire=AsyncMock(side_effect=failure))
        store = SimpleNamespace(retire=AsyncMock(), retirement_confirmed=True)
        sources = AttemptSources(job_ref="job:fixture", api=api, store=store,
                                 store_origin="https://store.example", directory=Path("/unused"),
                                 max_upload_bytes=1024, max_total_bytes=1024, retirement_seconds=1)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await sources.retire(readers_stopped=True)
        store.retire.assert_awaited_once_with(owner=sources.transfer)
        self.assertIs(caught.exception.exceptions[0], failure)

    async def test_grouped_chat_fatal_failure_does_not_skip_source_retirement(self):
        failure = BaseExceptionGroup("contained fixture exit", [SystemExit("private fixture detail")])
        chat = SimpleNamespace(retire=AsyncMock(side_effect=failure))
        sources = SimpleNamespace(retire=AsyncMock())
        session = SecsSession(worker=object(), sources=sources, chat=chat, work_seconds=1,
                              interpretation_seconds=1, startup_seconds=1, max_turns=1)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await session.retire()
        sources.retire.assert_awaited_once_with(readers_stopped=True)
        self.assertIs(caught.exception.exceptions[0], failure)

    async def test_failed_workspace_open_remains_operational_and_partial_directory_is_retired(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary) / "sources"
            api = SimpleNamespace(retire=AsyncMock())
            store = SimpleNamespace(retire=AsyncMock(), retirement_confirmed=True)
            sources = AttemptSources(job_ref="job:fixture", api=api, store=store,
                                     store_origin="https://store.example", directory=directory,
                                     max_upload_bytes=1024, max_total_bytes=1024, retirement_seconds=1)
            original = OSError("fixture workspace descriptor admission failed")
            with patch("os.open", side_effect=original):
                with self.assertRaises(SourceAcquisitionFailed) as caught:
                    await sources.open(deadline=monotonic() + 10, readers_quiescent=True)
            self.assertIs(caught.exception.cause, original)
            self.assertTrue(directory.exists())
            await sources.retire(readers_stopped=True)
            self.assertFalse(directory.exists())

    async def test_close_failure_retains_workspace_without_closing_a_reused_descriptor(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary) / "sources"
            api = SimpleNamespace(retire=AsyncMock(), uploads=AsyncMock(return_value=SimpleNamespace(members=[])))
            store = SimpleNamespace(retire=AsyncMock(), retirement_confirmed=True)
            sources = AttemptSources(job_ref="job:fixture", api=api, store=store,
                                     store_origin="https://store.example", directory=directory,
                                     max_upload_bytes=1024, max_total_bytes=1024, retirement_seconds=1)
            await sources.open(deadline=monotonic() + 10, readers_quiescent=True)
            original = sources._directory_fd
            close = os.close
            def uncertain_close(descriptor):
                close(descriptor)
                raise OSError("fixture close reports failure after the kernel closed it")
            with patch("os.close", side_effect=uncertain_close):
                with self.assertRaises(SourceAcquisitionFailed):
                    await sources.retire(readers_stopped=True)
            borrowed = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            self.assertEqual(borrowed, original, "Fixture must demonstrate actual descriptor reuse")
            try:
                with self.assertRaises(ExceptionGroup):
                    await sources.retire(readers_stopped=True)
                os.fstat(borrowed)
                self.assertTrue(directory.exists(), "Unconfirmed close must not release its workspace")
            finally:
                try:
                    close(borrowed)
                except OSError:
                    pass
