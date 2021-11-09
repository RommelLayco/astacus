from .clickhouse.plugin import ClickHousePlugin
from .files import FilesPlugin
from .flink.plugin import FlinkPlugin
from .m3db import M3DBPlugin
from astacus.common.ipc import Plugin

PLUGINS = {
    Plugin.clickhouse: ClickHousePlugin,
    Plugin.files: FilesPlugin,
    Plugin.m3db: M3DBPlugin,
    Plugin.flink: FlinkPlugin,
}
