import base64
import json

from danbi_common.pii.tokenize import Tokenizer, normalize

TOK = Tokenizer(key=b"k" * 32, key_version="v1")


def test_phone_normalization_variants_match():
    assert normalize("phone", "010-1234-5678") == "01012345678"
    assert normalize("phone", "+82 10 1234 5678") == "01012345678"
    assert TOK.token("phone", "010-1234-5678") == TOK.token("phone", "+82 10-1234-5678")


def test_email_case_insensitive():
    assert TOK.token("email", " Kim@Example.com ") == TOK.token("email", "kim@example.com")


def test_kind_separates_namespaces():
    assert TOK.token("phone", "01012345678") != TOK.token("account", "01012345678")


def test_token_format_and_version():
    t = TOK.token("email", "a@b.co")
    assert t.startswith("email_v1_") and len(t.split("_")[2]) == 24
    assert Tokenizer.key_version_of(t) == "v1"


def test_different_key_different_token():
    other = Tokenizer(key=b"x" * 32, key_version="v2")
    assert TOK.token("name", "김단비") != other.token("name", "김단비")


def test_empty_values_are_none():
    assert TOK.token("phone", None) is None
    assert TOK.token("phone", "---") is None


def test_from_secret_string():
    secret = json.dumps({"key_b64": base64.b64encode(b"k" * 32).decode(), "key_version": "v1"})
    assert Tokenizer.from_secret_string(secret).token("name", "김 단비") == TOK.token("name", "김단비")
