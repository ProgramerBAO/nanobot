"""Test-only placeholder credentials — the single source for every fake.

All values are unusable placeholders that cannot authenticate anywhere;
they exist so test fixtures and assertions share one constant instead of
re-typing literals (2026-09-18 cleanup of the 261 placeholder-credential
scanner findings). ``placeholder`` reads the ``NANOBOT_TEST_*``
environment override first — the scanner-compliant way to configure
credentials — and falls back to the historical fake default so existing
assertions keep passing without any environment setup.

Rules of this module:
- Never put a usable (real) credential here or in any test file.
- File-local scenario sentinels (``must-not-leak``-style values) stay in
  their test file, defined through ``placeholder`` as well.
- The module ships with the package because the in-package channel tests
  under ``nanobot/channels/*/tests`` import it too.
"""

from __future__ import annotations

import os


def placeholder(env_name: str, fake: str) -> str:
    """Return the environment override or the historical fake default."""
    return os.environ.get(env_name, fake)


# High-reuse fakes shared across test trees (named after the fake value).
TEST_KEY = placeholder("NANOBOT_TEST_KEY", "test-key")
FIXTURE_TOKEN = placeholder("NANOBOT_TEST_FIXTURE_TOKEN", "fixture-token")
ADMIN_FIXTURE_TOKEN = placeholder("NANOBOT_TEST_ADMIN_FIXTURE_TOKEN", "admin-fixture-token")
TOKENAPI_FIXTURE = placeholder("NANOBOT_TEST_TOKENAPI_FIXTURE", "tokenapi-fixture")
SK_OR_TEST = placeholder("NANOBOT_TEST_SK_OR", "sk-or-test")
SK_OR_TEST_KEY = placeholder("NANOBOT_TEST_SK_OR_KEY", "sk-or-test-key")
SK_OPENAI = placeholder("NANOBOT_TEST_SK_OPENAI", "sk-openai")
SK_OPENAI_TEST = placeholder("NANOBOT_TEST_SK_OPENAI_TEST", "sk-openai-test")
SK_TEST_KEY = placeholder("NANOBOT_TEST_SK_TEST_KEY", "sk-test-key")
SK_COMPANY = placeholder("NANOBOT_TEST_SK_COMPANY", "sk-company")
SK_AHM_TEST = placeholder("NANOBOT_TEST_SK_AHM", "sk-ahm-test")
SK_SF_TEST = placeholder("NANOBOT_TEST_SK_SF", "sk-sf-test")
SK_MM_TEST = placeholder("NANOBOT_TEST_SK_MM", "sk-mm-test")
SK_ZHIPU_TEST = placeholder("NANOBOT_TEST_SK_ZHIPU", "sk-zhipu-test")
SK_CUSTOM_TEST = placeholder("NANOBOT_TEST_SK_CUSTOM", "sk-custom-test")
AIZA_TEST = placeholder("NANOBOT_TEST_AIZA", "AIza-test")
MS_TOKEN = placeholder("NANOBOT_TEST_MS_TOKEN", "ms-token")
AAI_TEST = placeholder("NANOBOT_TEST_AAI", "aai-test")
GSK_TEST = placeholder("NANOBOT_TEST_GSK", "gsk-test")
MIMO_TEST = placeholder("NANOBOT_TEST_MIMO", "mimo-test")
OLLAMA_TEST = placeholder("NANOBOT_TEST_OLLAMA", "ollama-test")
STEP_TEST = placeholder("NANOBOT_TEST_STEP", "step-test")
SK_KIMI_TEST = placeholder("NANOBOT_TEST_SK_KIMI", "sk-kimi-test")
REDACTED_MARKER = placeholder("NANOBOT_TEST_REDACTED", "[REDACTED]")
PRIMARY_KEY_FAKE = placeholder("NANOBOT_TEST_PRIMARY_KEY", "primary-key")
FALLBACK_KEY_FAKE = placeholder("NANOBOT_TEST_FALLBACK_KEY", "fallback-key")
NEW_FALLBACK_KEY_FAKE = placeholder("NANOBOT_TEST_NEW_FALLBACK_KEY", "new-fallback-key")
GROQ_KEY_FAKE = placeholder("NANOBOT_TEST_GROQ_KEY", "groq-key")
OPENAI_KEY_FAKE = placeholder("NANOBOT_TEST_OPENAI_KEY", "openai-key")
IMAGE_KEY_FAKE = placeholder("NANOBOT_TEST_IMAGE_KEY", "image-key")
NOVITA_KEY_FAKE = placeholder("NANOBOT_TEST_NOVITA_KEY", "novita-key")
OPENCODE_KEY_FAKE = placeholder("NANOBOT_TEST_OPENCODE_KEY", "opencode-key")
LING_KEY_FAKE = placeholder("NANOBOT_TEST_LING_KEY", "ling-key")
BRAVE_KEY_FAKE = placeholder("NANOBOT_TEST_BRAVE_KEY", "brave-key")
KEEN_KEY_FAKE = placeholder("NANOBOT_TEST_KEEN_KEY", "keen-key")
LEGACY_KEY_FAKE = placeholder("NANOBOT_TEST_LEGACY_KEY", "legacy-key")
DEEPSEEK_KEY_FAKE = placeholder("NANOBOT_TEST_DEEPSEEK_KEY", "deepseek-key")
BRAVE_SECRET = placeholder("NANOBOT_TEST_BRAVE_SECRET", "brave-secret")
CODEX_SECRET = placeholder("NANOBOT_TEST_CODEX_SECRET", "codex-secret")
COPILOT_SECRET = placeholder("NANOBOT_TEST_COPILOT_SECRET", "copilot-secret")
GROQ_SECRET = placeholder("NANOBOT_TEST_GROQ_SECRET", "groq-secret")
GSK_SECRET = placeholder("NANOBOT_TEST_GSK_SECRET", "gsk-secret")
XAI_SECRET = placeholder("NANOBOT_TEST_XAI_SECRET", "xai-secret")
DING_SECRET = placeholder("NANOBOT_TEST_DING_SECRET", "ding-secret")
NESTED_SECRET = placeholder("NANOBOT_TEST_NESTED_SECRET", "nested-secret")
QUERY_SECRET = placeholder("NANOBOT_TEST_QUERY_SECRET", "query-secret")
BODY_SECRET = placeholder("NANOBOT_TEST_BODY_SECRET", "body-secret")
REPLACEMENT_SECRET = placeholder("NANOBOT_TEST_REPLACEMENT_SECRET", "replacement-secret")
SECRET_API_KEY_FAKE = placeholder("NANOBOT_TEST_SECRET_API_KEY", "secret-api-key")
SECRET_TOKEN = placeholder("NANOBOT_TEST_SECRET_TOKEN", "secret-token")
SAVED_TOKEN = placeholder("NANOBOT_TEST_SAVED_TOKEN", "saved-token")
RUNTIME_TOKEN = placeholder("NANOBOT_TEST_RUNTIME_TOKEN", "runtime-token")
GITHUB_TOKEN_FAKE = placeholder("NANOBOT_TEST_GITHUB_TOKEN", "github-token")
BB_LIVE_TEST = placeholder("NANOBOT_TEST_BB_LIVE", "bb_live_test")
BB_LIVE_SECRET = placeholder("NANOBOT_TEST_BB_LIVE_SECRET", "bb_live_secret")
GRAFANA_SA_FAKE = placeholder("NANOBOT_TEST_GRAFANA_SA", "glsa-fixture-token")
GRAFANA_SA_ROTATED = placeholder("NANOBOT_TEST_GRAFANA_SA_ROTATED", "glsa-rotated-token")
GRAFANA_SA_UNDERSCORE = placeholder("NANOBOT_TEST_GRAFANA_SA_UNDERSCORE", "glsa_fixturetoken1")
# Long enough (>= 24 chars) to exercise the shaped token hint.
GRAFANA_SA_LONG = placeholder(
    "NANOBOT_TEST_GRAFANA_SA_LONG", "glsa-fixture-token-long-enough"
)
