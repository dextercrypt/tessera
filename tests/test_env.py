"""Environment-variable sync: session names, scopes, and env.sh generation.

env.sh is sourced by every new shell, so the quoting test is a security test:
a value must never be able to break out of its single quotes and execute code.
"""
import subprocess
import sys

import pytest

import tess

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="env.sh is a macOS/Linux mechanism")


class TestSanitizeSessionName:
    def test_upn_passes_through(self):
        assert tess.sanitize_session_name("dev.user@example.com") == \
            "dev.user@example.com"

    def test_disallowed_chars_stripped(self):
        assert tess.sanitize_session_name("a!b#c$d e/f") == "abcdef"

    def test_capped_at_64_chars(self):
        assert len(tess.sanitize_session_name("x" * 200)) == 64

    def test_empty_falls_back_to_default(self):
        assert tess.sanitize_session_name("") == "tess-user"
        assert tess.sanitize_session_name("!!!") == "tess-user"


class TestDeriveSessionName:
    def test_uses_preferred_username(self, make_jwt):
        token = make_jwt({"preferred_username": "dev@example.com"})
        assert tess.derive_session_name(token) == "dev@example.com"

    def test_falls_back_to_upn_claim(self, make_jwt):
        token = make_jwt({"upn": "fallback@example.com"})
        assert tess.derive_session_name(token) == "fallback@example.com"

    def test_undecodable_token_gets_default(self):
        assert tess.derive_session_name("garbage") == "tess-user"


class TestResolveScopes:
    def test_default_is_own_resource(self, valid_config):
        cid = valid_config["client_id"]
        assert tess.resolve_scopes(valid_config) == [f"{cid}/.default"]

    def test_explicit_string_is_split(self, valid_config):
        valid_config["scope"] = "openid profile"
        assert tess.resolve_scopes(valid_config) == ["openid", "profile"]

    def test_explicit_list_passes_through(self, valid_config):
        valid_config["scope"] = ["a", "b"]
        assert tess.resolve_scopes(valid_config) == ["a", "b"]

    def test_empty_string_means_legacy_no_scopes(self, valid_config):
        valid_config["scope"] = ""
        assert tess.resolve_scopes(valid_config) == []


@posix_only
class TestWriteEnvSh:
    def test_exports_all_values(self, dirs):
        tess.write_env_sh({"AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/r",
                           "AWS_REGION": "us-east-1"})
        content = tess.env_sh_path().read_text()
        assert "export AWS_ROLE_ARN='arn:aws:iam::123456789012:role/r'\n" in content
        assert "export AWS_REGION='us-east-1'\n" in content

    def test_single_quotes_cannot_break_out(self, dirs):
        # The POSIX escape ' -> '\'' must keep a hostile value inert when
        # env.sh is sourced. Prove it by actually sourcing the file.
        evil = "x'; echo INJECTED; '"
        tess.write_env_sh({"TESS_TEST_VAR": evil})
        proc = subprocess.run(
            ["sh", "-c", f". '{tess.env_sh_path()}' && printf %s \"$TESS_TEST_VAR\""],
            capture_output=True, text=True)
        assert proc.returncode == 0
        assert proc.stdout == evil          # round-trips as pure data
        assert "INJECTED" not in proc.stderr


@pytest.mark.skipif(sys.platform == "win32",
                    reason="exercises the darwin/linux env.sh path")
class TestInjectChangeableEnv:
    @pytest.fixture
    def recorded(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            tess, "set_persistent_env", lambda k, v: calls.append((k, v)))
        # Protect the test runner's own environment from the os.environ.update.
        for var in tess.CHANGEABLE_ENV_VARS:
            monkeypatch.setenv(var, "sentinel-before")
        return calls

    def test_sets_role_region_and_session_name(
            self, dirs, valid_config, make_jwt, recorded, capsys):
        token = make_jwt({"preferred_username": "dev@example.com"})
        values = tess.inject_changeable_env(valid_config, token, quiet=True)
        assert values["AWS_ROLE_ARN"] == valid_config["role_arn"]
        assert values["AWS_REGION"] == "us-east-1"
        assert values["AWS_DEFAULT_REGION"] == "us-east-1"
        assert values["AWS_ROLE_SESSION_NAME"] == "dev@example.com"
        # env.sh written for new shells; current process updated too.
        assert "AWS_ROLE_ARN" in tess.env_sh_path().read_text()
        import os
        assert os.environ["AWS_ROLE_ARN"] == valid_config["role_arn"]

    def test_missing_region_warns_and_never_invents_one(
            self, dirs, valid_config, make_jwt, recorded, capsys):
        del valid_config["region"]
        token = make_jwt({"preferred_username": "dev@example.com"})
        values = tess.inject_changeable_env(valid_config, token, quiet=False)
        assert "AWS_REGION" not in values
        assert "AWS_DEFAULT_REGION" not in values
        assert "WARNING" in capsys.readouterr().err
