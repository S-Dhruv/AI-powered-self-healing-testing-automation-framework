from framework.security.masking import mask, mask_secrets_from_env


def test_masks_email():
    assert "***MASKED-EMAIL***" in mask("contact qa.tester@test.com now")
    assert "qa.tester@test.com" not in mask("contact qa.tester@test.com now")


def test_masks_password_label():
    out = mask("Password: Test@1234")
    assert "Test@1234" not in out
    assert "***MASKED***" in out


def test_masks_card_number():
    out = mask("Card No: 4111111111111111")
    assert "4111111111111111" not in out


def test_canary_secret_never_leaves_via_mask():
    """Plant a fake canary secret and prove it is stripped before it could reach
    a log line or an LLM payload, exactly as required by spec R8."""
    canary = "CANARY-SECRET-DO-NOT-LEAK-93af7c"
    payload = f"user token is {canary} in this request"
    masked = mask(payload, extra_secrets=[canary])
    assert canary not in masked
    assert "***MASKED***" in masked


def test_canary_secret_masked_from_env_dict():
    canary = "CANARY-ENV-SECRET-11bb22"
    env = {"OPENAI_API_KEY": canary, "FRAMEWORK_ENV": "test"}
    masked = mask_secrets_from_env(f"key used: {canary}", env)
    assert canary not in masked
