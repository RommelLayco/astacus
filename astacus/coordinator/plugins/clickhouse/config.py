"""
Copyright (c) 2021 Aiven Ltd
See LICENSE for details
"""
from .client import ClickHouseClient, HttpClickHouseClient
from .zookeeper import KazooZooKeeperClient, ZooKeeperClient
from astacus.common.utils import AstacusModel, build_netloc
from typing import List, Optional


class ZooKeeperNode(AstacusModel):
    host: str
    port: int


class ZooKeeperConfiguration(AstacusModel):
    nodes: List[ZooKeeperNode] = []


class ClickHouseNode(AstacusModel):
    host: str
    port: int


class ClickHouseConfiguration(AstacusModel):
    username: Optional[str] = None
    password: Optional[str] = None
    nodes: List[ClickHouseNode] = []


class ReplicatedDatabaseSettings(AstacusModel):
    max_broken_tables_ratio: Optional[float]
    max_replication_lag_to_enqueue: Optional[int]
    wait_entry_commited_timeout_sec: Optional[int]
    cluster_username: Optional[str]
    cluster_password: Optional[str]
    cluster_secret: Optional[str]


def get_zookeeper_client(configuration: ZooKeeperConfiguration) -> ZooKeeperClient:
    return KazooZooKeeperClient(hosts=[build_netloc(node.host, node.port) for node in configuration.nodes])


def get_clickhouse_clients(configuration: ClickHouseConfiguration) -> List[ClickHouseClient]:
    return [
        HttpClickHouseClient(
            host=node.host,
            port=node.port,
            username=configuration.username,
            password=configuration.password,
        ) for node in configuration.nodes
    ]
