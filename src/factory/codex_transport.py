from dataclasses import dataclass
import json
import os
from pathlib import Path
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

from factory.evidence import Conflict, Evidence


KEY_NAME = "CODEX_HUB_API_KEY"
CA_NAME = "CODEX_CA_CERTIFICATE"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Proxy:
    url: str
    key_file: Path
    ca_file: Path

    def key(self) -> str:
        stat = self.key_file.stat()
        if self.key_file.is_symlink() or not self.key_file.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise Conflict("proxy key must be in a private file owned by the runner")
        key = self.key_file.read_text().strip()
        if not 0 < len(key) <= 256 or any(not 32 < ord(char) < 127 for char in key):
            raise Conflict("invalid proxy key")
        return key

    def pin(self, store: Evidence) -> dict:
        url = urlsplit(self.url)
        if (url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None
                or url.path != "/v1" or url.query or url.fragment):
            raise Conflict("proxy URL must be an HTTPS /v1 endpoint")
        self.key()
        return {
            "kind": "cli-proxy", "url": self.url, "ca_digest": store.blob(self.ca_file.read_bytes()),
            "auth_mode": "chatgpt-oauth-via-cli-proxy", "identity": "factory-delivery",
        }

    def environment(self, store: Evidence, pinned: dict, directory: Path) -> dict:
        if self.pin(store) != pinned:
            raise Conflict("proxy transport changed after admission")
        ca = directory / "factory-hub-ca.pem"
        ca.write_bytes(store.read_blob(pinned["ca_digest"]))
        return {KEY_NAME: self.key(), CA_NAME: str(ca)}


def proxy_model(pinned: dict, env: dict, model: str) -> str | None:
    tls = ssl.create_default_context(cafile=env[CA_NAME])
    opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=tls))
    request = Request(pinned["url"] + "/models", headers={"Authorization": "Bearer " + env[KEY_NAME]})
    try:
        with opener.open(request, timeout=10) as response:
            models = json.loads(response.read(512_000))["data"]
    except HTTPError as error:
        return {401: "proxy_key_rejected", 403: "proxy_key_rejected", 429: "quota_exhausted"}.get(
            error.code, "model_catalog_unavailable",
        )
    except (URLError, OSError, ValueError, KeyError):
        return "model_catalog_unavailable"
    return None if any(item["id"] == model for item in models) else "model_unavailable"
