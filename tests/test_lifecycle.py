from __future__ import annotations

import pytest
from anki_rpc import AnkiConnectError

from anki_manager.errors import LifecycleError, NotReadyError
from anki_manager.lifecycle import Lifecycle
from conftest import cp, make_runner


UNIT = "kryshanti-anki.service"


class TestSystemctlOps:
    def test_start_calls_systemctl(self, fake_client):
        runner = make_runner({("start", UNIT): cp()})
        Lifecycle(UNIT, fake_client, runner=runner).start()
        assert runner.calls == [("start", UNIT)]

    def test_start_raises_on_nonzero(self, fake_client):
        runner = make_runner({("start", UNIT): cp(stderr="boom", returncode=5)})
        with pytest.raises(LifecycleError, match="boom"):
            Lifecycle(UNIT, fake_client, runner=runner).start()

    def test_stop_and_restart(self, fake_client):
        runner = make_runner({
            ("stop", UNIT): cp(),
            ("restart", UNIT): cp(),
        })
        lc = Lifecycle(UNIT, fake_client, runner=runner)
        lc.stop()
        lc.restart()
        assert runner.calls == [("stop", UNIT), ("restart", UNIT)]

    def test_is_active_true(self, fake_client):
        runner = make_runner({("is-active", UNIT): cp(stdout="active\n")})
        assert Lifecycle(UNIT, fake_client, runner=runner).is_active() is True

    def test_is_active_false(self, fake_client):
        runner = make_runner({("is-active", UNIT): cp(stdout="inactive\n", returncode=3)})
        assert Lifecycle(UNIT, fake_client, runner=runner).is_active() is False

    def test_sub_state(self, fake_client):
        runner = make_runner({
            ("show", UNIT, "--property=SubState", "--value"): cp(stdout="running\n"),
        })
        assert Lifecycle(UNIT, fake_client, runner=runner).sub_state() == "running"


class TestReadiness:
    def test_is_ready_true(self, fake_client):
        fake_client.deck_names.return_value = ["Default"]
        runner = make_runner({})
        assert Lifecycle(UNIT, fake_client, runner=runner).is_ready() is True

    def test_is_ready_false_on_anki_connect_error(self, fake_client):
        fake_client.deck_names.side_effect = AnkiConnectError("collection is not available")
        runner = make_runner({})
        assert Lifecycle(UNIT, fake_client, runner=runner).is_ready() is False

    def test_is_ready_false_on_connection_refused(self, fake_client):
        fake_client.deck_names.side_effect = OSError("Connection refused")
        runner = make_runner({})
        assert Lifecycle(UNIT, fake_client, runner=runner).is_ready() is False

    def test_wait_ready_returns_quickly_when_ready(self, fake_client):
        fake_client.deck_names.return_value = ["Default"]
        runner = make_runner({})
        Lifecycle(UNIT, fake_client, ready_timeout=2.0, runner=runner).wait_ready()

    def test_wait_ready_times_out(self, fake_client):
        fake_client.deck_names.side_effect = OSError("nope")
        runner = make_runner({
            ("show", UNIT, "--property=SubState", "--value"): cp(stdout="dead\n"),
        })
        with pytest.raises(NotReadyError, match="dead"):
            Lifecycle(UNIT, fake_client, ready_timeout=0.5, runner=runner).wait_ready()


class TestEnsureRunning:
    def test_skips_start_when_already_active(self, fake_client):
        fake_client.deck_names.return_value = ["Default"]
        runner = make_runner({("is-active", UNIT): cp(stdout="active\n")})
        Lifecycle(UNIT, fake_client, runner=runner).ensure_running()
        assert runner.calls == [("is-active", UNIT)]

    def test_starts_when_inactive(self, fake_client):
        fake_client.deck_names.return_value = ["Default"]
        runner = make_runner({
            ("is-active", UNIT): cp(stdout="inactive\n", returncode=3),
            ("start", UNIT): cp(),
        })
        Lifecycle(UNIT, fake_client, runner=runner).ensure_running()
        assert runner.calls == [("is-active", UNIT), ("start", UNIT)]


class TestStatus:
    def test_status_all_fields(self, fake_client):
        fake_client.deck_names.return_value = ["Default"]
        runner = make_runner({
            ("is-active", UNIT): cp(stdout="active\n"),
            ("show", UNIT, "--property=SubState", "--value"): cp(stdout="running\n"),
        })
        s = Lifecycle(UNIT, fake_client, runner=runner).status()
        assert s.active is True
        assert s.ready is True
        assert s.sub_state == "running"
