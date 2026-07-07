"""JWT payload decoding and display sanitization.

decode_jwt_payload is display-only (STS verifies signatures), so the invariant
under test is: garbage in, empty dict out — never an exception. safe_display
is the terminal/log injection barrier for claims parsed from that unverified
token.
"""
import pytest

import tess


class TestDecodeJwtPayload:
    def test_valid_token_roundtrip(self, make_jwt):
        payload = {"upn": "dev@example.com", "exp": 1893456000, "aud": "client-1"}
        assert tess.decode_jwt_payload(make_jwt(payload)) == payload

    def test_base64_padding_variants(self, make_jwt):
        # Payload lengths chosen to need 0, 1, and 2 padding chars once the
        # encoder strips '=' — the decoder must re-pad all of them.
        for filler in ("a", "ab", "abcd", "abcdef"):
            payload = {"x": filler}
            assert tess.decode_jwt_payload(make_jwt(payload)) == payload

    @pytest.mark.parametrize("garbage", [
        "",
        "only-one-part",
        "two.parts",
        "a.b.c.d",                      # four parts
        "head.!!!not-base64!!!.sig",    # undecodable payload
        "head.aGVsbG8.sig",             # decodes but is not JSON ("hello")
    ])
    def test_garbage_returns_empty_dict_never_raises(self, garbage):
        assert tess.decode_jwt_payload(garbage) == {}


class TestFmtExp:
    def test_valid_timestamp_formats_as_time(self):
        out = tess._fmt_exp(1893456000)
        assert len(out.split(":")) == 3  # HH:MM:SS

    @pytest.mark.parametrize("empty", [None, 0, ""])
    def test_falsy_exp_is_question_mark(self, empty):
        assert tess._fmt_exp(empty) == "?"

    def test_out_of_range_falls_back_to_raw_string(self):
        assert tess._fmt_exp(1e20) == str(1e20)


class TestSafeDisplay:
    def test_plain_value_unchanged(self):
        assert tess.safe_display("dev@example.com") == "dev@example.com"

    def test_strips_ansi_escape_and_control_chars(self):
        # ESC, CR, LF, BEL are non-printable and must be removed so a hostile
        # claim can't inject terminal escapes or forge extra log lines.
        assert tess.safe_display("a\x1b[31mb\rc\nd\x07e") == "a[31mbcde"

    def test_caps_length(self):
        assert len(tess.safe_display("x" * 1000)) == 256
        assert tess.safe_display("x" * 1000, limit=10) == "x" * 10

    def test_non_string_values_coerced(self):
        assert tess.safe_display(12345) == "12345"
