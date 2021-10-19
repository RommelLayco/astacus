"""
Copyright (c) 2021 Aiven Ltd
See LICENSE for details
"""
from .client import ClickHouseClient, escape_sql_identifier, escape_sql_string
from .config import ClickHouseConfiguration
from .dependencies import access_entities_sorted_by_dependencies, tables_sorted_by_dependencies
from .escaping import escape_for_file_name, unescape_from_file_name
from .manifest import AccessEntity, ClickHouseManifest, ReplicatedDatabase, Table
from .parts import check_parts_replication, distribute_parts_to_servers, get_frozen_parts_pattern, group_files_into_parts
from .zookeeper import ChangeWatch, NodeExistsError, ZooKeeperClient
from astacus.common import ipc
from astacus.common.exceptions import TransientException
from astacus.coordinator.cluster import Cluster
from astacus.coordinator.plugins.base import BackupManifestStep, SnapshotStep, Step, StepFailedError, StepsContext
from pathlib import Path
from typing import cast, Dict, List, Set, Tuple

import asyncio
import dataclasses
import logging
import uuid

logger = logging.getLogger(__name__)

DatabasesAndTables = Tuple[List[ReplicatedDatabase], List[Table]]

TABLES_LIST_QUERY = """SELECT
    system.databases.name,
    system.tables.name, system.tables.engine, system.tables.uuid, system.tables.create_table_query,
    arrayZip(system.tables.dependencies_database, system.tables.dependencies_table)
FROM system.databases LEFT JOIN system.tables ON system.tables.database == system.databases.name
WHERE
    system.databases.engine == 'Replicated'
    AND NOT system.tables.is_temporary
ORDER BY (system.databases.name,system.tables.name)
SETTINGS show_table_uuid_in_table_create_query_if_not_nil=true
"""


@dataclasses.dataclass
class ValidateConfigStep(Step[None]):
    """
    Validates that we have the same number of astacus node and clickhouse nodes.
    """
    clickhouse: ClickHouseConfiguration

    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        if len(self.clickhouse.nodes) != len(cluster.nodes):
            raise StepFailedError("Inconsistent number of nodes in the config")


@dataclasses.dataclass
class RetrieveAccessEntitiesStep(Step[List[AccessEntity]]):
    """
    Backups access entities (user, roles, quotas, row_policies, settings profiles) and their grants
    from ZooKeeper. This requires using the replicated storage engine for users.

    Inside the `access_entities_path` ZooKeeper node, there is one child znode for each type of
    access entity: each one with a single letter uppercase name (`P`, `Q`, `R`, `S`, `U`).

    Inside that same znode, there is also a child znode named `uuid`.

    Inside each single letter znode, there is one child znode for each entity of that type,
    the key is the entity name (escaped for zookeeper), the value is the entity uuid.

    Inside the `uuid` znode node, there is one child for each entity, the key is the entity uuid
    and the value is the SQL queries required to recreate that entity. Some entities have more
    than one query because they need separate queries to add grants related to the entity.
    """
    zookeeper_client: ZooKeeperClient
    access_entities_path: str

    async def run_step(self, cluster: Cluster, context: StepsContext) -> List[AccessEntity]:
        access_entities = []
        async with self.zookeeper_client.connect() as connection:
            change_watch = ChangeWatch()
            entity_types = await connection.get_children(self.access_entities_path, watch=change_watch)
            for entity_type in entity_types:
                if entity_type != "uuid":
                    entity_type_path = f"{self.access_entities_path}/{entity_type}"
                    node_names = await connection.get_children(entity_type_path, watch=change_watch)
                    for node_name in node_names:
                        uuid_bytes = await connection.get(f"{entity_type_path}/{node_name}", watch=change_watch)
                        entity_uuid = uuid.UUID(uuid_bytes.decode())
                        entity_path = f"{self.access_entities_path}/uuid/{entity_uuid}"
                        attach_query_bytes = await connection.get(entity_path, watch=change_watch)
                        access_entities.append(
                            AccessEntity(
                                type=entity_type,
                                uuid=entity_uuid,
                                name=unescape_from_file_name(node_name),
                                attach_query=attach_query_bytes.decode(),
                            )
                        )
            if change_watch.has_changed:
                # With care, we could instead look at what exactly changed and just update the minimum
                raise TransientException("Concurrent modification during access entities retrieval")
        return access_entities


@dataclasses.dataclass
class RetrieveDatabasesAndTablesStep(Step[DatabasesAndTables]):
    """
    Retrieves the list of all databases that use the replicated database engine and their tables.

    The table names, uuids and schemas of all tables are collected but the databases
    uuids are not collected.

    The shard and replica options of the replicated database engine are also not
    collected, the restore operation uses the `{shard}` and `{replica}` macro and assumes
    that each server of the cluster has values for them.

    This assumes that all servers of the cluster have created the same replicated
    databases (with the same database name pointing on the same ZooKeeper
    node), and relies on that to query only the first server of the cluster.
    """
    clients: List[ClickHouseClient]

    async def run_step(self, cluster: Cluster, context: StepsContext) -> DatabasesAndTables:
        clickhouse_client = self.clients[0]
        # We fetch everything in a single query, we don't have to care about consistency within that step.
        # However, the schema could be modified between now and the freeze step.
        databases: Dict[str, ReplicatedDatabase] = {}
        tables: List[Table] = []
        rows = await clickhouse_client.execute(TABLES_LIST_QUERY)
        for db_name, table_name, table_engine, table_uuid, table_query, dependencies in rows:
            if db_name not in databases:
                assert isinstance(db_name, str)
                databases[db_name] = ReplicatedDatabase(name=db_name)
            # Thanks to the LEFT JOIN, an empty database without table will still return a row.
            # Unlike standard SQL, the table properties will have a default value instead of NULL,
            # that's why we skip tables with an empty name.
            # We need these rows and the LEFT JOIN that makes them: we want to list all
            # Replicated databases, including those without any table.
            if table_name != "":
                tables.append(
                    Table(
                        database=db_name,
                        name=table_name,
                        engine=table_engine,
                        uuid=uuid.UUID(cast(str, table_uuid)),
                        create_query=table_query,
                        dependencies=dependencies,
                    )
                )
        databases_list = sorted(databases.values(), key=lambda d: d.name)
        return databases_list, tables


@dataclasses.dataclass
class CreateClickHouseManifestStep(Step[ClickHouseManifest]):
    """
    Collects access entities, databases and tables from previous steps into a `ClickHouseManifest`.
    """
    async def run_step(self, cluster: Cluster, context: StepsContext) -> ClickHouseManifest:
        databases, tables = context.get_result(RetrieveDatabasesAndTablesStep)
        return ClickHouseManifest(
            access_entities=context.get_result(RetrieveAccessEntitiesStep),
            replicated_databases=databases,
            tables=tables,
        )


@dataclasses.dataclass
class RemoveFrozenTablesStep(Step[None]):
    """
    Removes traces of previous backups that might have failed.
    """
    freeze_name: str

    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        root_globs = get_frozen_parts_pattern(self.freeze_name)
        node_request = ipc.SnapshotClearRequest(root_globs=[root_globs])
        start_results = await cluster.request_from_nodes(
            "clear", caller="RemoveFrozenTablesStep", method="post", req=node_request
        )
        await cluster.wait_successful_results(start_results=start_results, result_class=ipc.NodeResult)


@dataclasses.dataclass
class FreezeUnfreezeTablesStepBase(Step[None]):
    clients: List[ClickHouseClient]
    freeze_name: str

    @property
    def operation(self) -> str:
        # It's a bit silly to have this as a property but it let's us keep using dataclass like all other steps
        raise NotImplementedError

    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        _, tables = context.get_result(RetrieveDatabasesAndTablesStep)
        for table in tables:
            if table.requires_freezing:
                # We only run it on the first client because the `ALTER TABLE (UN)FREEZE` is replicated
                await self.clients[0].execute(
                    f"ALTER TABLE {table.escaped_sql_identifier} "
                    f"{self.operation} WITH NAME {escape_sql_string(self.freeze_name)}"
                )


@dataclasses.dataclass
class FreezeTablesStep(FreezeUnfreezeTablesStepBase):
    """
    Creates a frozen copy of the tables that won't change while we are uploading parts of it.

    Each table is frozen separately, one after the other. This means the complete backup of all
    tables will not represent a single, globally consistent, point in time.

    The frozen copy is done using hardlink and does not cost extra disk space (ClickHouse can
    use hardlinks because parts files never change after they are created).

    This does *not* lock the table or disable writes on the live table, this just makes the backup
    not see writes done after the `ALTER TABLE FREEZE` command.

    The frozen copy is stored in a `shadow/{freeze_name}` folder inside the ClickHouse data
    directory. This directory will be scanned by the `SnapshotStep`. However we will need to write
    it in a different place when restoring the backup (see `MoveFrozenPartsStep`).
    """
    @property
    def operation(self) -> str:
        return "FREEZE"


class UnfreezeTablesStep(FreezeUnfreezeTablesStepBase):
    """
    Removes the frozen parts after we're done uploading them.

    Frozen leftovers don't immediately harm ClickHouse or cost disk space since they are
    hardlinks to the parts used by the real table. However, as ClickHouse starts mutating
    the table and replaces existing parts with new ones, these frozen parts will take disk
    space. `ALTER TABLE UNFREEZE` removes these unused parts.
    """
    @property
    def operation(self) -> str:
        return "UNFREEZE"


@dataclasses.dataclass
class MoveFrozenPartsStep(Step[None]):
    """
    Renames files in the snapshot manifest to match what we will need during recover.

    The freeze step creates hardlinks of the table data in the `shadow/` folder, then the
    snapshot steps upload these file to backup storage and remember them by their
    hash.

    Later during the restore process, we need these files to be placed in the `store/`
    folder, with a slightly different hierarchy: we need the files in the correct place to be
    able to use the `ALTER TABLE ATTACH` command and re-add the data to the empty
    tables.

    By renaming files in the snapshot manifest, we can tell the restore step to put the
    files in a different place from where they were during the backup. This doesn't cause
    problem when actually downloading files from the backup storage because the storage
    only identifies files by their hash, it doesn't care about their original, or modified, path.
    """
    freeze_name: str

    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        # Note: we could also do that on restore, but this way we can erase the ClickHouse `FREEZE`
        # backup name from all the snapshot entries
        # I do not like mutating an existing result, we should making this more visible
        # by returning a mutated copy and use that in other steps
        snapshot_results: List[ipc.SnapshotResult] = context.get_result(SnapshotStep)
        escaped_freeze_name = escape_for_file_name(self.freeze_name)
        shadow_store_path = "shadow", escaped_freeze_name, "store"
        for snapshot_result in snapshot_results:
            for snapshot_file in snapshot_result.state.files:
                file_path_parts = snapshot_file.relative_path.parts
                # The original path starts with something like that :
                # shadow/astacus/store/123/12345678-1234-1234-1234-12345678abcd/all_1_1_0
                # where "astacus" is the freeze_name, the uuid is from the table (the folder before that is
                # the first 3 digits of the uuid), then "all_1_1_0" is the part name.
                # We transform it into :
                # store/123/12345678-1234-1234-1234-12345678abcd/detached/all_1_1_0
                # The "shadow/astacus" prefix is removed and the part folder is inside a "detached" folder.
                # The rest of the path, after the part folder, can contain anything and isn't modified.
                if file_path_parts[:3] == shadow_store_path and len(file_path_parts) >= 6:
                    # This is the uuid of the table containing that part
                    uuid_head, uuid_full, part_name, *rest = file_path_parts[3:]
                    part_path = Path(f"store/{uuid_head}/{uuid_full}/detached/{part_name}")
                    snapshot_file.relative_path = part_path.joinpath(*rest)


@dataclasses.dataclass
class DistributeReplicatedPartsStep(Step[None]):
    """
    Distribute replicated parts of table using the Replicated family of table engines.

    To avoid duplicating data during restoration, we must attach each replicated part
    to only on one server and let the replication do its work.

    This also serve as a performance and cost optimisation. Instead of fetching
    the same part from backup storage once for each server, we can fetch it only
    once for the entire cluster and then let the cluster exchange parts internally.

    This step must be run after `MoveFrozenPartsStep` to find the correct paths
    in the snapshot.
    """
    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        snapshot_results = context.get_result(SnapshotStep)
        snapshot_files = [snapshot_result.state.files for snapshot_result in snapshot_results]
        _, tables = context.get_result(RetrieveDatabasesAndTablesStep)
        table_uuids = {table.uuid for table in tables if table.is_replicated}
        parts, server_files = group_files_into_parts(snapshot_files, table_uuids)
        check_parts_replication(parts)
        distribute_parts_to_servers(parts, server_files)
        for files, snapshot_result in zip(server_files, snapshot_results):
            snapshot_result.state.files = files


@dataclasses.dataclass
class ClickHouseManifestStep(Step[ClickHouseManifest]):
    """
    Extracts the ClickHouse plugin manifest from the main backup manifest.
    """
    async def run_step(self, cluster: Cluster, context: StepsContext) -> ClickHouseManifest:
        backup_manifest = context.get_result(BackupManifestStep)
        return ClickHouseManifest.parse_obj(backup_manifest.plugin_data)


@dataclasses.dataclass
class RestoreReplicatedDatabasesStep(Step[None]):
    """
    Re-creates replicated databases on each client and re-create all tables in each database.

    After this step, all tables will be empty.
    """
    clients: List[ClickHouseClient]
    replicated_databases_zookeeper_path: str

    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        manifest = context.get_result(ClickHouseManifestStep)
        for database in manifest.replicated_databases:
            database_znode_name = escape_for_file_name(database.name)
            database_path = f"{self.replicated_databases_zookeeper_path}/{database_znode_name}"
            # The database must be dropped on *every* node before attempting to recreate it.
            # If we don't do that, then the recreated database on one node will recover data from
            # a node where the database wasn't recreated yet.
            for client in self.clients:
                await client.execute(f"DROP DATABASE IF EXISTS {escape_sql_identifier(database.name)} SYNC")
            for client in self.clients:
                await client.execute(
                    f"CREATE DATABASE {escape_sql_identifier(database.name)} "
                    f"ENGINE = Replicated({escape_sql_string(database_path)}, '{{shard}}', '{{replica}}')"
                )
        # If any known table depends on an unknown table that was inside a non-replicated
        # database engine, then this will crash. See comment in `RetrieveReplicatedDatabasesStep`.
        for table in tables_sorted_by_dependencies(manifest.tables):
            # Materialized views creates both a table for the view itself and a table
            # with the .inner_id. prefix to store the data, we don't need to recreate
            # them manually. We will need to restore their data parts however.
            if not table.name.startswith(".inner_id."):
                # Create on the first client and let replication do its thing
                await self.clients[0].execute(table.create_query)


@dataclasses.dataclass
class RestoreAccessEntitiesStep(Step[None]):
    """
    Restores access entities (user, roles, quotas, row_policies, settings profiles) and their grants
    to ZooKeeper. This requires using the replicated storage engine for users.

    The list of access entities to restore is read from the plugin manifest, which itself was
    filled by the `RetrieveAccessEntitiesStep` during a previous backup.

    Because of how the replicated storage engine works, recreating the entities in ZooKeeper
    is enough to have all ClickHouse servers notice the added znodes and create the entities:

    The replicated storage engine uses ZooKeeper as its main storage, each ClickHouse server
    only has an in-memory cache and uses ZooKeeper watches to detect added, modified or
    removed entities.
    """
    zookeeper_client: ZooKeeperClient
    access_entities_path: str

    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        clickhouse_manifest = context.get_result(ClickHouseManifestStep)
        async with self.zookeeper_client.connect() as connection:
            for access_entity in access_entities_sorted_by_dependencies(clickhouse_manifest.access_entities):
                escaped_entity_name = escape_for_file_name(access_entity.name)
                entity_name_path = f"{self.access_entities_path}/{access_entity.type}/{escaped_entity_name}"
                entity_path = f"{self.access_entities_path}/uuid/{access_entity.uuid}"
                attach_query_bytes = access_entity.attach_query.encode()
                # Nobody else should be touching ZooKeeper during the restore operation,
                # and we know that ClickHouse only reacts to creation of the node at `entity_path`.
                # Theses conditions make it safe to create this pair of nodes without a transaction.
                try:
                    await connection.create(entity_name_path, str(access_entity.uuid).encode())
                except NodeExistsError:
                    pass
                try:
                    await connection.create(entity_path, attach_query_bytes)
                except NodeExistsError:
                    pass


@dataclasses.dataclass
class AttachMergeTreePartsStep(Step[None]):
    """
    Restore data to all tables by using `ALTER TABLE ... ATTACH`.

    Which part are restored to which servers depends on whether the tables uses
    a Replicated table engine or not, see `DistributeReplicatedPartsStep` for more
    details.
    """
    clients: List[ClickHouseClient]

    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        backup_manifest = context.get_result(BackupManifestStep)
        clickhouse_manifest = context.get_result(ClickHouseManifestStep)
        tasks = []
        tables_by_uuid = {table.uuid: table for table in clickhouse_manifest.tables}
        for client, snapshot_result in zip(self.clients, backup_manifest.snapshot_results):
            parts_to_attach: Set[Tuple[str, str]] = set()
            for snapshot_file in snapshot_result.state.files:
                table_uuid = uuid.UUID(snapshot_file.relative_path.parts[2])
                table = tables_by_uuid.get(table_uuid)
                if table is not None:
                    part_name = unescape_from_file_name(snapshot_file.relative_path.parts[4])
                    parts_to_attach.add((table.escaped_sql_identifier, part_name))
            for table_identifier, part_name in sorted(parts_to_attach):
                tasks.append(client.execute(f"ALTER TABLE {table_identifier} ATTACH PART {escape_sql_string(part_name)}"))
        await asyncio.gather(*tasks)


@dataclasses.dataclass
class SyncReplicasStep(Step[None]):
    """
    Before declaring the restoration as finished, make sure all parts of replicated tables
    are all exchanged between all nodes.
    """
    clients: List[ClickHouseClient]
    sync_timeout: float

    async def run_step(self, cluster: Cluster, context: StepsContext) -> None:
        manifest = context.get_result(ClickHouseManifestStep)
        tasks = [
            client.execute(f"SYSTEM SYNC REPLICA {table.escaped_sql_identifier}", timeout=self.sync_timeout)
            for table in manifest.tables
            for client in self.clients
            if table.is_replicated
        ]
        await asyncio.gather(*tasks)