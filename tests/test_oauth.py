"""OAuth flow used by Claude custom connectors, exercised over real HTTP on a local port."""

import base64
import hashlib
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import oauth as oauthmod  # noqa: E402
import server  # noqa: E402
from oauth import OAuth  # noqa: E402
from pool import Pool  # noqa: E402

POOL_TOKEN = "t" * 40
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"


async def _never(**kw):  # the OAuth tests never run an agent
    raise RuntimeError("not used")


def pkce():
    verifier = base64.urlsafe_b64encode(hashlib.sha256(b"seed").digest()).rstrip(b"=").decode() + "x" * 10
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OAuthHTTPTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.pool = Pool(tmp / "data", runner=_never, accounts=[{"id": "acc1", "role": "developer"}])
        self.oauth = OAuth(self.pool.db_path, POOL_TOKEN)
        server.configure(self.pool, POOL_TOKEN, self.oauth)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        out = (resp.status, dict(resp.getheaders()), data)
        conn.close()
        return out

    def jpost(self, path, obj, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        status, hdrs, data = self.call("POST", path, json.dumps(obj), h)
        return status, hdrs, json.loads(data or b"{}")

    def fpost(self, path, fields, headers=None):
        h = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
        return self.call("POST", path, urllib.parse.urlencode(fields), h)

    def register(self, **kw):
        status, _, body = self.jpost("/register", {"client_name": "Claude", "redirect_uris": [CLAUDE_CB], **kw})
        self.assertEqual(status, 201, body)
        return body

    def authorize(self, client, challenge, token=POOL_TOKEN, redirect=CLAUDE_CB, state="st8"):
        return self.fpost("/authorize", {
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256", "pool_token": token,
        })

    def get_code(self, client, challenge):
        status, hdrs, _ = self.authorize(client, challenge)
        self.assertEqual(status, 302)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(hdrs["Location"]).query)
        self.assertEqual(query["state"], ["st8"])
        return query["code"][0]

    def exchange(self, client, code, verifier, redirect=CLAUDE_CB):
        return self.fpost("/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                     "client_id": client["client_id"], "code_verifier": verifier})

    # ---- discovery ---------------------------------------------------------

    def test_unauthenticated_mcp_gets_401_with_resource_metadata(self):
        for method in ("POST", "GET"):
            status, hdrs, _ = self.call(method, "/mcp", "{}" if method == "POST" else None)
            self.assertEqual(status, 401)
            self.assertIn("resource_metadata=", hdrs["WWW-Authenticate"])
            self.assertIn("/.well-known/oauth-protected-resource", hdrs["WWW-Authenticate"])

    def test_metadata_documents(self):
        status, _, data = self.call("GET", "/.well-known/oauth-protected-resource")
        meta = json.loads(data)
        self.assertEqual(status, 200)
        self.assertTrue(meta["resource"].endswith("/mcp"))  # must equal the MCP URL exactly
        base = meta["authorization_servers"][0]
        _, _, data = self.call("GET", "/.well-known/oauth-authorization-server")
        asm = json.loads(data)
        self.assertEqual(asm["issuer"], base)
        self.assertEqual(asm["code_challenge_methods_supported"], ["S256"])
        self.assertIn("none", asm["token_endpoint_auth_methods_supported"])
        self.assertIn("offline_access", asm["scopes_supported"])
        for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
            self.assertTrue(asm[key].startswith(base))

    # ---- registration --------------------------------------------------------

    def test_registration_only_accepts_claude_callbacks(self):
        client = self.register()
        self.assertNotIn("client_secret", client)  # public client
        for bad in (["https://evil.example/api/mcp/auth_callback"], ["https://claude.ai.evil.example/x"],
                    ["http://claude.ai/api/mcp/auth_callback"], ["https://claude.ai/other"], [], "nope"):
            status, _, body = self.jpost("/register", {"redirect_uris": bad})
            self.assertEqual((status, body["error"]), (400, "invalid_redirect_uri"), bad)

    def test_claude_code_loopback_callback_is_accepted(self):
        client = self.register(redirect_uris=["http://localhost:3118/callback"])
        self.assertTrue(oauthmod.redirect_matches(client["redirect_uris"], "http://localhost:51234/callback"))
        self.assertFalse(oauthmod.redirect_matches(client["redirect_uris"], "http://127.0.0.1:51234/callback"))
        self.assertFalse(oauthmod.redirect_matches(client["redirect_uris"], "http://localhost:51234/steal"))

    # ---- full flow -----------------------------------------------------------

    def test_full_flow_then_mcp_works_with_the_access_token(self):
        client = self.register()
        verifier, challenge = pkce()
        status, _, page = self.call("GET", "/authorize?" + urllib.parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": CLAUDE_CB, "response_type": "code",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "st8"}))
        self.assertEqual(status, 200)
        self.assertIn(b"claude.ai", page)  # the redirect host is shown to the owner
        status, _, data = self.exchange(client, self.get_code(client, challenge), verifier)
        tokens = json.loads(data)
        self.assertEqual((status, tokens["token_type"]), (200, "Bearer"))
        self.assertTrue(tokens["refresh_token"])
        rpc = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        status, _, data = self.call("POST", "/mcp", rpc, {"Authorization": "Bearer " + tokens["access_token"]})
        self.assertEqual(status, 200)
        self.assertIn("submit_task", data.decode())
        status, _, _ = self.call("POST", "/mcp", rpc, {"Authorization": "Bearer " + POOL_TOKEN})  # Claude Code path
        self.assertEqual(status, 200)

    def test_access_token_does_not_open_the_dashboard_api(self):
        client = self.register()
        verifier, challenge = pkce()
        _, _, data = self.exchange(client, self.get_code(client, challenge), verifier)
        access = json.loads(data)["access_token"]
        status, _, _ = self.call("GET", "/api/overview", headers={"Authorization": "Bearer " + access})
        self.assertEqual(status, 401)

    def test_drain_endpoint_needs_the_pool_token_and_toggles(self):
        auth = {"Authorization": "Bearer " + POOL_TOKEN, "Content-Type": "application/json"}
        status, _, _ = self.call("POST", "/api/drain", '{"drain": true}', {"Content-Type": "application/json"})
        self.assertEqual(status, 401)
        status, _, data = self.call("POST", "/api/drain", '{"drain": true}', auth)
        self.assertEqual((status, json.loads(data)["safe_to_deploy"]), (200, True))
        _, _, data = self.call("GET", "/api/overview", headers=auth)
        self.assertTrue(json.loads(data)["draining"])
        status, _, _ = self.call("POST", "/api/drain", '{"drain": "yes"}', auth)
        self.assertEqual(status, 400)
        status, _, data = self.call("POST", "/api/drain", '{"drain": false}', auth)
        self.assertFalse(json.loads(data)["draining"])

    def test_wrong_owner_token_issues_no_code(self):
        client = self.register()
        status, hdrs, body = self.authorize(client, pkce()[1], token="wrong")
        self.assertEqual(status, 200)
        self.assertNotIn("Location", hdrs)
        self.assertIn("Неверный токен".encode(), body)

    def test_unregistered_redirect_is_never_followed(self):
        client = self.register()
        status, hdrs, _ = self.authorize(client, pkce()[1], redirect="https://evil.example/cb")
        self.assertEqual(status, 400)
        self.assertNotIn("Location", hdrs)

    def test_code_works_once(self):
        client = self.register()
        verifier, challenge = pkce()
        code = self.get_code(client, challenge)
        self.assertEqual(self.exchange(client, code, verifier)[0], 200)
        status, _, data = self.exchange(client, code, verifier)
        self.assertEqual((status, json.loads(data)["error"]), (400, "invalid_grant"))

    def test_wrong_pkce_verifier_is_rejected(self):
        client = self.register()
        _, challenge = pkce()
        status, _, data = self.exchange(client, self.get_code(client, challenge), "y" * 50)
        self.assertEqual((status, json.loads(data)["error"]), (400, "invalid_grant"))

    def test_unknown_client_gets_invalid_client(self):
        status, _, data = self.fpost("/token", {"grant_type": "authorization_code", "code": "x", "client_id": "nope",
                                                "redirect_uri": CLAUDE_CB, "code_verifier": "v" * 50})
        self.assertEqual((status, json.loads(data)["error"]), (401, "invalid_client"))

    def test_refresh_rotates_the_refresh_token(self):
        client = self.register()
        verifier, challenge = pkce()
        _, _, data = self.exchange(client, self.get_code(client, challenge), verifier)
        first = json.loads(data)
        status, _, data = self.fpost("/token", {"grant_type": "refresh_token", "client_id": client["client_id"],
                                                "refresh_token": first["refresh_token"]})
        second = json.loads(data)
        self.assertEqual(status, 200)
        self.assertNotEqual(second["refresh_token"], first["refresh_token"])
        status, _, data = self.fpost("/token", {"grant_type": "refresh_token", "client_id": client["client_id"],
                                                "refresh_token": first["refresh_token"]})
        self.assertEqual((status, json.loads(data)["error"]), (400, "invalid_grant"))

    def test_expired_access_token_is_rejected(self):
        client = self.register()
        verifier, challenge = pkce()
        _, _, data = self.exchange(client, self.get_code(client, challenge), verifier)
        access = json.loads(data)["access_token"]
        self.assertTrue(self.oauth.validate_access(access))
        conn = sqlite3.connect(self.pool.db_path)
        conn.execute("UPDATE oauth_tokens SET expires_at = 1 WHERE kind = 'access'")
        conn.commit()
        conn.close()
        self.assertFalse(self.oauth.validate_access(access))
        status, _, _ = self.call("POST", "/mcp", "{}", {"Authorization": "Bearer " + access})
        self.assertEqual(status, 401)

    def test_repeated_wrong_owner_tokens_lock_the_consent_page(self):
        client = self.register()
        _, challenge = pkce()
        for _ in range(oauthmod.MAX_FAILURES):
            self.authorize(client, challenge, token="wrong")
        status, _, _ = self.authorize(client, challenge, token=POOL_TOKEN)  # even the right token waits
        self.assertEqual(status, 429)

    def test_confidential_client_needs_its_secret(self):
        client = self.register(token_endpoint_auth_method="client_secret_post")
        verifier, challenge = pkce()
        code = self.get_code(client, challenge)
        status, _, _ = self.exchange(client, code, verifier)  # no secret sent
        self.assertEqual(status, 401)
        code = self.get_code(client, challenge)
        status, _, _ = self.fpost("/token", {"grant_type": "authorization_code", "code": code, "redirect_uri": CLAUDE_CB,
                                             "client_id": client["client_id"], "client_secret": client["client_secret"],
                                             "code_verifier": verifier})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
