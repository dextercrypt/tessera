"""Atomic file writes, private directories, and the audit log.

The security invariants: the token file is never world-readable (0600 from
birth), the data dir is owner-only (0700), and a log event can never span
multiple lines (CR/LF collapse defeats log forgery).
"""
import os
import re
import stat
import sys

import pytest

import tess
from tess import TokenWriteError

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file modes are cosmetic on Windows")


class TestAtomicWrite:
    def test_writes_content(self, dirs):
        target = dirs / "data" / "token"
        tess.atomic_write(target, "the-token")
        assert target.read_text() == "the-token"

    def test_overwrites_existing(self, dirs):
        target = dirs / "data" / "token"
        tess.atomic_write(target, "old")
        tess.atomic_write(target, "new")
        assert target.read_text() == "new"

    def test_creates_parent_dirs(self, dirs):
        target = dirs / "a" / "b" / "c" / "file"
        tess.atomic_write(target, "x")
        assert target.read_text() == "x"

    def test_no_temp_file_left_behind(self, dirs):
        target = dirs / "data" / "token"
        tess.atomic_write(target, "x")
        assert list(target.parent.iterdir()) == [target]

    @posix_only
    def test_default_mode_is_owner_only(self, dirs):
        target = dirs / "data" / "token"
        tess.atomic_write(target, "secret")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    @posix_only
    def test_explicit_mode_respected(self, dirs):
        target = dirs / "data" / "daemon.pid"
        tess.atomic_write(target, "123", mode=0o644)
        assert stat.S_IMODE(target.stat().st_mode) == 0o644

    def test_unwritable_destination_raises_token_write_error(self, dirs):
        # A path whose parent is a regular FILE can never be created.
        blocker = dirs / "blocker"
        blocker.write_text("i am a file")
        with pytest.raises(TokenWriteError):
            tess.atomic_write(blocker / "child", "x")

    @posix_only
    def test_symlink_planted_at_temp_path_is_refused(self, dirs):
        # O_NOFOLLOW defense: an attacker-planted symlink at the .tmp path
        # must not let the token be written through to another location.
        target = dirs / "data" / "token"
        target.parent.mkdir(parents=True, exist_ok=True)
        (dirs / "data" / "token.tmp").symlink_to(dirs / "attacker-controlled")
        with pytest.raises(TokenWriteError):
            tess.atomic_write(target, "secret")


class TestEnsurePrivateDir:
    def test_creates_directory(self, dirs):
        d = tess.ensure_private_dir(dirs / "new" / "nested")
        assert d.is_dir()

    @posix_only
    def test_mode_is_0700(self, dirs):
        d = tess.ensure_private_dir(dirs / "private")
        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    @posix_only
    def test_tightens_existing_directory(self, dirs):
        d = dirs / "loose"
        d.mkdir()
        d.chmod(0o755)
        tess.ensure_private_dir(d)
        assert stat.S_IMODE(d.stat().st_mode) == 0o700


class TestLogEvent:
    def test_appends_timestamped_line(self, dirs):
        tess.log_event("session started")
        tess.log_event("token refreshed", level="WARNING")
        lines = tess.log_path().read_text().splitlines()
        assert len(lines) == 2
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[INFO\] "
                        r"session started$", lines[0])
        assert "[WARNING] token refreshed" in lines[1]

    def test_crlf_collapsed_to_single_line(self, dirs):
        # A UPN with embedded newlines must not be able to forge log entries.
        tess.log_event("user=evil\r\n2026-01-01 00:00:00 [INFO] forged entry")
        lines = tess.log_path().read_text().splitlines()
        assert len(lines) == 1
        assert "forged entry" in lines[0]  # present, but on the SAME line

    @posix_only
    def test_log_file_is_owner_only(self, dirs):
        tess.log_event("x")
        assert stat.S_IMODE(tess.log_path().stat().st_mode) == 0o600

    def test_never_raises_even_when_unwritable(self, dirs, monkeypatch):
        # Point the log inside a path blocked by a regular file: log_event
        # must swallow the failure (logging must never break a command).
        blocker = dirs / "blocker"
        blocker.write_text("file")
        monkeypatch.setattr(tess, "data_dir", lambda: blocker / "sub")
        tess.log_event("x")  # no exception


class TestBanner:
    def test_banner_text_contains_tagline(self):
        assert tess.TAGLINE in tess.banner_text()

    def test_banner_suppressed_when_not_a_tty(self, capsys):
        tess.print_banner()  # captured stdout is not a tty
        assert capsys.readouterr().out == ""

    def test_banner_forced(self, capsys):
        tess.print_banner(force=True, with_story=True)
        out = capsys.readouterr().out
        assert tess.TAGLINE in out
        assert "tessera" in out  # the name story
