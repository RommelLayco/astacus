"""
Copyright (c) 2020 Aiven Ltd
See LICENSE for details
"""

from astacus.common.cassandra.config import CassandraClientConfiguration
from astacus.common.rohmustorage import RohmuConfig
from astacus.common.statsd import StatsdConfig
from astacus.common.utils import AstacusModel
from fastapi import Request
from pathlib import Path
from pydantic import DirectoryPath, Field, validator
from typing import List, Optional

APP_KEY = "node_config"


class NodeParallel(AstacusModel):
    # Optional parallelization of operations
    downloads: int = 1
    hashes: int = 1
    uploads: int = 1


class CassandraNodeConfig(AstacusModel):
    # Used in subop=get-schema-hash
    client: CassandraClientConfiguration

    # Nodetool is used to take snapshots in in cassandra subop=refresh-snapshot
    # (arguments passed as-is to subprocess.run)
    nodetool_command: List[str]

    # Cassandra start/stop are used in cassandra subop start-cassandra / stop-cassandra
    # (arguments passed as-is to subprocess.run)
    start_command: List[str]
    stop_command: List[str]

    @classmethod
    @validator("client")
    def ensure_config_path_specified(cls, v):
        assert v.config_path, "config_path must be specified for client in node section"
        return v


class NodeConfig(AstacusModel):
    # Where is the root of the file hierarchy we care about
    root: DirectoryPath

    # Which availability zone is this node in (optional)
    az: str = ""

    # Where do we hardlink things from the file hierarchy we care about
    # By default, .astacus subdirectory is created in root if this is not set
    # Directory is created if it does not exist
    root_link: Optional[Path]

    # These can be either globally or locally set
    object_storage: Optional[RohmuConfig] = None
    statsd: Optional[StatsdConfig] = None

    parallel: NodeParallel = Field(default_factory=NodeParallel)

    # Cassandra configuration is optional; for now, in node part of
    # the code, there are no plugins. (This may change later.)
    cassandra: Optional[CassandraNodeConfig]


def node_config(request: Request) -> NodeConfig:
    return getattr(request.app.state, APP_KEY)
