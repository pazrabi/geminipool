"""Single sign-on cookie shared with DroidHub (https://molokohub.site/sso/login).

The cookie value is `<user>|<expires_epoch>|<hmac_sha256_hex>`, signed over `<user>|<expires_epoch>` with a secret both
sides read from their own environment file. Without the secret nobody can make one, so it is safe to check on any host.
(A plain `X-Sso-User` header would not be: the old sslip.io address reaches the pool without the SSO gate in front.)
"""

from __future__ import annotations

import hashlib
import hmac
import time
from http.cookies import CookieError, SimpleCookie


def verify_cookie(value: str, secret: str, now: float | None = None) -> str | None:
    """The user the cookie was issued to, or None for a bad signature, an expired cookie or an empty secret."""
    try:
        if not secret or not value:
            return None
        user, exp, sig = value.rsplit("|", 2)
        good = hmac.new(secret.encode(), f"{user}|{exp}".encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig.encode(), good.encode()) and float(exp) > (time.time() if now is None else now):
            return user or None
    except Exception:
        pass
    return None


def user_from_cookie_header(header: str, name: str, secret: str, now: float | None = None) -> str | None:
    try:
        jar = SimpleCookie()
        jar.load(header or "")
    except CookieError:
        return None
    morsel = jar.get(name)
    return verify_cookie(morsel.value, secret, now) if morsel else None
