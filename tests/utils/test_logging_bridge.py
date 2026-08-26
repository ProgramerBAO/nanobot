from nanobot.utils.logging_bridge import _redact_sensitive_log_text


def test_redact_sensitive_log_text_hides_url_and_structured_credentials() -> None:
    message = (
        "connected to wss://example/ws?access_key=abc123&ticket=t-456 "
        'payload={"app_secret": "secret", "access_token": "bearer"} '
        "client_secret=plain"
    )

    redacted = _redact_sensitive_log_text(message)

    assert "abc123" not in redacted
    assert "t-456" not in redacted
    assert '"secret"' not in redacted
    assert '"bearer"' not in redacted
    assert "=plain" not in redacted
    assert redacted.count("[REDACTED]") == 5
    assert "access_key=" in redacted
