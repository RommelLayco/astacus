"""
Copyright (c) 2020 Aiven Ltd
See LICENSE for details

Common base classes for the plugins

"""
from __future__ import annotations

from astacus.common import exceptions, ipc, magic, utils
from astacus.common.asyncstorage import AsyncHexDigestStorage, AsyncJsonStorage
from astacus.common.utils import AstacusModel
from astacus.coordinator.cluster import Cluster, Result
from astacus.coordinator.config import CoordinatorNode
from astacus.coordinator.manifest import download_backup_manifest
from collections import Counter
from typing import Any, Counter as TCounter, Dict, Generic, List, Optional, Set, Type, TypeVar

import dataclasses
import datetime
import logging

logger = logging.getLogger(__name__)

T = TypeVar("T")
StepResult = TypeVar("StepResult", covariant=True)


class CoordinatorPlugin(AstacusModel):
    def get_backup_steps(self, *, context: OperationContext) -> List[Step]:
        raise NotImplementedError

    def get_restore_steps(self, *, context: OperationContext, req: ipc.RestoreRequest) -> List[Step]:
        raise NotImplementedError


@dataclasses.dataclass
class OperationContext:
    storage_name: str
    json_storage: AsyncJsonStorage
    hexdigest_storage: AsyncHexDigestStorage


class Step(Generic[StepResult]):
    async def run_step(self, cluster: Cluster, context: StepsContext) -> StepResult:
        raise NotImplementedError


class StepFailedError(Exception):
    pass


class StepsContext:
    def __init__(self, *, attempt: int = 1, attempt_start: Optional[datetime.datetime] = None):
        self.attempt = attempt
        self.attempt_start = utils.now() if attempt_start is None else attempt_start
        self.step_results: Dict[Type[Step], Any] = {}

    @property
    def backup_name(self) -> str:
        iso = self.attempt_start.isoformat(timespec="seconds")
        return f"{magic.JSON_BACKUP_PREFIX}{iso}"

    def get_result(self, step_class: Type[Step[T]]) -> T:
        return self.step_results[step_class]

    def set_result(self, step_class: Type[Step[T]], result: T) -> None:
        if step_class in self.step_results:
            raise RuntimeError(f"result already set for step {step_class}")
        self.step_results[step_class] = result


@dataclasses.dataclass
class SnapshotStep(Step[List[ipc.SnapshotResult]]):
    """
    Request a snapshot of all files matching the `snapshot_root_globs`, on each nodes.

    The snapshot for each file contains its path, size, modification time and hash,
    see `SnapshotFile` for details.
    """
    snapshot_root_globs: List[str]

    async def run_step(self, cluster: Cluster, context: StepsContext) -> List[ipc.SnapshotResult]:
        req = ipc.SnapshotRequest(root_globs=self.snapshot_root_globs)
        start_results = await cluster.request_from_nodes("snapshot", method="post", caller="SnapshotStep", req=req)
        return await cluster.wait_successful_results(
            start_results=start_results, result_class=ipc.SnapshotResult, required_successes=len(start_results)
        )


@dataclasses.dataclass
class ListHexdigestsStep(Step[Set[str]]):
    """
    Fetch the list of all files already present in object storage, identified by their hexdigest.
    """
    hexdigest_storage: AsyncHexDigestStorage

    async def run_step(self, cluster: Cluster, context: StepsContext) -> Set[str]:
        return set(await self.hexdigest_storage.list_hexdigests())


@dataclasses.dataclass
class UploadBlocksStep(Step[List[ipc.SnapshotUploadResult]]):
    """
    Upload to object storage all files that are not yet in that storage.

    The list of files to upload comes from the snapshot taken on each node during
    the `SnapshotStep`, the list of files already uploaded come from the `ListHexdigestsStep`.

    If multiple nodes have the same files (according to their hexdigest, the path is ignored),
    each file will be uploaded only once, with an effort to distribute the work fairly among
    all nodes.

    This returns a list of `SnapshotUploadResult`, one for each node, that collects statistics
    about the uploads.
    """
    storage_name: str

    async def run_step(self, cluster: Cluster, context: StepsContext) -> List[ipc.SnapshotUploadResult]:
        node_index_datas = build_node_index_datas(
            hexdigests=context.get_result(ListHexdigestsStep),
            snapshots=context.get_result(SnapshotStep),
            node_indices=list(range(len(cluster.nodes))),
        )
        return await upload_node_index_datas(cluster, self.storage_name, node_index_datas)


@dataclasses.dataclass
class UploadManifestStep(Step[None]):
    """
    Store the backup manifest in the object storage.

    The backup manifest contains the snapshot from the `SnapshotStep` as well as the
    statistics collected by the `UploadBlocksStep` and the plugin manifest.
    """
    json_storage: AsyncJsonStorage
    plugin: ipc.Plugin
    plugin_manifest_step: Optional[Type[Step[Dict]]] = None
    snapshot_step: Optional[Type[Step[List[ipc.SnapshotResult]]]] = SnapshotStep
    upload_step: Optional[Type[Step[List[ipc.SnapshotUploadResult]]]] = UploadBlocksStep

    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        plugin_data = context.get_result(self.plugin_manifest_step) if self.plugin_manifest_step else {}
        manifest = ipc.BackupManifest(
            attempt=context.attempt,
            start=context.attempt_start,
            snapshot_results=context.get_result(self.snapshot_step) if self.snapshot_step else [],
            upload_results=context.get_result(self.upload_step) if self.upload_step else [],
            plugin=self.plugin,
            plugin_data=plugin_data,
        )
        logger.debug("Storing backup manifest %s", context.backup_name)
        await self.json_storage.upload_json(context.backup_name, manifest)


@dataclasses.dataclass
class BackupNameStep(Step[str]):
    """
    Select the name of the backup to restore.

    If the backup name was not specified in the restore request, this will select the
    most recent backup available in object storage, and fail if there are no backup.
    """
    json_storage: AsyncJsonStorage
    requested_name: str

    async def run_step(self, cluster: Cluster, context: StepsContext) -> str:
        if not self.requested_name:
            return sorted(await self.json_storage.list_jsons())[-1]
        if self.requested_name.startswith(magic.JSON_BACKUP_PREFIX):
            return self.requested_name
        return f"{magic.JSON_BACKUP_PREFIX}{self.requested_name}"


@dataclasses.dataclass
class BackupManifestStep(Step[ipc.BackupManifest]):
    """
    Download the backup manifest from object storage.
    """
    json_storage: AsyncJsonStorage

    async def run_step(self, cluster: Cluster, context: StepsContext) -> ipc.BackupManifest:
        backup_name = context.get_result(BackupNameStep)
        assert backup_name
        return await download_backup_manifest(self.json_storage, backup_name)


@dataclasses.dataclass
class RestoreStep(Step[List[ipc.NodeResult]]):
    """
    Request each node to download and restore all files listed in the backup manifest.
    """
    storage_name: str
    partial_restore_nodes: Optional[List[ipc.PartialRestoreRequestNode]] = None

    async def run_step(self, cluster: Cluster, context: StepsContext) -> List[ipc.NodeResult]:
        # AZ distribution should in theory be forced to match, but in
        # practise it doesn't really matter. So we restore nodes 'as
        # well as we can' and hope that is well enough (or whoever
        # configures us may lie about the real availability zone of
        # the nodes anyway).

        backup_name = context.get_result(BackupNameStep)
        backup_manifest = context.get_result(BackupManifestStep)
        snapshot_results = backup_manifest.snapshot_results

        node_to_backup_index = get_node_to_backup_index(
            partial_restore_nodes=self.partial_restore_nodes,
            snapshot_results=snapshot_results,
            nodes=cluster.nodes,
        )
        start_results: List[Optional[Result]] = []
        for node, backup_index in zip(cluster.nodes, node_to_backup_index):
            if backup_index is not None:
                # Restore whatever was backed up
                node_request: ipc.NodeRequest = ipc.SnapshotDownloadRequest(
                    storage=self.storage_name,
                    backup_name=backup_name,
                    snapshot_index=backup_index,
                    root_globs=snapshot_results[backup_index].state.root_globs,
                )
                op = "download"
            elif self.partial_restore_nodes:
                # If partial restore, do not clear other nodes
                continue
            else:
                node_request = ipc.SnapshotClearRequest(root_globs=snapshot_results[0].state.root_globs)
                op = "clear"
            start_result = await cluster.request_from_nodes(
                op, caller="RestoreSnapshotStep", method="post", req=node_request, nodes=[node]
            )
            if len(start_result) != 1:
                return []
            start_results.extend(start_result)
        return await cluster.wait_successful_results(start_results=start_results, result_class=ipc.NodeResult)


def get_node_to_backup_index(
    *,
    partial_restore_nodes: Optional[List[ipc.PartialRestoreRequestNode]],
    snapshot_results: List[ipc.SnapshotResult],
    nodes: List[CoordinatorNode],
) -> List[Optional[int]]:
    if partial_restore_nodes:
        return get_node_to_backup_index_from_partial_restore_nodes(
            partial_restore_nodes=partial_restore_nodes,
            snapshot_results=snapshot_results,
            nodes=nodes,
        )
    covered_nodes = len(snapshot_results)
    configured_nodes = len(nodes)
    if configured_nodes < covered_nodes:
        missing_nodes = covered_nodes - configured_nodes
        raise exceptions.InsufficientNodesException(f"{missing_nodes} node(s) missing - unable to restore backup")

    azs_in_backup = Counter(result.az for result in snapshot_results)
    azs_in_nodes = Counter(node.az for node in nodes)
    if len(azs_in_backup) > len(azs_in_nodes):
        azs_missing = len(azs_in_backup) - len(azs_in_nodes)
        raise exceptions.InsufficientAZsException(f"{azs_missing} az(s) missing - unable to restore backup")

    return get_node_to_backup_index_from_azs(
        snapshot_results=snapshot_results,
        nodes=nodes,
        azs_in_backup=azs_in_backup,
        azs_in_nodes=azs_in_nodes,
    )


def get_node_to_backup_index_from_partial_restore_nodes(
    *, partial_restore_nodes: List[ipc.PartialRestoreRequestNode], snapshot_results: List[ipc.SnapshotResult],
    nodes: List[CoordinatorNode]
) -> List[Optional[int]]:
    node_to_backup_index: List[Optional[int]] = [None] * len(nodes)
    hostname_to_backup_index: Dict[Optional[str], int] = {}
    url_to_node_index: Dict[Optional[str], int] = {}
    for i, node in enumerate(nodes):
        url_to_node_index[node.url] = i
    for i, res in enumerate(snapshot_results):
        hostname_to_backup_index[res.hostname] = i
    for req_node in partial_restore_nodes:
        node_index = req_node.node_index
        if node_index is not None:
            num_nodes = len(nodes)
            if node_index < 0 or node_index >= num_nodes:
                raise exceptions.NotFoundException(
                    f"Invalid node_index in partial restore: Must be 0 <= {node_index} < {num_nodes}"
                )
        else:
            node_index = url_to_node_index.get(req_node.node_url)
            if node_index is None:
                raise exceptions.NotFoundException(
                    f"Partial restore url {req_node.node_url} not found in active configuration"
                )
        backup_index = req_node.backup_index
        if backup_index is not None:
            num_backup_nodes = len(snapshot_results)
            if backup_index < 0 or backup_index >= num_backup_nodes:
                raise exceptions.NotFoundException(
                    f"Invalid backup_index in partial restore: Must be 0 <= {backup_index} < {num_backup_nodes}"
                )
        else:
            backup_index = hostname_to_backup_index.get(req_node.backup_hostname)
            if backup_index is None:
                raise exceptions.NotFoundException(
                    f"Partial restore hostname {req_node.backup_hostname} not found in backup manifest"
                )
        node_to_backup_index[node_index] = backup_index
    return node_to_backup_index


def get_node_to_backup_index_from_azs(
    *,
    snapshot_results: List[ipc.SnapshotResult],
    nodes: List[CoordinatorNode],
    azs_in_backup: TCounter[str],
    azs_in_nodes: TCounter[str],
) -> List[Optional[int]]:
    node_to_backup_index: List[Optional[int]] = [None] * len(nodes)
    # This is strictly speaking just best-effort assignment
    for (backup_az, backup_n), (node_az, node_n) in zip(azs_in_backup.most_common(), azs_in_nodes.most_common()):
        if backup_n > node_n:
            missing_n = backup_n - node_n
            raise exceptions.InsufficientNodesException(
                f"AZ {node_az}, to be restored from {backup_az}, is missing {missing_n} nodes"
            )

        for backup_index, snapshot_result in enumerate(snapshot_results):
            if snapshot_result.az != backup_az:
                continue
            for node_index, node in enumerate(nodes):
                if node.az != node_az or node_to_backup_index[node_index] is not None:
                    continue
                node_to_backup_index[node_index] = backup_index
                break
    return node_to_backup_index


class NodeIndexData(ipc.AstacusModel):
    node_index: int
    sshashes: List[ipc.SnapshotHash] = []
    total_size: int = 0

    def append_sshash(self, sshash: ipc.SnapshotHash) -> None:
        self.total_size += sshash.size
        self.sshashes.append(sshash)


def build_node_index_datas(*, hexdigests, snapshots: List[ipc.SnapshotResult],
                           node_indices: List[int]) -> List[NodeIndexData]:
    assert len(snapshots) == len(node_indices)
    sshash_to_node_indexes: Dict[ipc.SnapshotHash, List[int]] = {}
    for i, snapshot_result in enumerate(snapshots):
        for snapshot_hash in snapshot_result.hashes or []:
            sshash_to_node_indexes.setdefault(snapshot_hash, []).append(i)

    node_index_datas = [NodeIndexData(node_index=node_index) for node_index in node_indices]

    # This is not really optimal algorithm, but probably good enough.

    # Allocate the things based on first off, how often they show
    # up (the least common first), and then reverse size order, to least loaded node.
    def _sshash_to_node_indexes_key(item):
        (sshash, indexes) = item
        return len(indexes), -sshash.size

    todo = sorted(sshash_to_node_indexes.items(), key=_sshash_to_node_indexes_key)
    for snapshot_hash, node_indexes in todo:
        if snapshot_hash.hexdigest in hexdigests:
            continue
        _, node_index = min((node_index_datas[node_index].total_size, node_index) for node_index in node_indexes)
        node_index_datas[node_index].append_sshash(snapshot_hash)
    return [data for data in node_index_datas if data.sshashes]


async def upload_node_index_datas(cluster: Cluster, storage_name: str, node_index_datas: List[NodeIndexData]):
    logger.debug("upload_node_index_datas")
    start_results: List[Optional[Result]] = []
    for data in node_index_datas:
        req = ipc.SnapshotUploadRequest(hashes=data.sshashes, storage=storage_name)
        start_result = await cluster.request_from_nodes(
            "upload", caller="upload_node_index_datas", method="post", req=req, nodes=[cluster.nodes[data.node_index]]
        )
        if len(start_result) != 1:
            raise StepFailedError("upload failed")
        start_results.extend(start_result)
    return await cluster.wait_successful_results(start_results=start_results, result_class=ipc.SnapshotUploadResult)
