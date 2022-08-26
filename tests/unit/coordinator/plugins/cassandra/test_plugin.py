"""
Copyright (c) 2022 Aiven Ltd
See LICENSE for details
"""

from astacus.common import ipc
from astacus.coordinator.plugins.base import StepFailedError
from tests.unit.node.test_node_cassandra import CassandraTestConfig
from types import SimpleNamespace

import pytest


@pytest.fixture(name="cplugin")
def fixture_cplugin(mocker, tmpdir):
    plugin = pytest.importorskip("astacus.coordinator.plugins.cassandra.plugin")
    ctc = CassandraTestConfig(mocker=mocker, tmpdir=tmpdir)
    yield plugin.CassandraPlugin(client=ctc.cassandra_client_config)


async def test_step_cassandrasubop(mocker):
    plugin = pytest.importorskip("astacus.coordinator.plugins.cassandra.plugin")
    mocker.patch.object(plugin, "run_subop")

    step = plugin.CassandraSubOpStep(op=ipc.CassandraSubOp.stop_cassandra)
    cluster = None
    context = None
    result = await step.run_step(cluster, context)
    assert result is None


@pytest.mark.parametrize("success", [False, True])
async def test_step_cassandra_validate_configuration(mocker, success):
    plugin = pytest.importorskip("astacus.coordinator.plugins.cassandra.plugin")
    step = plugin.ValidateConfigurationStep(nodes=[])
    context = None
    if success:
        cluster = SimpleNamespace(nodes=[])
        result = await step.run_step(cluster, context)
        assert result is None
    else:
        # node count mismatch
        cluster = SimpleNamespace(nodes=[42])
        with pytest.raises(StepFailedError):
            await step.run_step(cluster, context)


def test_get_backup_steps(mocker, cplugin):
    context = mocker.Mock()
    cplugin.get_backup_steps(context=context)


def test_get_restore_steps(mocker, cplugin):
    context = mocker.Mock()
    cplugin.get_restore_steps(context=context, req=ipc.RestoreRequest())