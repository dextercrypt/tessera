"""CLI surface: every advertised subcommand parses, defaults are stable, and
the version flag reports the pinned VERSION."""
import pytest

import tess


@pytest.fixture
def parser():
    return tess.build_parser()


class TestParser:
    @pytest.mark.parametrize("cmd", [
        "start", "stop", "status", "refresh", "logs", "config", "version"])
    def test_every_subcommand_parses(self, parser, cmd):
        args = parser.parse_args([cmd])
        assert callable(args.func)

    def test_start_flags(self, parser):
        args = parser.parse_args(
            ["start", "--force", "--quiet", "--config", "/tmp/x.json"])
        assert args.force and args.quiet and args.config == "/tmp/x.json"

    def test_start_defaults(self, parser):
        args = parser.parse_args(["start"])
        assert not args.force and not args.quiet and args.config is None

    def test_status_flags(self, parser):
        args = parser.parse_args(["status", "--json", "-v"])
        assert args.json and args.verbose

    def test_logs_defaults(self, parser):
        args = parser.parse_args(["logs"])
        assert args.lines == 50 and not args.follow

    def test_logs_flags(self, parser):
        args = parser.parse_args(["logs", "-n", "10", "-f"])
        assert args.lines == 10 and args.follow

    def test_version_flag_exits_zero(self, parser, capsys):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--version"])
        assert exc.value.code == 0
        assert tess.VERSION in capsys.readouterr().out

    def test_unknown_command_exits_nonzero(self, parser, capsys):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["frobnicate"])
        assert exc.value.code != 0

    def test_command_is_required(self, parser, capsys):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([])
        assert exc.value.code != 0

    def test_hidden_commands_not_advertised(self, parser):
        help_text = parser.format_help()
        assert "_refresh-daemon" not in help_text
        assert "revelio" not in help_text
        assert "_banner" not in help_text
