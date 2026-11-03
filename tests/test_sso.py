"""Single sign-on: the dashboard gets the pool token only for a correctly signed, unexpired cookie."""

import hashlib
import hmac
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "app"))

import server  # noqa: E402
import sso  # noqa: E402
from pool import Pool  # noqa: E402
from test_pool import FakeAgent  # noqa: E402

TOKEN = "t" * 40
SECRET = "shared-secret-for-tests"


def cookie(user="owner", exp=None, secret=SECRET):
    exp = str(int(time.time() + 3600)) if exp is None else str(exp)
    sig = hmac.new(secret.encode(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
    return f"{user}|{exp}|{sig}"


class SsoTests(unittest.TestCase):
    def setUp(self):
        old = {k: os.environ.get(k) for k in ("POOL_SSO_SECRET", "POOL_SSO_COOKIE", "POOL_SSO_LOGOUT_URL")}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        os.environ["POOL_SSO_SECRET"] = SECRET
        os.environ["POOL_SSO_LOGOUT_URL"] = "https://molokohub.site/sso/logout"
        os.environ.pop("POOL_SSO_COOKIE", None)
        tmp = Path(tempfile.mkdtemp())
        pool = Pool(tmp / "data", runner=FakeAgent(), allow_local_repos=True, accounts=[{"id": "acc1", "role": "developer"}])
        server.configure(pool, TOKEN, None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", path, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, body, r

    def test_dashboard_can_be_framed_only_by_molokohub(self):
        status, body, r = self.get("/")
        self.assertEqual(status, 200)
        self.assertEqual(r.getheader("Content-Security-Policy"), "frame-ancestors 'self' https://molokohub.site")
        self.assertIsNone(r.getheader("X-Frame-Options"))
        self.assertIn(b"pool.embed", body)

    def test_dashboard_assets_are_served(self):
        for path, ctype in (("/dashboard.css", "text/css"), ("/dashboard.js", "text/javascript")):
            status, body, r = self.get(path)
            self.assertEqual(status, 200)
            self.assertIn(ctype, r.getheader("Content-Type"))
            self.assertGreater(len(body), 1000)

    def session(self, value, **extra):
        return self.get("/api/sso/session", {"Cookie": f"molokohub_sso={value}", **extra})

    def test_valid_cookie_gives_the_token_and_is_not_cached(self):
        status, body, resp = self.session(cookie())
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["token"], data["user"]), (TOKEN, "owner"))
        self.assertEqual(data["logout_url"], "https://molokohub.site/sso/logout")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")

    def test_bad_cookies_get_nothing(self):
        for value in (cookie(secret="other"), cookie(exp=int(time.time()) - 5), "x|1|2", "", cookie().rsplit("|", 1)[0],
                      cookie(user="owner2").replace("owner2", "owner")):
            status, body, _ = self.session(value)
            self.assertEqual(status, 401, value)
            self.assertNotIn(TOKEN.encode(), body)

    def test_no_cookie_no_secret_and_cross_site_are_refused(self):
        self.assertEqual(self.get("/api/sso/session")[0], 401)
        self.assertEqual(self.session(cookie(), **{"Sec-Fetch-Site": "cross-site"})[0], 401)
        os.environ["POOL_SSO_SECRET"] = ""  # SSO not configured on this server
        self.assertEqual(self.session(cookie(secret=""))[0], 401)

    def test_a_forged_header_does_nothing_and_the_cookie_opens_nothing_else(self):
        self.assertEqual(self.get("/api/sso/session", {"X-Sso-User": "owner"})[0], 401)
        self.assertEqual(self.get("/api/overview", {"Cookie": f"molokohub_sso={cookie()}"})[0], 401)
        self.assertEqual(self.get("/api/overview", {"X-Sso-User": "owner"})[0], 401)

    def test_cookie_helpers(self):
        now = 1000.0
        self.assertEqual(sso.verify_cookie(cookie(exp=2000), SECRET, now), "owner")
        self.assertIsNone(sso.verify_cookie(cookie(exp=900), SECRET, now))
        self.assertIsNone(sso.verify_cookie("garbage", SECRET, now))
        self.assertIsNone(sso.user_from_cookie_header("a=b; ; ;;=", "molokohub_sso", SECRET))
        self.assertEqual(sso.user_from_cookie_header(f"a=b; molokohub_sso={cookie(exp=2000)}", "molokohub_sso", SECRET, now), "owner")


if __name__ == "__main__":
    unittest.main()
