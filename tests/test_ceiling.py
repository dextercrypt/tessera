"""Last reachable branches: force-restart, win32 env sync, verbose status,
signal-handler cleanup, notify's swallow, banner-on-tty, and main() dispatch.

Everything beyond these needs something real: a live Entra tenant (interactive
auth, silent refresh against Microsoft), a real Windows host (registry,
taskkill, Win32 process APIs — exercised by the CI windows runner), a real
keychain (msal-extensions persistence), or an interactive terminal
(`logs -f`). Those are runbook territory, not unit-test territory.
"""
import logging
import os
import signal
import sys
import time
import types

import pytest

import tess


class TestCmdStartForceRestart:
    def test_force_stops_existing_session_first(
            self, dirs, write_config, monkeypatch, make_jwt, capsys):
        write_config()
        monkeypatch.chdir(dirs)
        # An "existing session": alive PID (our own — kill is recorded, not real).
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        killed = []
        monkeypatch.setattr(tess, "kill_process", lambda pid: killed.append(pid))
        monkeypatch.setattr(tess, "spawn_daemon", lambda cfg: 4242)
        monkeypatch.setattr(tess, "set_persistent_env", lambda k, v: None)
        for var in tess.CHANGEABLE_ENV_VARS:
            monkeypatch.setenv(var, "sentinel")
        mod = types.ModuleType("msal")
        mod.SerializableTokenCache = type("STC", (), {})
        monkeypatch.setitem(sys.modules, "msal", mod)
        token = make_jwt({"preferred_username": "dev@example.com",
                          "exp": int(time.time()) + 3600})
        app = types.SimpleNamespace(
            acquire_token_interactive=lambda **kw: {"id_token": token})
        monkeypatch.setattr(tess, "build_msal_app",
                            lambda c, clear_cache=False: (app, object()))

        args = types.SimpleNamespace(config=None, force=True, quiet=False)
        assert tess.cmd_start(args) == 0
        assert killed == [os.getpid()]            # old session stopped
        assert tess.pid_path().read_text() == "4242"  # new one recorded
        assert "Stopping existing session" in capsys.readouterr().out


class TestInjectWin32Branch:
    def test_windows_uses_persistent_env_not_env_sh(
            self, dirs, valid_config, make_jwt, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(tess, "set_persistent_env",
                            lambda k, v: calls.append(k))
        for var in tess.CHANGEABLE_ENV_VARS:
            monkeypatch.setenv(var, "sentinel")
        monkeypatch.setattr(sys, "platform", "win32")
        token = make_jwt({"preferred_username": "dev@example.com"})
        tess.inject_changeable_env(valid_config, token, quiet=False)
        assert "AWS_ROLE_ARN" in calls            # registry path used
        assert not tess.env_sh_path().exists()    # env.sh is not a Windows thing
        assert "relaunch" in capsys.readouterr().out


class TestStatusVerbose:
    def test_verbose_shows_pid_log_and_token_paths(
            self, dirs, make_jwt, capsys):
        tess.atomic_write(tess.pid_path(), str(os.getpid()), mode=0o644)
        tess.atomic_write(tess.session_started_path(), str(time.time()))
        tess.atomic_write(tess.token_path(), make_jwt(
            {"upn": "dev@example.com", "exp": int(time.time()) + 3000}))
        args = types.SimpleNamespace(json=False, verbose=True)
        assert tess.cmd_status(args) == 0
        out = capsys.readouterr().out
        assert str(tess.log_path()) in out
        assert str(tess.token_path()) in out
        assert f"PID:        {os.getpid()}" in out


class TestNotify:
    def test_notify_never_raises_without_notifier_dep(self, monkeypatch):
        # desktop_notifier isn't installed in the test env: the ImportError
        # must be swallowed — notifications are nice-to-have, never fatal.
        monkeypatch.setitem(sys.modules, "desktop_notifier", None)
        tess.notify("title", "message")  # no exception


@pytest.mark.skipif(sys.platform == "win32",
                    reason="signal-based cleanup is the POSIX logout path")
class TestDaemonSignalCleanup:
    def test_sigterm_cleans_session_and_exits(self, dirs):
        logger = logging.getLogger("tess-signal-test")
        tess.atomic_write(tess.token_path(), "live-token")
        old_term = signal.getsignal(signal.SIGTERM)
        old_hup = signal.getsignal(signal.SIGHUP)
        try:
            tess.install_daemon_signal_handlers(logger)
            with pytest.raises(SystemExit) as exc:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.5)  # deliver the signal to this thread
            assert exc.value.code == 0
            assert not tess.token_path().exists()  # logout leaves no token
        finally:
            signal.signal(signal.SIGTERM, old_term)
            signal.signal(signal.SIGHUP, old_hup)


class TestBannerParserTty:
    def test_help_carries_banner_on_a_terminal(self, monkeypatch):
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
        text = tess.build_parser().format_help()
        assert tess.TAGLINE in text

    def test_help_is_plain_when_piped(self, monkeypatch):
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
        text = tess.build_parser().format_help()
        assert tess.TAGLINE not in text


class TestMainDispatch:
    """main() executed in-process (sandboxed by `dirs`), covering the argv
    dispatch that subprocess-based tests can't show in coverage."""

    def _main(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["tess", *argv])
        with pytest.raises(SystemExit) as exc:
            tess.main()
        return exc.value.code

    def test_status_dispatch(self, dirs, monkeypatch, capsys):
        assert self._main(monkeypatch, "status") == 0
        assert "No active session" in capsys.readouterr().out

    def test_config_error_becomes_exit_1(self, dirs, monkeypatch, capsys):
        monkeypatch.chdir(dirs)  # nowhere resolves a config
        assert self._main(monkeypatch, "start") == 1
        assert "ERROR" in capsys.readouterr().err

    def test_hidden_daemon_dispatch(self, dirs, monkeypatch):
        # Bad config path -> daemon exits 1 after cleaning up. Stays sandboxed.
        code = self._main(monkeypatch, "_refresh-daemon",
                          str(dirs / "missing.json"))
        assert code == 1
        logging.getLogger("tess").handlers.clear()

    def test_hidden_revelio_dispatch(self, dirs, monkeypatch, capsys):
        assert self._main(monkeypatch, "revelio") == 1
        assert "no token file" in capsys.readouterr().out

    def test_bare_invocation_prints_help(self, dirs, monkeypatch, capsys):
        assert self._main(monkeypatch) == 0
        assert "usage:" in capsys.readouterr().out
