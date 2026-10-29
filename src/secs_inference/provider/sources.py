"""Keep SECS source selection, cache and successful-download budget policy."""

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import stat
from time import monotonic

from provider_core.api import ApiFailure
from provider_core.uploads import UploadFailed, UploadTransfer

from secs_inference.provider.source_access import InputReadError


class SourceSetChanged(InputReadError):
    """Return current membership to the same scientific selection conversation."""

    def __init__(self, members):
        self.members = members
        super().__init__("The available Uploads changed; choose from the current list: " + json.dumps(
            [asdict(member) for member in members], ensure_ascii=False,
        ))


class SourceAcquisitionFailed(RuntimeError):
    """Source access failed operationally; it is not a scientific input rejection."""

    def __init__(self, reason, *, cause=None):
        super().__init__("Cannot obtain SECS input: " + reason)
        self.cause = cause


class AttemptSources:
    """Own admitted receipts until scientific readers and original I/O settle.

    Only completed declared downloads consume SECS's aggregate source allowance.
    The shared transfer owner independently bounds each response by its grant.
    Cache and retry policy here grants no authority to parse partial files.
    """

    def __init__(self, *, job_ref, api, store, store_origin, directory: Path,
                 max_upload_bytes, max_total_bytes, retirement_seconds):
        self.job_ref, self.api, self.store = job_ref, api, store
        self.directory = directory
        self.max_upload_bytes, self.max_total_bytes = max_upload_bytes, max_total_bytes
        self.retirement_seconds = retirement_seconds
        self._deadline = self._acquisition_deadline = None
        self._directory_fd = None
        self._directory_close_failure = None
        self._created = False
        self._uploads, self.acquired = {}, {}
        self._spent_bytes = 0
        self.transfer = UploadTransfer(
            job_ref, store=store, store_origin=store_origin, remaining=self._remaining,
            before_request=lambda: None, account=lambda _count: None,
        )

    @property
    def members(self):
        return tuple(self._uploads.values())

    def _remaining(self):
        remaining = self._acquisition_deadline - monotonic()
        if remaining <= 0:
            raise SourceAcquisitionFailed("the input acquisition deadline elapsed")
        return remaining

    async def open(self, *, deadline, readers_quiescent):
        if readers_quiescent is not True:
            raise SourceAcquisitionFailed("source workspace admission requires confirmed scientific quiescence")
        if self._deadline is not None:
            raise RuntimeError("Cannot reopen an Attempt's source owner")
        self._deadline = self._acquisition_deadline = deadline
        try:
            self._open_workspace()
        except OSError as cause:
            raise SourceAcquisitionFailed("the private source workspace could not be opened", cause=cause) from None
        self._uploads = {member.upload_ref: member for member in await self._read_members()}

    def _open_workspace(self):
        if self.directory.exists() or self.directory.is_symlink():
            facts = self.directory.lstat()
            if (self.directory.resolve() != self.directory or not stat.S_ISDIR(facts.st_mode)
                    or facts.st_uid != os.geteuid() or stat.S_IMODE(facts.st_mode) != 0o700):
                raise SourceAcquisitionFailed("the retained source workspace is not a canonical caller-owned mode 0700 directory")
            # The owning Session first joins the admitted native READY exchange.
            # Only that qualified quiescence barrier permits stale-byte cleanup.
            shutil.rmtree(self.directory)
        self.directory.mkdir(mode=0o700)
        self._created = True
        self._directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    async def _read_members(self):
        delay = 1
        while True:
            try:
                async with asyncio.timeout_at(self._acquisition_deadline):
                    return (await self.api.uploads(self.job_ref)).members
            except ApiFailure as cause:
                await self.api.retire()
                if not self.api.can_retry_read(cause):
                    raise SourceAcquisitionFailed("the current Upload list could not be obtained", cause=cause) from None
                await asyncio.sleep(min(delay, self._remaining()))
                delay = min(300, delay * 2)

    async def _grant(self, selected):
        try:
            async with asyncio.timeout_at(self._acquisition_deadline):
                grant = await self.api.read_grant(self.job_ref, selected.upload_ref)
        except ApiFailure as cause:
            if cause.status == 404 and cause.problem_code == "resource_not_found":
                await self.api.retire()
                current = await self._read_members()
                change = self._adopt(current)
                if change is not None:
                    raise change from None
            raise
        if (grant.job_ref != self.job_ref or grant.upload_ref != selected.upload_ref
                or grant.byte_length != selected.byte_length
                or selected.content_hash is not None and grant.content_hash != selected.content_hash):
            raise SourceAcquisitionFailed("the read grant differs from the selected Upload identity or bytes")
        return grant

    async def acquire(self, ref, *, deadline):
        self._acquisition_deadline = min(self._deadline, deadline)
        selected = self._uploads.get(ref)
        if selected is None:
            raise InputReadError("Choose an Upload listed for this Job; the selected identity is not in that list.")
        if ref in self.acquired:
            return self.acquired[ref]
        remaining = self.max_total_bytes - self._spent_bytes
        if selected.byte_length > remaining:
            raise InputReadError(
                f"The selected {selected.byte_length}-byte Upload was not downloaded: only {remaining} bytes "
                f"remain in this Attempt's {self.max_total_bytes}-byte allowance. Earlier inspection "
                "downloads use the same allowance and cannot be discarded to make room. "
                "Choosing a ZIP member still requires its whole Upload."
            )
        if selected.byte_length > self.max_upload_bytes:
            raise SourceAcquisitionFailed(f"the selected Upload exceeds the {self.max_upload_bytes}-byte per-Upload limit")
        for attempt in range(3):
            self._remaining()
            try:
                receipt = await self.transfer.acquire_file(
                    lambda: self._grant(selected), directory_fd=self._directory_fd,
                    retirement_seconds=self.retirement_seconds,
                )
            except UploadFailed as cause:
                await self.api.retire()
                await self.store.retire(owner=self.transfer)
                if self.transfer.file_cleanup_pending:
                    await self.transfer.settle_file()
                original = cause.cause
                if isinstance(original, SourceSetChanged):
                    raise original from None
                if isinstance(original, ApiFailure):
                    retryable = self.api.can_renew_read_grant(original, job_ref=self.job_ref, upload_ref=selected.upload_ref)
                else:
                    retryable = self.transfer.can_retry_transfer(cause)
                if not retryable or attempt == 2:
                    raise SourceAcquisitionFailed("the selected Upload could not be transferred", cause=cause) from None
                await asyncio.sleep(min(1, self._remaining()))
            else:
                self.acquired[ref] = receipt
                self._spent_bytes += receipt.byte_length
                return receipt
        raise AssertionError("Acquisition retries ended without a transfer outcome")

    def _adopt(self, members):
        current = {member.upload_ref: member for member in members}
        if current == self._uploads:
            return None
        for ref, member in self._uploads.items():
            if ref in current and current[ref] != member:
                self.acquired.pop(ref, None)
        self._uploads = current
        return SourceSetChanged(members)

    async def reconcile(self):
        if not self.acquired:
            return None
        self._acquisition_deadline = self._deadline
        return self._adopt(await self._read_members())

    def evidence(self):
        return {ref: {"byte_length": receipt.byte_length, "content_hash": receipt.content_hash}
                for ref, receipt in self.acquired.items()}

    def worker_inputs(self):
        receipts = {ref: receipt for ref, receipt in self.acquired.items() if ref in self._uploads}
        return {"directory": str(self.directory),
                "receipts": {ref: asdict(receipt) for ref, receipt in receipts.items()},
                "max_upload_bytes": self.max_upload_bytes, "max_total_bytes": self.max_total_bytes}

    async def retire(self, *, readers_stopped):
        failures = []
        if self._directory_close_failure is not None:
            failures.append(self._directory_close_failure)
        operations = [self.api.retire, lambda: self.store.retire(owner=self.transfer)]
        for operation in operations:
            try:
                await operation()
            except (Exception, BaseExceptionGroup) as cause:
                failures.append(cause)
        if self.transfer.file_cleanup_pending:
            try:
                await self.transfer.settle_file()
            except (Exception, BaseExceptionGroup) as cause:
                failures.append(cause)
        if not readers_stopped:
            failures.append(SourceAcquisitionFailed("scientific reader retirement is unconfirmed; its sources remain owned"))
        if failures:
            raise BaseExceptionGroup("SECS source retirement remains unconfirmed", failures)
        if self._directory_fd is not None:
            descriptor, self._directory_fd = self._directory_fd, None
            try:
                os.close(descriptor)
            except OSError as cause:
                # The number may already have been reused despite this failure.
                self._directory_close_failure = SourceAcquisitionFailed(
                    "workspace descriptor closure is unconfirmed; retain its directory and retire the enclosing process",
                    cause=cause,
                )
                raise self._directory_close_failure from None
        if self._created:
            shutil.rmtree(self.directory)
            self._created = False
