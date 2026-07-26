"""Credential redaction tests use synthetic values only."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from astrbot_plugin_mi_fitness_health.utils.privacy import redact_error


class PrivacyTest(unittest.TestCase):
    def test_sensitive_llm_authorization_defaults_to_false_in_schema(self) -> None:
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIs(schema["allow_health_data_to_llm"]["default"], False)
        self.assertEqual(
            schema["context_decision_provider_id"]["_special"], "select_provider"
        )
        self.assertEqual(schema["context_decision_provider_id"]["default"], "")

    def test_common_xiaomi_and_provider_secrets_are_redacted(self) -> None:
        synthetic_secret = "synthetic-secret-value"
        samples = (
            f"serviceToken={synthetic_secret}",
            f'"accessToken":"{synthetic_secret}"',
            f"Authorization: Bearer {synthetic_secret}",
            f"Set-Cookie: serviceToken={synthetic_secret}",
            f"Cookie: cUserId=synthetic-id; passToken={synthetic_secret}",
            f"_nonce={synthetic_secret}&signature={synthetic_secret}",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = redact_error(sample)
                self.assertNotIn(synthetic_secret, redacted)
                self.assertNotIn("synthetic-id", redacted)

    def test_api_keys_and_common_provider_token_formats_are_redacted(self) -> None:
        samples = (
            "api_key=sk-synthetic-secret-value",
            '"apiKey":"AIzaSyntheticSecretValue123456"',
            "x-api-key: ghp_syntheticSecretValue1234567890",
            "client_secret=synthetic-secret-value",
            "private_key=synthetic-private-key-value",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = redact_error(sample)
                self.assertNotIn("synthetic", redacted.lower())

    def test_urls_controls_and_newlines_never_reach_status_text(self) -> None:
        redacted = redact_error(
            "failure\nhttps://example.invalid/path?serviceToken=synthetic\r\nnext"
        )
        self.assertNotIn("https://", redacted)
        self.assertNotIn("\n", redacted)
        self.assertNotIn("\r", redacted)
