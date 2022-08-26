"""
Copyright (c) 2021 Aiven Ltd
See LICENSE for details
"""

from astacus.common import ipc
from astacus.common.cassandra.config import SNAPSHOT_NAME
from astacus.node.config import CassandraNodeConfig
from tests.unit.conftest import CassandraTestConfig

import pytest
import subprocess


class CassandraTestEnv(CassandraTestConfig):
    cassandra_node_config: CassandraNodeConfig

    def __init__(self, *, app, client, mocker, tmpdir):
        super().__init__(mocker=mocker, tmpdir=tmpdir)
        self.app = app
        self.client = client

    def lock(self):
        response = self.client.post("/node/lock?locker=x&ttl=10")
        assert response.status_code == 200, response.json()

    def post(self, *, subop, **kwargs):
        url = f"/node/cassandra/{subop}"
        return self.client.post(url, **kwargs)

    def get_status(self, response):
        assert response.status_code == 200, response.json()
        status_url = response.json()["status_url"]
        return self.client.get(status_url)

    def setup_cassandra_node_config(self):
        self.cassandra_node_config = CassandraNodeConfig(
            client=self.cassandra_client_config,
            nodetool_command=["nodetool"],
            start_command=["dummy-start"],
            stop_command=["dummy-stop"],
        )
        self.app.state.node_config.cassandra = self.cassandra_node_config


@pytest.fixture(name="ctenv")
def fixture_ctenv(app, client, mocker, tmpdir):
    return CassandraTestEnv(app=app, client=client, mocker=mocker, tmpdir=tmpdir)


@pytest.mark.parametrize("subop", ipc.CassandraSubOp)
def test_api_cassandra_subop(app, ctenv, mocker, subop):
    req_json = {"tokens": ["42", "7"]}

    # Without lock, we shouldn't be able to do use the endpoint
    response = ctenv.post(subop=subop, json=req_json)
    if response.status_code == 501:
        # If Cassandra module is missing, endpoint will return 501
        return

    assert response.status_code == 409, response.json()

    ctenv.lock()

    # No Cassandra configuration in node_config yet -> should fail
    with pytest.raises(AssertionError):
        response = ctenv.post(subop=subop, json=req_json)

    ctenv.setup_cassandra_node_config()
    subprocess_run = mocker.patch.object(subprocess, "run")

    # Now the app is actually correctly configured

    if subop == ipc.CassandraSubOp.get_schema_hash:
        # This is tested separately
        return

    response = ctenv.post(subop=subop, json=req_json)

    # Ensure that besides the measurable internal effect, the API also
    # returns 200 for the status-url + the progress has completed.
    status = ctenv.get_status(response)
    assert status.status_code == 200, response.json()

    progress = status.json()["progress"]
    assert not progress["failed"]
    assert progress["final"]

    if subop == ipc.CassandraSubOp.remove_snapshot:
        assert not ctenv.snapshot_path.exists()
        assert ctenv.other_snapshot_path.exists()
    elif subop == ipc.CassandraSubOp.restore_snapshot:
        # The file should be moved from dummytable-123 snapshot dir to
        # dummytable-234
        assert (ctenv.root / "data" / "dummyks" / "dummytable-234" / "asdf").read_text() == "foobar"
        assert progress["handled"]
    elif subop == ipc.CassandraSubOp.start_cassandra:
        subprocess_run.assert_any_call(ctenv.cassandra_node_config.start_command + ["tempfilename"], check=True)

        assert (
            ctenv.fake_conffile.getvalue()
            == """auto_bootstrap: false
initial_token: 42, 7
listen_address: 127.0.0.1
num_tokens: 2
"""
        )
    elif subop == ipc.CassandraSubOp.stop_cassandra:
        subprocess_run.assert_any_call(ctenv.cassandra_node_config.stop_command, check=True)
    elif subop == ipc.CassandraSubOp.take_snapshot:
        subprocess_run.assert_any_call(
            ctenv.cassandra_node_config.nodetool_command + ["snapshot", "-t", SNAPSHOT_NAME], check=True
        )
    else:
        raise NotImplementedError(subop)


@pytest.mark.parametrize("fail", [True])
def test_api_cassandra_get_schema_hash(ctenv, fail, mocker):
    # The state of API *before* these two setup steps are done is checked in the test_api_cassandra_subop
    ctenv.lock()
    ctenv.setup_cassandra_node_config()

    node_cassandra = pytest.importorskip("astacus.node.cassandra")

    if not fail:
        mocker.patch.object(node_cassandra.CassandraOp, "_get_schema_hash", return_value="mockhash")

    response = ctenv.post(subop=ipc.CassandraSubOp.get_schema_hash, json={})
    status = ctenv.get_status(response)
    assert status.status_code == 200, response.json()

    progress = status.json()["progress"]
    assert not progress["failed"]
    assert progress["final"]

    schema_hash = status.json()["schema_hash"]
    if fail:
        assert not schema_hash
    else:
        assert schema_hash == "mockhash"