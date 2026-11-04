"""
Like and repost from your own X account through the official X API (OAuth 1.0a user context).
Keys come from your X developer app (Read and write permission): API key/secret + access token/secret.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.x.com/2"


def _quote(s):
    return urllib.parse.quote(str(s), safe="~")


def _auth_header(keys, method, url):
    """OAuth 1.0a HMAC-SHA1 header. JSON bodies are not part of the signature, so only URL params are signed."""
    oauth = {
        "oauth_consumer_key": keys["x_api_key"],
        "oauth_nonce": secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": keys["x_access_token"],
        "oauth_version": "1.0",
    }
    base_url, _, query = url.partition("?")
    params = dict(oauth, **dict(urllib.parse.parse_qsl(query)))
    param_str = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(params.items()))
    base = "&".join([method.upper(), _quote(base_url), _quote(param_str)])
    key = f"{_quote(keys['x_api_secret'])}&{_quote(keys['x_access_secret'])}"
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_quote(k)}="{_quote(v)}"' for k, v in sorted(oauth.items()))


def _call(keys, method, path, body=None):
    url = API + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": _auth_header(keys, method, url), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError(f"X API {e.code}: {detail}") from None


def configured(cfg):
    return all(str(cfg.get(k) or "").strip() and not str(cfg.get(k)).startswith("PUT_")
               for k in ("x_api_key", "x_api_secret", "x_access_token", "x_access_secret"))


_ME = {}


def my_user_id(keys):
    if "id" not in _ME:
        _ME["id"] = _call(keys, "GET", "/users/me")["data"]["id"]
    return _ME["id"]


def like(keys, tweet_id):
    return _call(keys, "POST", f"/users/{my_user_id(keys)}/likes", {"tweet_id": str(tweet_id)})


def repost(keys, tweet_id):
    return _call(keys, "POST", f"/users/{my_user_id(keys)}/retweets", {"tweet_id": str(tweet_id)})
