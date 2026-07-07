"""Session state: PID handling, liveness, cleanup, and `tess status`.

Uses the test runner's own PID as the "alive daemon" and an absurdly high PID
as the dead one, so no processes are spawned or killed.
"""
import json
import subprocess
import sys
import os
import time

import pytest

import tess

DEAD_PID = 2 ** 22 + 12345  # far above any real PID on the test machine


class TestPidAndLiveness:
    def test_read_pid_roundtrip(self, dirs):
        tess.atomic_write(tess.pid_path(), "12345", mode=0o644)
        assert tess.read_pid() == 12345

    def test_read_pid_missing_or_garbage_is_none(self, dirs):
        assert tess.read_pid() is None
        tess.atomic_write(tess.pid_path(), "not-a-pid", mode=0o644)
        assert tess.read_pid() is None

    def test_own_process_is_alive(self):
        assert tess.is_process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_dead(self):
        assert tess.is_process_alive(DEAD_PID) is False

    def test_nonpositive_pids_are_dead(self):
        assert tess.is_process_alive(0) is False
        assert tess.is_process_alive(-1) is False

    def test_session_active_requires_live_pid(self, dirs):
        assert tess.is_session_active() is False          # no pid file
        tess.atomic_write(tess.pid_path(), str(DEAD_PID), mode=0o644)
        assert tess.is_session_active() is False          # dead pid
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        assert tess.is_session_active() is True           # alive pid


class TestKillProcess:
    @pytest.mark.skipif(sys.platform == "win32",
                        reason="SIGTERM/SIGKILL escalation is POSIX-only")
    def test_sigterm_ignorer_gets_sigkilled(self):
        # A daemon stuck ignoring SIGTERM must still die (SIGKILL fallback).
        proc = subprocess.Popen([
            sys.executable, "-c",
            "import signal, time; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
        try:
            time.sleep(0.3)  # let the child install its handler
            tess.kill_process(proc.pid)
            proc.wait(timeout=5)
            assert proc.poll() is not None
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_dead_pid_is_a_noop(self):
        tess.kill_process(DEAD_PID)  # must not raise


class TestSessionAge:
    def test_no_session_is_none(self, dirs):
        assert tess.session_age_seconds() is None

    def test_age_measured_from_started_file(self, dirs):
        tess.atomic_write(tess.session_started_path(), str(time.time() - 120))
        age = tess.session_age_seconds()
        assert 119 < age < 125


class TestCleanup:
    def test_removes_all_session_files_and_is_idempotent(self, dirs):
        for p in (tess.token_path(), tess.pid_path(),
                  tess.session_started_path(), tess.session_config_path(),
                  tess.refresh_signal_path()):
            tess.atomic_write(p, "x")
        tess.cleanup_session_files()
        for p in (tess.token_path(), tess.pid_path(),
                  tess.session_started_path(), tess.session_config_path(),
                  tess.refresh_signal_path()):
            assert not p.exists()
        tess.cleanup_session_files()  # second run must not raise


def _status_args(json_out=False, verbose=False):
    class Args:
        pass
    a = Args()
    a.json = json_out
    a.verbose = verbose
    return a


class TestCmdStatus:
    def test_no_session_plain(self, dirs, capsys):
        assert tess.cmd_status(_status_args()) == 0
        assert "No active session" in capsys.readouterr().out

    def test_no_session_json(self, dirs, capsys):
        assert tess.cmd_status(_status_args(json_out=True)) == 0
        assert json.loads(capsys.readouterr().out) == {"active": False}

    def test_stale_pid_is_cleaned_up(self, dirs, capsys):
        tess.atomic_write(tess.pid_path(), str(DEAD_PID), mode=0o644)
        tess.atomic_write(tess.token_path(), "leftover-token")
        assert tess.cmd_status(_status_args()) == 0
        out = capsys.readouterr().out
        assert "stale" in out
        assert not tess.token_path().exists()   # token wiped, not left behind
        assert not tess.pid_path().exists()

    def test_active_healthy_session_json(self, dirs, make_jwt, capsys):
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        tess.atomic_write(tess.session_started_path(), str(time.time()))
        token = make_jwt({
            "preferred_username": "dev@example.com",
            "exp": int(time.time()) + 3000,
        })
        tess.atomic_write(tess.token_path(), token)
        assert tess.cmd_status(_status_args(json_out=True)) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["active"] is True
        assert data["healthy"] is True
        assert data["upn"] == "dev@example.com"
        assert data["pid"] == os.getpid()

    def test_expired_token_reports_unhealthy(self, dirs, make_jwt, capsys):
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        tess.atomic_write(tess.session_started_path(), str(time.time()))
        token = make_jwt({"upn": "dev@example.com",
                          "exp": int(time.time()) - 60})
        tess.atomic_write(tess.token_path(), token)
        tess.cmd_status(_status_args(json_out=True))
        assert json.loads(capsys.readouterr().out)["healthy"] is False

    def test_pid_alive_but_token_missing_is_broken(self, dirs, capsys):
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        assert tess.cmd_status(_status_args()) == 1
        assert "broken" in capsys.readouterr().out

    def test_hostile_upn_is_sanitized_in_output(self, dirs, make_jwt, capsys):
        # A claim with terminal escapes must reach the terminal stripped.
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        tess.atomic_write(tess.session_started_path(), str(time.time()))
        token = make_jwt({"preferred_username": "evil\x1b[2Juser",
                          "exp": int(time.time()) + 3000})
        tess.atomic_write(tess.token_path(), token)
        tess.cmd_status(_status_args())
        assert "\x1b" not in capsys.readouterr().out
