"""Final reachable edges: the error-swallow invariants (best-effort paths must
never raise), remaining status/config/revelio branches, and daemon I/O edges.

After this file, what's left in tess.py genuinely requires something real:
- interactive MSAL auth + silent refresh against a live Entra tenant
- msal-extensions keychain persistence (real OS keychain)
- Windows-only branches (registry, taskkill, Win32 APIs) — the CI windows
  runner executes what's reachable there
- spawn_daemon's real detached subprocess (would escape the test sandbox and
  touch a real tess install)
- `logs -f` (infinite follow loop) and the refresh 10-second timeout path
"""
import logging
import os
import sys
import time
import types
from pathlib import Path

import pytest

import tess


class TestErrorSwallowInvariants:
    """Best-effort cleanup paths must swallow OS errors, never crash."""

    def test_clear_msal_cache_survives_unlink_failure(self, monkeypatch):
        class ExplodingPath:
            def unlink(self, missing_ok=False):
                raise OSError("device busy")
        monkeypatch.setattr(tess, "msal_cache_path", lambda: ExplodingPath())
        tess.clear_msal_cache()  # must not raise

    def test_save_plain_cache_survives_write_failure(self, dirs, monkeypatch):
        mod = types.ModuleType("msal")

        class SerializableTokenCache:
            has_state_changed = True

            def serialize(self):
                return "{}"
        mod.SerializableTokenCache = SerializableTokenCache
        monkeypatch.setitem(sys.modules, "msal", mod)

        def failing_write(path, content, mode=0o600):
            raise tess.TokenWriteError("disk full")
        monkeypatch.setattr(tess, "atomic_write", failing_write)
        tess.save_plain_cache_if_needed(SerializableTokenCache())  # no raise

    def test_ensure_private_dir_survives_chmod_failure(self, dirs, monkeypatch):
        monkeypatch.setattr(Path, "chmod",
                            lambda self, mode: (_ for _ in ()).throw(
                                OSError("read-only fs")))
        d = tess.ensure_private_dir(dirs / "unchmoddable")
        assert d.is_dir()  # created despite chmod failing

    def test_cleanup_survives_unlink_failure(self, dirs, monkeypatch):
        tess.atomic_write(tess.token_path(), "x")
        monkeypatch.setattr(Path, "unlink",
                            lambda self, missing_ok=False: (_ for _ in ()).throw(
                                OSError("busy")))
        tess.cleanup_session_files()  # must not raise

    def test_setup_daemon_logging_survives_chmod_failure(self, dirs,
                                                         monkeypatch):
        monkeypatch.setattr(Path, "chmod",
                            lambda self, mode: (_ for _ in ()).throw(
                                OSError("nope")))
        logger = tess.setup_daemon_logging()
        assert logger is not None
        logging.getLogger("tess").handlers.clear()

    def test_atomic_write_replace_failure_cleans_temp(self, dirs, monkeypatch):
        target = dirs / "data" / "token"

        def failing_replace(src, dst):
            raise OSError("cross-device link")
        monkeypatch.setattr(tess.os, "replace", failing_replace)
        with pytest.raises(tess.TokenWriteError):
            tess.atomic_write(target, "x")
        assert not target.with_suffix(".tmp").exists()  # temp reaped


class TestStatusRemainingBranches:
    def test_stale_session_json(self, dirs, capsys):
        import json
        tess.atomic_write(tess.pid_path(), str(2 ** 22 + 12345), mode=0o644)
        args = types.SimpleNamespace(json=True, verbose=False)
        assert tess.cmd_status(args) == 0
        assert json.loads(capsys.readouterr().out) == \
            {"active": False, "reason": "stale"}

    def test_broken_session_json(self, dirs, capsys):
        import json
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        args = types.SimpleNamespace(json=True, verbose=False)
        assert tess.cmd_status(args) == 1
        assert json.loads(capsys.readouterr().out)["reason"] == "no-token"

    def test_token_without_exp_shows_question_mark(self, dirs, make_jwt,
                                                   capsys):
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        tess.atomic_write(tess.session_started_path(), str(time.time()))
        tess.atomic_write(tess.token_path(), make_jwt({"upn": "x@y.z"}))
        args = types.SimpleNamespace(json=False, verbose=False)
        tess.cmd_status(args)
        out = capsys.readouterr().out
        assert "token expires ?" in out
        assert "expired/stale" in out


class TestConfigRemainingBranches:
    def test_running_session_changes_label(self, dirs, write_config, capsys,
                                           monkeypatch):
        write_config()
        monkeypatch.chdir(dirs)
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        tess.cmd_config(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert f"Session: running (pid {os.getpid()})" in out
        assert "Config in use" in out

    def test_invalid_config_content_reported(self, dirs, capsys, monkeypatch):
        bad = dirs / "config" / tess.CONFIG_FILENAME
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text('{"tenant_id": "only-this"}')
        monkeypatch.chdir(dirs)
        tess.cmd_config(types.SimpleNamespace())
        assert "Valid:       NO" in capsys.readouterr().out

    def test_drift_when_ladder_resolves_nothing(self, dirs, write_config,
                                                capsys, monkeypatch):
        recorded = write_config(path=dirs / "recorded.json")
        sc = tess.session_config_path()
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text(str(recorded))
        monkeypatch.chdir(dirs)  # ladder finds nothing now
        tess.cmd_config(types.SimpleNamespace())
        out = capsys.readouterr().out
        assert "DRIFT" in out
        assert "none resolvable" in out


class TestRevelioRemainingBranches:
    @pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                        reason="permission bits don't block win32 or root")
    def test_unreadable_token_file(self, dirs, capsys):
        tess.atomic_write(tess.token_path(), "secret")
        tess.token_path().chmod(0)  # exists, but read_text -> PermissionError
        assert tess.cmd_revelio() == 1
        assert "cannot read token file" in capsys.readouterr().out

    def test_no_config_still_reports_claims(self, dirs, make_jwt, capsys,
                                            monkeypatch):
        monkeypatch.chdir(dirs)
        tess.atomic_write(tess.token_path(), make_jwt(
            {"upn": "dev@example.com", "exp": int(time.time()) + 60}))
        assert tess.cmd_revelio() == 0
        out = capsys.readouterr().out
        assert "config unavailable" in out
        assert "token not expired" in out

    def test_token_without_exp_fails_expiry_check(self, dirs, write_config,
                                                  make_jwt, capsys,
                                                  monkeypatch):
        write_config()
        monkeypatch.chdir(dirs)
        tess.atomic_write(tess.token_path(), make_jwt({"upn": "x@y.z"}))
        tess.cmd_revelio()
        assert "no exp claim" in capsys.readouterr().out


class TestRefreshRemainingBranches:
    def test_refresh_with_no_prior_token_and_no_exp(self, dirs, make_jwt,
                                                    monkeypatch, capsys):
        # No token file before, and the new token has no exp claim -> the
        # plain "Token refreshed." message (no expiration shown).
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        new_token = make_jwt({"upn": "x@y.z"})

        def fake_daemon_sleep(_s):
            if tess.refresh_signal_path().exists():
                tess.refresh_signal_path().unlink()
                tess.atomic_write(tess.token_path(), new_token)
        monkeypatch.setattr(time, "sleep", fake_daemon_sleep)
        assert tess.cmd_refresh(types.SimpleNamespace()) == 0
        out = capsys.readouterr().out
        assert "Token refreshed." in out
        assert "expiration" not in out


class TestDaemonRemainingEdges:
    @pytest.fixture(autouse=True)
    def _clean_logger(self):
        yield
        logging.getLogger("tess").handlers.clear()

    @pytest.fixture
    def fast_daemon(self, dirs, monkeypatch):
        mod = types.ModuleType("msal")
        mod.SerializableTokenCache = type("STC", (), {})
        monkeypatch.setitem(sys.modules, "msal", mod)
        monkeypatch.setattr(tess, "notify", lambda t, m: None)
        monkeypatch.setattr(tess, "REFRESH_SIGNAL_CHECK_SECONDS", 0.01)
        monkeypatch.setattr(tess, "FAILURE_BACKOFF_SECONDS", 0.03)
        monkeypatch.setattr(tess, "FAILURE_GIVE_UP_AFTER_SECONDS", 0.1)
        return monkeypatch

    def test_empty_msal_cache_is_an_auth_failure(self, fast_daemon, dirs,
                                                 write_config, make_jwt):
        app = types.SimpleNamespace(get_accounts=lambda: [],
                                    acquire_token_silent=lambda **kw: None)
        fast_daemon.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (app, object()))
        cfg = write_config()
        tess.atomic_write(tess.session_started_path(), str(time.time()))
        tess.refresh_signal_path().touch()
        tess.cmd_refresh_daemon(str(cfg))
        assert "no MSAL account in cache" in tess.log_path().read_text()

    def test_refresh_with_missing_token_file_still_writes(
            self, fast_daemon, dirs, write_config, make_jwt):
        # prev_exp read fails (no token yet); refresh must still succeed.
        token = make_jwt({"exp": int(time.time()) + 7200})

        class App:
            def get_accounts(self):
                return [{"username": "dev"}]

            def acquire_token_silent(self, scopes, account,
                                     force_refresh=False):
                return {"id_token": token}
        fast_daemon.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (App(), object()))
        cfg = write_config(session_max_hours=0.0001)
        tess.atomic_write(tess.session_started_path(), str(time.time()))
        tess.refresh_signal_path().touch()   # no token file exists
        tess.cmd_refresh_daemon(str(cfg))
        assert "token refreshed" in tess.log_path().read_text()

    def test_token_write_failure_logged_and_retried(
            self, fast_daemon, dirs, write_config, make_jwt):
        token = make_jwt({"exp": int(time.time()) + 7200})

        class App:
            def get_accounts(self):
                return [{"username": "dev"}]

            def acquire_token_silent(self, scopes, account,
                                     force_refresh=False):
                return {"id_token": token}
        fast_daemon.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (App(), object()))
        real_write = tess.atomic_write

        def flaky(path, content, mode=0o600):
            if path == tess.token_path():
                raise tess.TokenWriteError("disk full")
            real_write(path, content, mode)
        fast_daemon.setattr(tess, "atomic_write", flaky)
        cfg = write_config(session_max_hours=0.0001)
        real_write(tess.session_started_path(), str(time.time()))
        tess.refresh_signal_path().touch()
        tess.cmd_refresh_daemon(str(cfg))
        assert "token write failed (will retry)" in tess.log_path().read_text()

    def test_daemon_resolves_config_when_started_without_one(
            self, fast_daemon, dirs, write_config):
        # No config argument -> falls back to the recorded/ladder resolution;
        # nothing resolvable -> clean error exit.
        fast_daemon.chdir(dirs)
        assert tess.cmd_refresh_daemon(None) == 1
        assert "could not load config" in tess.log_path().read_text()


class TestMainBannerDispatch:
    def test_banner_argv_dispatch(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["tess", "_banner"])
        with pytest.raises(SystemExit) as exc:
            tess.main()
        assert exc.value.code == 0
        assert tess.TAGLINE in capsys.readouterr().out


class TestConfigDirDarwin:
    def test_config_dir_macos(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert tess.config_dir() == Path.home() / ".config" / "tess"
