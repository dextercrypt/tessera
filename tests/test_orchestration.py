"""cmd_start orchestration and MSAL-adjacent plumbing, with the MSAL library
stubbed at its interface.

These tests do NOT pretend to test authentication itself (that needs a live
Entra tenant). They test what tess does AROUND the auth call: write ordering,
failure cleanup, daemon spawn/kill bookkeeping, cache handling, banner output.
"""
import os
import sys
import time
import types

import pytest

import tess


class FakeInteractiveApp:
    """Stands in for msal.PublicClientApplication in cmd_start."""
    def __init__(self, result):
        self.result = result

    def acquire_token_interactive(self, scopes, prompt=None,
                                  success_template=None, error_template=None):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def fake_msal_module(monkeypatch):
    """Inject a minimal fake `msal` module so functions that lazily
    `from msal import SerializableTokenCache` work without the real dep."""
    mod = types.ModuleType("msal")

    class SerializableTokenCache:
        def __init__(self):
            self.has_state_changed = False
            self._state = "{}"

        def serialize(self):
            return self._state

        def deserialize(self, s):
            self._state = s

        def find(self, *a, **k):
            return []

    class PublicClientApplication:
        def __init__(self, client_id, authority=None, token_cache=None):
            self.client_id = client_id
            self.authority = authority
            self.token_cache = token_cache

    mod.SerializableTokenCache = SerializableTokenCache
    mod.PublicClientApplication = PublicClientApplication
    monkeypatch.setitem(sys.modules, "msal", mod)
    return mod


@pytest.fixture
def start_env(dirs, write_config, fake_msal_module, monkeypatch, make_jwt):
    """Everything cmd_start needs except the token: sandboxed dirs, a config,
    a recorded spawn_daemon, muted persistent-env calls, protected environ."""
    cfg = write_config()
    monkeypatch.chdir(dirs)
    spawned = []
    monkeypatch.setattr(tess, "spawn_daemon",
                        lambda config_file: spawned.append(config_file) or 4242)
    killed = []
    monkeypatch.setattr(tess, "kill_process", lambda pid: killed.append(pid))
    monkeypatch.setattr(tess, "set_persistent_env", lambda k, v: None)
    for var in tess.CHANGEABLE_ENV_VARS:
        monkeypatch.setenv(var, "sentinel-before")
    token = make_jwt({"preferred_username": "dev@example.com",
                      "exp": int(time.time()) + 3600})
    return types.SimpleNamespace(cfg=cfg, spawned=spawned, killed=killed,
                                 token=token)


def _args(**kw):
    ns = types.SimpleNamespace(config=None, force=False, quiet=False)
    ns.__dict__.update(kw)
    return ns


class TestCmdStartSuccess:
    def test_full_success_path(self, start_env, monkeypatch, capsys):
        app = FakeInteractiveApp({"id_token": start_env.token})
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (app, object()))
        assert tess.cmd_start(_args()) == 0
        # Session state on disk:
        assert tess.token_path().read_text() == start_env.token
        assert tess.pid_path().read_text() == "4242"
        assert tess.session_config_path().read_text() == str(start_env.cfg)
        assert tess.session_started_path().is_file()
        # Daemon handed the exact resolved config:
        assert start_env.spawned == [start_env.cfg]
        # User-facing banner + audit trail:
        assert "Session active for: dev@example.com" in capsys.readouterr().out
        assert "interactive sign-in" in tess.log_path().read_text()

    def test_session_started_written_before_token(self, start_env, monkeypatch):
        # The cap must be enforceable from the moment the token is usable,
        # so the start timestamp must hit disk first.
        app = FakeInteractiveApp({"id_token": start_env.token})
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (app, object()))
        order = []
        real_write = tess.atomic_write

        def recording_write(path, content, mode=0o600):
            order.append(path)
            real_write(path, content, mode)
        monkeypatch.setattr(tess, "atomic_write", recording_write)
        tess.cmd_start(_args(quiet=True))
        assert order.index(tess.session_started_path()) \
            < order.index(tess.token_path())

    def test_already_active_without_force_short_circuits(
            self, start_env, monkeypatch, capsys):
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        called = []
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda *a, **k: called.append(1))
        assert tess.cmd_start(_args()) == 0
        assert called == []          # never reached auth
        assert "already active" in capsys.readouterr().out


class TestCmdStartFailures:
    def test_auth_exception_returns_error(self, start_env, monkeypatch, capsys):
        app = FakeInteractiveApp(RuntimeError("browser closed"))
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (app, object()))
        assert tess.cmd_start(_args()) == 1
        assert "Authentication failed" in capsys.readouterr().err
        assert not tess.token_path().exists()

    def test_result_without_id_token_returns_error(
            self, start_env, monkeypatch, capsys):
        app = FakeInteractiveApp({"error_description": "AADSTS50076: MFA denied"})
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (app, object()))
        assert tess.cmd_start(_args()) == 1
        assert "MFA denied" in capsys.readouterr().err
        assert not tess.token_path().exists()

    def test_pid_write_failure_kills_spawned_daemon(
            self, start_env, monkeypatch, capsys):
        # A daemon we can't record is a daemon we can't stop — cmd_start must
        # kill it and abort rather than orphan it.
        app = FakeInteractiveApp({"id_token": start_env.token})
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (app, object()))
        real_write = tess.atomic_write

        def flaky_write(path, content, mode=0o600):
            if path == tess.pid_path():
                raise tess.TokenWriteError("disk full")
            real_write(path, content, mode)
        monkeypatch.setattr(tess, "atomic_write", flaky_write)
        assert tess.cmd_start(_args(quiet=True)) == 1
        assert start_env.killed == [4242]      # spawned daemon reaped
        assert not tess.token_path().exists()  # session fully rolled back

    def test_token_write_failure_aborts_before_daemon(
            self, start_env, monkeypatch, capsys):
        app = FakeInteractiveApp({"id_token": start_env.token})
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (app, object()))
        real_write = tess.atomic_write

        def flaky_write(path, content, mode=0o600):
            if path == tess.token_path():
                raise tess.TokenWriteError("disk full")
            real_write(path, content, mode)
        monkeypatch.setattr(tess, "atomic_write", flaky_write)
        assert tess.cmd_start(_args(quiet=True)) == 1
        assert start_env.spawned == []         # daemon never launched
        assert not tess.session_started_path().exists()


class TestBuildMsalApp:
    """The keychain-unavailable fallback path, with fake msal modules."""

    @pytest.fixture
    def no_extensions(self, fake_msal_module, monkeypatch):
        # sys.modules[name] = None makes `import msal_extensions` raise
        # ImportError -> build_msal_app must fall back gracefully.
        monkeypatch.setitem(sys.modules, "msal_extensions", None)
        return fake_msal_module

    def test_fallback_warns_and_builds_app(self, dirs, no_extensions,
                                           valid_config, capsys):
        app, cache = tess.build_msal_app(valid_config)
        assert valid_config["tenant_id"] in app.authority
        assert app.client_id == valid_config["client_id"]
        assert "UNENCRYPTED" in capsys.readouterr().err

    def test_clear_cache_deletes_existing_cache_file(self, dirs, no_extensions,
                                                     valid_config, capsys):
        tess.msal_cache_path().parent.mkdir(parents=True, exist_ok=True)
        tess.msal_cache_path().write_text("old-cache")
        tess.build_msal_app(valid_config, clear_cache=True)
        assert not tess.msal_cache_path().exists()

    def test_existing_plain_cache_is_loaded(self, dirs, no_extensions,
                                            valid_config, capsys):
        tess.msal_cache_path().parent.mkdir(parents=True, exist_ok=True)
        tess.msal_cache_path().write_text('{"AccessToken": {}}')
        app, cache = tess.build_msal_app(valid_config)
        assert cache._state == '{"AccessToken": {}}'

    def test_corrupt_plain_cache_starts_fresh(self, dirs, no_extensions,
                                              valid_config, capsys,
                                              monkeypatch):
        tess.msal_cache_path().parent.mkdir(parents=True, exist_ok=True)
        tess.msal_cache_path().write_text("corrupt")

        def raising_deserialize(self, s):
            raise ValueError("bad json")
        monkeypatch.setattr(sys.modules["msal"].SerializableTokenCache,
                            "deserialize", raising_deserialize)
        app, cache = tess.build_msal_app(valid_config)  # must not raise


class TestPlainCachePersistence:
    def test_changed_cache_is_saved_owner_only(self, dirs, fake_msal_module):
        cache = fake_msal_module.SerializableTokenCache()
        cache.has_state_changed = True
        cache._state = '{"refresh_token": "rt-secret"}'
        tess.save_plain_cache_if_needed(cache)
        p = tess.msal_cache_path()
        assert p.read_text() == '{"refresh_token": "rt-secret"}'
        if sys.platform != "win32":
            import stat
            assert stat.S_IMODE(p.stat().st_mode) == 0o600

    def test_unchanged_cache_not_written(self, dirs, fake_msal_module):
        cache = fake_msal_module.SerializableTokenCache()
        cache.has_state_changed = False
        tess.save_plain_cache_if_needed(cache)
        assert not tess.msal_cache_path().exists()


class TestSessionBanner:
    def test_prints_identity_and_cadence(self, valid_config, make_jwt, capsys):
        token = make_jwt({"preferred_username": "dev@example.com"})
        tess.print_session_banner(token, valid_config)
        out = capsys.readouterr().out
        assert "Session active for: dev@example.com" in out
        assert "every 50 minutes" in out
        assert "8-hour cap" in out

    def test_respects_configured_overrides(self, valid_config, make_jwt, capsys):
        valid_config["refresh_interval_minutes"] = 30
        valid_config["session_max_hours"] = 4
        tess.print_session_banner(make_jwt({"upn": "x@y.z"}), valid_config)
        out = capsys.readouterr().out
        assert "every 30 minutes" in out
        assert "4-hour cap" in out
