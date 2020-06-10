"""

Copyright (c) 2020 Aiven Ltd
See LICENSE for details

General restore utilities that are product independent.

The basic file restoration steps should be implementable by using the
API of this module with proper parameters.

"""

from .node import NodeOp
from .snapshotter import Snapshotter
from astacus.common import ipc, utils
from typing import Dict, List, Optional

import base64
import logging
import os
import shutil

logger = logging.getLogger(__name__)


class Downloader:
    def __init__(self, *, dst, snapshotter, storage, parallel):
        self.dst = dst
        self.snapshotter = snapshotter
        self.storage = storage
        self.parallel = parallel

    def _snapshotfile_already_exists(self, snapshotfile: ipc.SnapshotFile) -> bool:
        relative_path = snapshotfile.relative_path
        existing_snapshotfile = self.snapshotter.relative_path_to_snapshotfile.get(relative_path)
        return existing_snapshotfile and existing_snapshotfile.equals_excluding_mtime(snapshotfile)

    def _download_snapshotfile(self, snapshotfile: ipc.SnapshotFile):
        if self._snapshotfile_already_exists(snapshotfile):
            return
        relative_path = snapshotfile.relative_path
        download_path = self.dst / relative_path
        download_path.parent.mkdir(parents=True, exist_ok=True)
        with download_path.open("wb") as f:
            if snapshotfile.hexdigest:
                self.storage.download_hexdigest_to_file(snapshotfile.hexdigest, f)
            else:
                assert snapshotfile.content_b64
                f.write(base64.b64decode(snapshotfile.content_b64))
        os.utime(download_path, ns=(snapshotfile.mtime_ns, snapshotfile.mtime_ns))

    def _copy_snapshotfile(self, snapshotfile_src: ipc.SnapshotFile, snapshotfile: ipc.SnapshotFile):
        if self._snapshotfile_already_exists(snapshotfile):
            return
        src_path = self.dst / snapshotfile_src.relative_path
        dst_path = self.dst / snapshotfile.relative_path
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src_path, dst_path)
        os.utime(dst_path, ns=(snapshotfile.mtime_ns, snapshotfile.mtime_ns))

    def download_from_storage(self, *, progress, snapshotstate: ipc.SnapshotState, still_running_callback=lambda: True):
        hexdigest_to_snapshotfiles: Dict[str, List[ipc.SnapshotFile]] = {}
        valid_relative_path_set = set()
        for snapshotfile in snapshotstate.files:
            valid_relative_path_set.add(snapshotfile.relative_path)
            if snapshotfile.hexdigest:
                hexdigest_to_snapshotfiles.setdefault(snapshotfile.hexdigest, []).append(snapshotfile)

        self.snapshotter.snapshot()
        # TBD: Error checking, what to do if we're told to restore to existing directory?
        progress.start(sum(1 + snapshotfile.file_size for snapshotfile in snapshotstate.files))
        for snapshotfile in snapshotstate.files:
            if not snapshotfile.hexdigest:
                self._download_snapshotfile(snapshotfile)
                progress.download_success(snapshotfile.file_size + 1)
        all_snapshotfiles = hexdigest_to_snapshotfiles.values()

        def _cb(*, map_in, map_out):
            snapshotfiles = map_in
            progress.download_success((snapshotfiles[0].file_size + 1) * len(snapshotfiles))
            return still_running_callback()

        if not utils.parallel_map_to(
            fun=self._download_snapshotfiles_from_storage, iterable=all_snapshotfiles, result_callback=_cb, n=self.parallel
        ):
            progress.add_fail()
            progress.done()
            return

        # Delete files that were not supposed to exist
        for relative_path in self.snapshotter.relative_path_to_snapshotfile.keys():
            if relative_path not in valid_relative_path_set:
                absolute_path = self.dst / relative_path
                absolute_path.unlink()

        # This operation is done. It may or may not have been a success.
        progress.done()

    def _download_snapshotfiles_from_storage(self, snapshotfiles):
        self._download_snapshotfile(snapshotfiles[0])

        # We don't report progress for these, as local copying
        # should be ~instant
        for snapshotfile in snapshotfiles[1:]:
            self._copy_snapshotfile(snapshotfiles[0], snapshotfile)


class DownloadOp(NodeOp):
    snapshotter: Optional[Snapshotter] = None

    def start(self, *, req: ipc.SnapshotDownloadRequest):
        self.req = req
        self.snapshotter = self.get_or_create_snapshotter(req.state.root_globs)
        logger.debug("start_download %r", req)
        return self.start_op(op_name="download", op=self, fun=self.download)

    def download(self):
        assert self.snapshotter
        downloader = Downloader(
            dst=self.config.root,
            snapshotter=self.snapshotter,
            storage=self.storage,
            parallel=self.config.parallel_downloads
        )
        downloader.download_from_storage(
            snapshotstate=self.req.state, progress=self.result.progress, still_running_callback=self.still_running_callback
        )
