"""The refresh daemon's loop, with MSAL stubbed at its interface.

This is the code that runs unattended for a full workday on every laptop, so
its failure handling is tested hardest: backoff, escalation, give-up, the
stale-token self-check, and the signal-driven early refresh. Timing constants
are shrunk via monkeypatch so each scenario completes in well under a second.
"""
import logging
import sys
import time
import types

import pytest

import tess


class FakeSilentApp:
    """Stands in for msal.PublicClientApplication in the daemon.

    `results` is a queue of dicts returned by acquire_token_silent; the last
    entry repeats forever. An empty queue means "always fail" (returns None).
    """
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = 0

    def get_accounts(self):
        return [{"username": "dev@example.com"}]

    def acquire_token_silent(self, scopes, account, force_refresh=False):
        self.calls += 1
        if not self.results:
            return None
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


@pytest.fixture(autouse=True)
def _clean_daemon_logger():
    yield
    logging.getLogger("tess").handlers.clear()


@pytest.fixture
def daemon_env(dirs, monkeypatch):
    """Fast-clock daemon sandbox: fake msal module, recorded notifications,
    and sub-second timing constants."""
    mod = types.ModuleType("msal")

    class SerializableTokenCache:
        pass
    mod.SerializableTokenCache = SerializableTokenCache
    monkeypatch.setitem(sys.modules, "msal", mod)

    notifications = []
    monkeypatch.setattr(tess, "notify",
                        lambda title, msg: notifications.append(title))
    monkeypatch.setattr(tess, "REFRESH_SIGNAL_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(tess, "FAILURE_BACKOFF_SECONDS", 0.03)
    monkeypatch.setattr(tess, "FAILURE_GIVE_UP_AFTER_SECONDS", 0.15)

    def install(app, **config_overrides):
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (app, object()))
        return notifications
    return install


def _write_session(cfg_writer, make_jwt, exp_offset=3600, **cfg_overrides):
    cfg = cfg_writer(**cfg_overrides)
    tess.atomic_write(tess.session_started_path(), str(time.time()))
    tess.atomic_write(tess.token_path(),
                      make_jwt({"exp": int(time.time()) + exp_offset}))
    return cfg


class TestDaemonRefresh:
    def test_signal_triggers_refresh_and_advances_token(
            self, daemon_env, write_config, make_jwt):
        new_exp = int(time.time()) + 7200
        app = FakeSilentApp([{"id_token": make_jwt({"exp": new_exp})}])
        notes = daemon_env(app)
        # Long refresh interval (never fires) + short cap (ends the test);
        # the pre-planted signal is the only refresh trigger.
        cfg = _write_session(write_config, make_jwt,
                             session_max_hours=0.0001)  # ~0.36s
        tess.refresh_signal_path().touch()
        tess.cmd_refresh_daemon(str(cfg))
        log = tess.log_path().read_text()
        assert "token refreshed (signal=True" in log
        assert app.calls >= 1
        assert notes == ["Session ended"]          # cap ended it, cleanly

    def test_scheduled_refresh_fires_on_interval(
            self, daemon_env, write_config, make_jwt):
        exp = int(time.time()) + 7200
        app = FakeSilentApp([
            {"id_token": make_jwt({"exp": exp})},
            {"id_token": make_jwt({"exp": exp + 60})},
        ])
        daemon_env(app)
        cfg = _write_session(write_config, make_jwt,
                             refresh_interval_minutes=0.001,   # ~60ms
                             session_max_hours=0.0001)         # ~360ms
        tess.cmd_refresh_daemon(str(cfg))
        assert app.calls >= 2                       # several cycles ran
        assert "token refreshed (signal=False" in tess.log_path().read_text()

    def test_stale_token_from_msal_is_flagged(
            self, daemon_env, write_config, make_jwt):
        # MSAL returning a token whose exp does NOT advance means the file
        # will silently go stale — the daemon must log it loudly.
        same_exp = int(time.time()) + 1800
        stale = make_jwt({"exp": same_exp})
        app = FakeSilentApp([{"id_token": stale}])
        daemon_env(app)
        cfg = write_config(session_max_hours=0.0001)
        tess.atomic_write(tess.session_started_path(), str(time.time()))
        tess.atomic_write(tess.token_path(), stale)   # same exp already there
        tess.refresh_signal_path().touch()
        tess.cmd_refresh_daemon(str(cfg))
        assert "did NOT advance" in tess.log_path().read_text()


class TestDaemonFailureHandling:
    def test_failure_notifies_reauth_then_gives_up(
            self, daemon_env, write_config, make_jwt):
        app = FakeSilentApp([])                     # every refresh fails
        notes = daemon_env(app)
        cfg = _write_session(write_config, make_jwt)  # default 8h cap
        tess.refresh_signal_path().touch()            # fail immediately
        tess.cmd_refresh_daemon(str(cfg))
        # First failure -> re-auth nudge; persistent failure -> session ended.
        assert notes == ["AWS session needs re-authentication",
                         "AWS session ended"]
        assert not tess.token_path().exists()         # torn down
        assert "ending session" in tess.log_path().read_text()
        assert app.calls >= 2                         # it did retry first

    def test_failures_escalate_from_warning_to_error(
            self, daemon_env, write_config, make_jwt):
        app = FakeSilentApp([])
        daemon_env(app)
        cfg = _write_session(write_config, make_jwt)
        tess.refresh_signal_path().touch()
        tess.cmd_refresh_daemon(str(cfg))
        log = tess.log_path().read_text()
        assert "[WARNING] silent refresh failed (attempt 1)" in log
        assert f"[ERROR] silent refresh failed (attempt "\
               f"{tess.TRANSIENT_FAILURE_GRACE})" in log

    def test_recovery_clears_the_failure_streak(
            self, daemon_env, write_config, make_jwt):
        good = {"id_token": make_jwt({"exp": int(time.time()) + 7200})}
        app = FakeSilentApp([None, None, good])   # two failures, then success
        notes = daemon_env(app)
        cfg = _write_session(write_config, make_jwt,
                             session_max_hours=0.0002)  # ~0.7s: room to recover
        tess.refresh_signal_path().touch()
        tess.cmd_refresh_daemon(str(cfg))
        log = tess.log_path().read_text()
        assert "token refreshed" in log            # recovered
        # Re-auth was nudged during the outage, but the session survived to
        # its natural cap — it must NOT have died from the failure path.
        assert notes[-1] == "Session ended"
        assert "failures persisted" not in log
