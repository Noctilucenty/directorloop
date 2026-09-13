"""Credentials must not escape via deep values or exception-like strings."""
import json

from directorloop.observability.weave_ops import redact_inputs, redact_output


def test_nested_secrets_and_camel_case_headers_are_redacted():
    value = {"AccessToken": "private-access", "headers": {"SET-COOKIE": "private-cookie"},
             "nested": {"client_secret": "private-client", "safe_count": 12}}
    result = redact_inputs(value)
    assert "private-" not in json.dumps(result)
    assert result["nested"]["safe_count"] == 12


def test_depth_limit_does_not_dump_unredacted_repr():
    value = {"api_key": "private-deep"}
    for _ in range(12):
        value = {"nested": value}
    assert "private-deep" not in json.dumps(redact_output(value))


def test_embedded_authorization_and_signed_url_are_scrubbed():
    message = "request failed Authorization: Bearer private-bearer https://media.test/clip.mp4?id=12&X-Amz-Signature=private-signed"
    result = redact_output(message)
    assert "private-bearer" not in result and "private-signed" not in result
    assert "id=12" in result
    assert "user-secret" not in redact_output("https://user:user-secret@example.test/path")


def test_unknown_object_repr_cannot_leak_credentials():
    class Unsafe:
        def __repr__(self):
            return "private-credential"
    assert redact_output(Unsafe()) == "<Unsafe>"


def test_redaction_does_not_mutate_live_inputs():
    value = {"API_KEY": "private-value", "count": 2}
    redact_inputs(value)
    assert value["API_KEY"] == "private-value"


def test_basic_creative_information_is_not_treated_as_a_credential():
    prose = "The opening needs basic information about the product."
    assert redact_output(prose) == prose
    assert "dXNlcjpwYXNzd29yZA==" not in redact_output("Authorization: Basic dXNlcjpwYXNzd29yZA==")
