"""Config resolution ladder and validation.

The validators double as an injection barrier: every value that reaches
env.sh or the MSAL authority URL must reject shell metacharacters, so a
poisoned config file can never smuggle code into a sourced shell file.
"""
import json
from pathlib import Path

import pytest

import tess
from tess import ConfigError


# ---------- validate_config ----------

class TestValidateConfig:
    def test_valid_config_passes(self, valid_config):
        assert tess.validate_config(valid_config, Path("x")) == valid_config

    def test_shipped_example_template_is_valid(self):
        example = Path(tess.__file__).parent / "tess-config.example.json"
        data = json.loads(example.read_text())
        assert tess.validate_config(data, example) == data

    def test_non_object_rejected(self):
        with pytest.raises(ConfigError, match="not a JSON object"):
            tess.validate_config(["not", "a", "dict"], Path("x"))

    @pytest.mark.parametrize("key", ["tenant_id", "client_id", "role_arn"])
    def test_missing_required_key_rejected(self, valid_config, key):
        del valid_config[key]
        with pytest.raises(ConfigError, match=key):
            tess.validate_config(valid_config, Path("x"))

    @pytest.mark.parametrize("key", ["tenant_id", "client_id", "role_arn"])
    def test_empty_required_key_rejected(self, valid_config, key):
        valid_config[key] = ""
        with pytest.raises(ConfigError, match=key):
            tess.validate_config(valid_config, Path("x"))

    def test_tenant_domain_accepted(self, valid_config):
        valid_config["tenant_id"] = "contoso.onmicrosoft.com"
        tess.validate_config(valid_config, Path("x"))

    @pytest.mark.parametrize("bad", [
        "foo$bar",            # command substitution
        "foo;bar",            # command separator
        "foo bar",            # word splitting
        "foo'bar",            # quote escape
        'foo"bar',            # quote escape
        "foo`bar`",           # backtick substitution
    ])
    def test_tenant_id_rejects_shell_metacharacters(self, valid_config, bad):
        valid_config["tenant_id"] = bad
        with pytest.raises(ConfigError, match="tenant_id"):
            tess.validate_config(valid_config, Path("x"))

    @pytest.mark.parametrize("bad", ["id with space", "id$", "id;id", "id.dot"])
    def test_client_id_rejects_non_guid_charset(self, valid_config, bad):
        valid_config["client_id"] = bad
        with pytest.raises(ConfigError, match="client_id"):
            tess.validate_config(valid_config, Path("x"))

    @pytest.mark.parametrize("arn", [
        "arn:aws:iam::123456789012:role/dev-role",
        "arn:aws:iam::123456789012:role/path/to/role",
        "arn:aws-cn:iam::123456789012:role/cn-role",
        "arn:aws-us-gov:iam::123456789012:role/gov+=,.@_role",
    ])
    def test_role_arn_valid_variants(self, valid_config, arn):
        valid_config["role_arn"] = arn
        tess.validate_config(valid_config, Path("x"))

    @pytest.mark.parametrize("arn", [
        "not-an-arn",
        "arn:aws:iam::12345:role/short-account",           # account not 12 digits
        "arn:aws:iam::123456789012:user/not-a-role",       # wrong resource type
        "arn:aws:iam::123456789012:role/bad;injection",    # metacharacter
        "arn:aws:iam::123456789012:role/has space",        # space
    ])
    def test_role_arn_invalid_variants(self, valid_config, arn):
        valid_config["role_arn"] = arn
        with pytest.raises(ConfigError, match="role_arn"):
            tess.validate_config(valid_config, Path("x"))

    def test_region_optional_and_empty_ok(self, valid_config):
        del valid_config["region"]
        tess.validate_config(valid_config, Path("x"))
        valid_config["region"] = ""
        tess.validate_config(valid_config, Path("x"))

    @pytest.mark.parametrize("bad", ["US-EAST-1", "us east 1", "region;x"])
    def test_region_bad_format_rejected(self, valid_config, bad):
        valid_config["region"] = bad
        with pytest.raises(ConfigError, match="region"):
            tess.validate_config(valid_config, Path("x"))

    @pytest.mark.parametrize("key", ["refresh_interval_minutes", "session_max_hours"])
    @pytest.mark.parametrize("value", [0, -5, "abc", None, [50]])
    def test_optional_numeric_keys_must_be_positive_numbers(
            self, valid_config, key, value):
        valid_config[key] = value
        with pytest.raises(ConfigError, match=key):
            tess.validate_config(valid_config, Path("x"))

    @pytest.mark.parametrize("key", ["refresh_interval_minutes", "session_max_hours"])
    def test_optional_numeric_keys_accept_positive_numbers(self, valid_config, key):
        valid_config[key] = 0.5
        tess.validate_config(valid_config, Path("x"))


# ---------- load_config ----------

class TestLoadConfig:
    def test_loads_valid_file(self, write_config):
        path = write_config()
        assert tess.load_config(path)["role_arn"].startswith("arn:aws:iam::")

    def test_missing_file(self, dirs):
        with pytest.raises(ConfigError, match="not found"):
            tess.load_config(dirs / "nope.json")

    def test_invalid_json(self, dirs):
        p = dirs / "bad.json"
        p.write_text("{ not json")
        with pytest.raises(ConfigError, match="not valid JSON"):
            tess.load_config(p)


# ---------- resolve_config_path (the ladder) ----------

class TestResolveLadder:
    def test_explicit_flag_wins_over_everything(self, dirs, write_config, monkeypatch):
        flag_cfg = write_config(path=dirs / "explicit.json")
        write_config()  # config-dir default also present
        monkeypatch.setenv("TESS_CONFIG", str(write_config(path=dirs / "env.json")))
        path, rung = tess.resolve_config_path(str(flag_cfg))
        assert path == flag_cfg.resolve()
        assert rung == "--config flag"

    def test_explicit_flag_missing_is_hard_error(self, dirs):
        with pytest.raises(ConfigError, match="--config"):
            tess.resolve_config_path(str(dirs / "missing.json"))

    def test_env_var_wins_over_cwd(self, dirs, write_config, monkeypatch):
        env_cfg = write_config(path=dirs / "env.json")
        cwd = dirs / "cwd"
        write_config(path=cwd / tess.CONFIG_FILENAME)
        monkeypatch.chdir(cwd)
        monkeypatch.setenv("TESS_CONFIG", str(env_cfg))
        path, rung = tess.resolve_config_path(None)
        assert path == env_cfg.resolve()
        assert rung == "$TESS_CONFIG"

    def test_env_var_pointing_nowhere_is_hard_error(self, dirs, monkeypatch):
        monkeypatch.setenv("TESS_CONFIG", str(dirs / "missing.json"))
        with pytest.raises(ConfigError, match="TESS_CONFIG"):
            tess.resolve_config_path(None)

    def test_empty_env_var_falls_through(self, dirs, write_config, monkeypatch):
        default_cfg = write_config()
        monkeypatch.setenv("TESS_CONFIG", "")
        monkeypatch.chdir(dirs)  # no tess-config.json here
        path, rung = tess.resolve_config_path(None)
        assert path == default_cfg.resolve()
        assert rung == "config-dir default"

    def test_cwd_wins_over_config_dir_default(self, dirs, write_config, monkeypatch):
        write_config()  # config-dir default
        cwd = dirs / "cwd"
        cwd_cfg = write_config(path=cwd / tess.CONFIG_FILENAME)
        monkeypatch.chdir(cwd)
        path, rung = tess.resolve_config_path(None)
        assert path == cwd_cfg.resolve()
        assert rung == "current dir"

    def test_nothing_found_lists_searched_locations(self, dirs, monkeypatch):
        monkeypatch.chdir(dirs)
        with pytest.raises(ConfigError, match="Searched"):
            tess.resolve_config_path(None)


# ---------- session_or_resolved_config ----------

class TestSessionConfig:
    def test_recorded_session_config_wins(self, dirs, write_config, monkeypatch):
        write_config()  # would be the ladder's answer
        recorded = write_config(path=dirs / "recorded.json")
        sc = tess.session_config_path()
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text(str(recorded))
        monkeypatch.chdir(dirs)
        path, rung = tess.session_or_resolved_config(None)
        assert path == recorded
        assert "session" in rung

    def test_explicit_flag_bypasses_recorded_session(self, dirs, write_config):
        recorded = write_config(path=dirs / "recorded.json")
        sc = tess.session_config_path()
        sc.parent.mkdir(parents=True, exist_ok=True)
        sc.write_text(str(recorded))
        explicit = write_config(path=dirs / "explicit.json")
        path, rung = tess.session_or_resolved_config(str(explicit))
        assert path == explicit.resolve()
        assert rung == "--config flag"

    def test_no_session_falls_back_to_ladder(self, dirs, write_config, monkeypatch):
        default_cfg = write_config()
        monkeypatch.chdir(dirs)
        path, rung = tess.session_or_resolved_config(None)
        assert path == default_cfg.resolve()
        assert rung == "config-dir default"
