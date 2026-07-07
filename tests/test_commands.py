"""Command-level tests: stop, logs, version, config, revelio, refresh, the
start guard paths, and the daemon's teardown edges.

The MSAL interactive sign-in and the happy-path daemon loop are deliberately
NOT tested here — they require a live Entra tenant. Everything around them is.
"""
import logging
import os
import subprocess
import sys
import time

import pytest

import tess


class Args:
    """Minimal argparse.Namespace stand-in."""
    def __init__(self, **kw):
        self.config = None
        self.force = False
        self.quiet = False
        self.json = False
        self.verbose = False
        self.lines = 50
        self.follow = False
        self.__dict__.update(kw)


@pytest.fixture(autouse=True)
def _clean_daemon_logger():
    """setup_daemon_logging attaches handlers to a global logger; detach them
    after each test so handlers never point into a torn-down tmp dir."""
    yield
    logging.getLogger("tess").handlers.clear()


# ---------- stop ----------

class TestCmdStop:
    def test_no_session(self, dirs, capsys):
        assert tess.cmd_stop(Args()) == 0
        assert "No active session" in capsys.readouterr().out

    def test_kills_daemon_and_cleans_up(self, dirs, capsys):
        # A real child process stands in for the daemon.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            tess.atomic_write(tess.pid_path(), str(proc.pid), mode=0o644)
            tess.atomic_write(tess.token_path(), "live-token")
            assert tess.cmd_stop(Args()) == 0
            assert "Session ended" in capsys.readouterr().out
            proc.wait(timeout=5)                      # actually killed
            assert not tess.pid_path().exists()       # state cleaned
            assert not tess.token_path().exists()     # token not left behind
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_stop_is_recorded_in_audit_log(self, dirs, capsys):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            tess.atomic_write(tess.pid_path(), str(proc.pid), mode=0o644)
            tess.cmd_stop(Args())
            assert "session stopped" in tess.log_path().read_text()
        finally:
            if proc.poll() is None:
                proc.kill()


# ---------- logs ----------

class TestCmdLogs:
    def test_no_log_file_yet(self, dirs, capsys):
        assert tess.cmd_logs(Args()) == 0
        assert "No log file yet" in capsys.readouterr().out

    def test_tails_last_n_lines(self, dirs, capsys):
        tess.log_path().parent.mkdir(parents=True, exist_ok=True)
        tess.log_path().write_text(
            "".join(f"line-{i}\n" for i in range(100)))
        assert tess.cmd_logs(Args(lines=10)) == 0
        out_lines = capsys.readouterr().out.splitlines()
        assert out_lines == [f"line-{i}" for i in range(90, 100)]


# ---------- version ----------

class TestCmdVersion:
    def test_reports_version_and_config(self, dirs, write_config, capsys,
                                        monkeypatch):
        write_config()
        monkeypatch.chdir(dirs)
        assert tess.cmd_version(Args()) == 0
        out = capsys.readouterr().out
        assert f"tess {tess.VERSION}" in out
        assert "arn:aws:iam::123456789012:role/dev-role" in out

    def test_survives_missing_config(self, dirs, capsys, monkeypatch):
        monkeypatch.chdir(dirs)
        assert tess.cmd_version(Args()) == 0
        assert "not loaded" in capsys.readouterr().out


# ---------- config ----------

class TestCmdConfig:
    def test_no_session_no_config(self, dirs, capsys, monkeypatch):
        monkeypatch.chdir(dirs)
        assert tess.cmd_config(Args()) == 0
        out = capsys.readouterr().out
        assert "Session: not running" in out
        assert "none resolvable" in out

    def test_valid_config_reported(self, dirs, write_config, capsys,
                                   monkeypatch):
        write_config()
        monkeypatch.chdir(dirs)
        tess.cmd_config(Args())
        out = capsys.readouterr().out
        assert "Valid:       yes" in out
        assert "AWS_ROLE_ARN" in out  # managed env vars listed

    def test_drift_between_recorded_and_ladder_is_flagged(
            self, dirs, write_config, capsys, monkeypatch):
        write_config()                                    # ladder answer
        recorded = write_config(path=dirs / "recorded.json")
        sc = tess.session_config_path()
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text(str(recorded))
        monkeypatch.chdir(dirs)
        tess.cmd_config(Args())
        assert "DRIFT" in capsys.readouterr().out


# ---------- revelio ----------

class TestCmdRevelio:
    def test_no_token_file(self, dirs, capsys):
        assert tess.cmd_revelio() == 1
        assert "no token file" in capsys.readouterr().out

    def test_undecodable_token(self, dirs, capsys):
        tess.atomic_write(tess.token_path(), "not-a-jwt")
        assert tess.cmd_revelio() == 1
        assert "not a decodable JWT" in capsys.readouterr().out

    def test_never_prints_the_raw_token(self, dirs, write_config, make_jwt,
                                        capsys, monkeypatch, valid_config):
        write_config()
        monkeypatch.chdir(dirs)
        token = make_jwt({
            "preferred_username": "dev@example.com",
            "aud": valid_config["client_id"],
            "iss": f"https://login.microsoftonline.com/"
                   f"{valid_config['tenant_id']}/v2.0",
            "exp": int(time.time()) + 3000,
        })
        tess.atomic_write(tess.token_path(), token)
        assert tess.cmd_revelio() == 0
        out = capsys.readouterr().out
        assert token not in out                # THE invariant: claims only
        assert "dev@example.com" in out        # ...but claims are shown
        assert "aud == client_id" in out

    def test_sanity_checks_pass_for_matching_config(
            self, dirs, write_config, make_jwt, capsys, monkeypatch,
            valid_config):
        write_config()
        monkeypatch.chdir(dirs)
        token = make_jwt({
            "aud": valid_config["client_id"],
            "iss": f"https://login.microsoftonline.com/"
                   f"{valid_config['tenant_id']}/v2.0",
            "exp": int(time.time()) + 3000,
        })
        tess.atomic_write(tess.token_path(), token)
        tess.cmd_revelio()
        out = capsys.readouterr().out
        for check in ("aud == client_id", "issuer carries tenant_id",
                      "token not expired"):
            assert f"[✓] {check}" in out

    def test_mismatched_aud_and_expired_are_flagged(
            self, dirs, write_config, make_jwt, capsys, monkeypatch):
        write_config()
        monkeypatch.chdir(dirs)
        token = make_jwt({"aud": "some-other-app",
                          "iss": "https://evil.example/",
                          "exp": int(time.time()) - 60})
        tess.atomic_write(tess.token_path(), token)
        tess.cmd_revelio()
        out = capsys.readouterr().out
        for check in ("aud == client_id", "issuer carries tenant_id",
                      "token not expired"):
            assert f"[✗] {check}" in out


# ---------- refresh ----------

class TestCmdRefresh:
    def test_no_session_is_an_error(self, dirs, capsys):
        assert tess.cmd_refresh(Args()) == 1
        assert "No active session" in capsys.readouterr().err

    def test_signals_daemon_and_reports_new_expiry(
            self, dirs, make_jwt, monkeypatch, capsys):
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        tess.atomic_write(tess.token_path(), make_jwt({"exp": 1}))
        new_token = make_jwt({"exp": int(time.time()) + 3600})

        def fake_daemon_sleep(_seconds):
            # Stand in for the daemon: when the refresh signal appears,
            # consume it and deliver a fresh token.
            if tess.refresh_signal_path().exists():
                tess.refresh_signal_path().unlink()
                tess.atomic_write(tess.token_path(), new_token)
        monkeypatch.setattr(time, "sleep", fake_daemon_sleep)

        assert tess.cmd_refresh(Args()) == 0
        out = capsys.readouterr().out
        assert "Token refreshed" in out
        assert tess.token_path().read_text() == new_token
        assert tess.refresh_signal_path().exists() is False


# ---------- start (guard paths only — interactive sign-in needs Entra) ----------

class TestCmdStartGuards:
    def test_already_active_session_short_circuits(
            self, dirs, write_config, capsys, monkeypatch):
        write_config()
        monkeypatch.chdir(dirs)
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        assert tess.cmd_start(Args()) == 0
        assert "already active" in capsys.readouterr().out

    def test_unresolvable_config_fails_before_auth(self, dirs, monkeypatch):
        monkeypatch.chdir(dirs)
        with pytest.raises(tess.ConfigError):
            tess.cmd_start(Args())


# ---------- the daemon's teardown edges (no MSAL needed) ----------

class TestDaemonTeardown:
    def test_unloadable_config_cleans_up_and_exits(self, dirs):
        tess.atomic_write(tess.token_path(), "leftover")
        rc = tess.cmd_refresh_daemon(str(dirs / "missing.json"))
        assert rc == 1
        assert not tess.token_path().exists()

    def test_missing_session_start_marker_exits_clean(
            self, dirs, write_config, monkeypatch):
        cfg = write_config()
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (None, None))
        tess.atomic_write(tess.token_path(), "leftover")
        # No session.started file -> daemon must clean up and exit, never loop.
        tess.cmd_refresh_daemon(str(cfg))
        assert not tess.token_path().exists()

    def test_session_cap_ends_the_session(self, dirs, write_config,
                                          monkeypatch):
        # A session started 10s ago against a ~0.4s cap: the daemon's first
        # loop iteration must tear the session down and exit.
        cfg = write_config(session_max_hours=0.0001)
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (None, None))
        notifications = []
        monkeypatch.setattr(tess, "notify",
                            lambda title, msg: notifications.append(title))
        tess.atomic_write(tess.session_started_path(), str(time.time() - 10))
        tess.atomic_write(tess.token_path(), "expiring-token")
        tess.cmd_refresh_daemon(str(cfg))
        assert not tess.token_path().exists()
        assert not tess.session_started_path().exists()
        assert notifications == ["Session ended"]
