"""Copyright (c) 2021 Aiven Ltd
See LICENSE for details

Client configuration data model

Note that resides in its own file mostly because it is imported in few
places in Astacus; the other modules in the directory have
dependencies on the actual cassandra driver, but this one does not.


NOTE: While caching e.g. self.get_config result would be tempting (and
even moreso get_port and get_hostnames), sadly pydantic and functools
do not live together too well: see
https://github.com/samuelcolvin/pydantic/issues/3376


"""

from astacus.common.utils import AstacusModel
from pydantic import FilePath, root_validator
from typing import List, Optional

import yaml

SNAPSHOT_NAME = "astacus-backup"


class CassandraClientConfiguration(AstacusModel):
    config_path: Optional[FilePath]

    # WhiteListRoundRobinPolicy contact points
    hostnames: Optional[List[str]]

    port: Optional[int]

    # PlainTextAuthProvider
    username: str
    password: str

    # If set, configure ssl access configuration which requires the ca cert
    ca_cert_path: Optional[str]

    @classmethod
    @root_validator
    def config_or_hostname_port_provided(cls, values: dict) -> dict:
        assert "config_path" in values or "port" in values, "Either config_path, or port must be provided"
        return values

    def get_port(self) -> int:
        if self.port:
            return self.port
        return int(self.get_config()["native_transport_port"])

    def get_hostnames(self) -> List[str]:
        if self.hostnames:
            return self.hostnames
        return ["127.0.0.1"]

    def get_listen_address(self) -> str:
        return self.get_config()["listen_address"]

    def get_config(self) -> dict:
        assert self.config_path
        with self.config_path.open() as f:
            return yaml.safe_load(f)
