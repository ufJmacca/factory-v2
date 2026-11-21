from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory.codex_delivery import cli_args
from factory.codex_transport import Proxy
from factory.codex_workspace import Workspace
from factory.evidence import Evidence


class ProxyTest(unittest.TestCase):
    def test_secret_stays_out_of_launch_arguments_and_retained_settings(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "device-key"
            key.write_text("test-device-key")
            key.chmod(0o600)
            ca = root / "hub-ca"
            ca.write_bytes(b"test certificate pin")
            store = Evidence(root / "archive")
            proxy = Proxy("https://example.test/v1", key, ca)
            settings = proxy.pin(store)
            env = proxy.environment(store, settings, root)
            launch = cli_args(
                "codex", root, store, {"run_id": "sample", "base_sha": "base", "transport": settings},
                Workspace("sample"), {"model": "gpt-6.2-sol", "reasoning_effort": "medium"},
            )
            self.assertEqual(env["CODEX_HUB_API_KEY"], "test-device-key")
            self.assertNotIn("test-device-key", str(launch))
            self.assertNotIn("test-device-key", str(settings))
            self.assertIn('model_provider="factory-hub"', launch)
            self.assertIn('model_providers.factory-hub.requires_openai_auth=false', launch)
            self.assertNotIn('model_provider="openai"', launch)

    def test_rejects_insecure_proxy_url_and_exposed_key(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            key = root / "key"
            key.write_text("test")
            key.chmod(0o644)
            store = Evidence(root / "archive")
            for url in ("http://example.test/v1", "https://example.test/v1?x=1",
                        "https://user@example.test/v1", "https://example.test/v1"):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    Proxy(url, key, key).pin(store)


if __name__ == "__main__":
    unittest.main()
