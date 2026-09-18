/**
 * Test-only placeholder credentials — the single source for the WebUI suite.
 *
 * Every value is an unusable fake that cannot authenticate anywhere; the
 * NANOBOT_TEST_* environment overrides exist for special runs only.
 * Mirrors nanobot/testing/credentials.py (2026-09-19 cleanup).
 */

export const SK_OR_TEST = process.env.NANOBOT_TEST_SK_OR ?? "sk-or-test";
export const SK_COMPANY = process.env.NANOBOT_TEST_SK_COMPANY ?? "sk-company";
export const SECRET_TOKEN = process.env.NANOBOT_TEST_SECRET_TOKEN ?? "secret-token";
export const BB_LIVE_TEST = process.env.NANOBOT_TEST_BB_LIVE ?? "bb_live_test";
