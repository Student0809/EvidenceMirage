import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from src.evidence.predicates.provider import configure_deepseek, DEEPSEEK_URL


class ProviderTests(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    def test_missing_key_fails_without_mutation(self):
        with self.assertRaises(ValueError):
            configure_deepseek()
        self.assertNotIn('ANTHROPIC_BASE_URL', os.environ)

    @patch.dict(os.environ, {'ANTHROPIC_AUTH_TOKEN':'wrong-provider', 'ANTHROPIC_BASE_URL':'https://example.org'}, clear=True)
    def test_other_provider_token_not_forwarded(self):
        with self.assertRaises(ValueError):
            configure_deepseek()

    @patch.dict(os.environ, {'DEEPSEEK_API_KEY':'test-secret', 'CLAUDE_CODE_OAUTH_TOKEN':'stale'}, clear=True)
    def test_configuration_and_no_secret_metadata(self):
        metadata=configure_deepseek()
        self.assertEqual(os.environ['ANTHROPIC_BASE_URL'], DEEPSEEK_URL)
        self.assertEqual(os.environ['ANTHROPIC_AUTH_TOKEN'], 'test-secret')
        self.assertEqual(os.environ['CLAUDE_CODE_EFFORT_LEVEL'], 'max')
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', os.environ)
        self.assertNotIn('test-secret', str(metadata))

    @patch.dict(os.environ, {}, clear=True)
    def test_env_file_only_loads_deepseek_key(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'.env';p.write_text('DEEPSEEK_API_KEY=test-file-key\nUNRELATED_SECRET=do-not-load\n')
            configure_deepseek(p)
            self.assertEqual(os.environ['ANTHROPIC_AUTH_TOKEN'],'test-file-key')
            self.assertNotIn('UNRELATED_SECRET',os.environ)

if __name__=='__main__':unittest.main()
