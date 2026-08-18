from integration.gateway_startup.redaction import redact_gateway_console_line


def test_redacts_common_gateway_secret_shapes():
    line = (
        'Authorization: Bearer token-value Cookie: session=value '
        'api_key=key-value url=https://user:password@example.test/?access_token=query-value '
        'jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature'
    )

    result = redact_gateway_console_line(line)

    assert "token-value" not in result
    assert "session=value" not in result
    assert "key-value" not in result
    assert "user:password" not in result
    assert "query-value" not in result
    assert "eyJhbGci" not in result
    # Cookie headers deliberately redact the remainder of a mixed diagnostic
    # line, so this checks safety rather than a particular replacement count.
    assert result.count("<redacted>") >= 2
