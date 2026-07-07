"""Platform-dependent path/env plumbing, exercised on every OS by forcing
sys.platform, plus the real `main()` entry via safe subprocesses (banner,
help, --version write no files)."""
import subprocess
import sys
from pathlib import Path

import pytest

import tess

TESS_PY = Path(tess.__file__)


class TestPlatformDirs:
    def test_data_dir_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", "C:/Users/x/AppData/Local")
        assert tess.data_dir() == Path("C:/Users/x/AppData/Local") / "tess"

    def test_data_dir_macos(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert tess.data_dir() == \
            Path.home() / "Library" / "Application Support" / "tess"

    def test_data_dir_linux(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert tess.data_dir() == Path.home() / ".local" / "share" / "tess"

    def test_config_dir_windows(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("APPDATA", "C:/Users/x/AppData/Roaming")
        assert tess.config_dir() == Path("C:/Users/x/AppData/Roaming") / "tess"

    def test_config_dir_linux_respects_xdg(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/xdg")
        assert tess.config_dir() == Path("/custom/xdg") / "tess"

    def test_config_dir_linux_default(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        assert tess.config_dir() == Path.home() / ".config" / "tess"


class TestSetPersistentEnv:
    @pytest.fixture
    def recorded_run(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tess.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd))
        return calls

    def test_windows_uses_setx(self, monkeypatch, recorded_run):
        monkeypatch.setattr(sys, "platform", "win32")
        tess.set_persistent_env("AWS_ROLE_ARN", "arn:x")
        assert recorded_run == [["setx", "AWS_ROLE_ARN", "arn:x"]]

    def test_macos_uses_launchctl(self, monkeypatch, recorded_run):
        monkeypatch.setattr(sys, "platform", "darwin")
        tess.set_persistent_env("AWS_REGION", "us-east-1")
        assert recorded_run == [["launchctl", "setenv", "AWS_REGION",
                                 "us-east-1"]]

    def test_linux_is_a_noop(self, monkeypatch, recorded_run):
        # Linux terminals read env.sh; there is no machine-wide setter.
        monkeypatch.setattr(sys, "platform", "linux")
        tess.set_persistent_env("AWS_REGION", "us-east-1")
        assert recorded_run == []


class TestMainEntry:
    """Real subprocess runs of tess.py. Only modes that write NOTHING are
    exercised this way — the child process is not sandboxed."""

    def _run(self, *argv):
        return subprocess.run([sys.executable, str(TESS_PY), *argv],
                              capture_output=True, text=True, timeout=30)

    def test_banner_mode(self):
        r = self._run("_banner")
        assert r.returncode == 0
        assert "federated AWS credentials" in r.stdout
        assert "Roman tessera" in r.stdout          # the name story

    def test_bare_invocation_shows_help_not_error(self):
        r = self._run()
        assert r.returncode == 0
        assert "usage:" in r.stdout

    def test_version_flag(self):
        r = self._run("--version")
        assert r.returncode == 0
        assert f"tess {tess.VERSION}" in r.stdout

    def test_help_lists_all_public_commands(self):
        r = self._run("--help")
        for cmd in ("start", "stop", "status", "refresh", "logs",
                    "config", "version"):
            assert cmd in r.stdout
