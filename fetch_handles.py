"""
One-time helper: save the accounts that @VoltaFamily follows into handles.txt.

Usage:
    python fetch_handles.py              # defaults to VoltaFamily
    python fetch_handles.py SomeAccount
"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
TWITTERAPI_BASE = "https://api.twitterapi.io"


def main():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    account = sys.argv[1].lstrip("@") if len(sys.argv) > 1 else "VoltaFamily"

    handles, cursor = [], ""
    while True:
        params = {"userName": account}
        if cursor:
            params["cursor"] = cursor
        url = f"{TWITTERAPI_BASE}/twitter/user/followings?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"X-API-Key": cfg["twitterapi_key"]})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for u in data.get("followings") or []:
            if u.get("userName"):
                handles.append(u["userName"])
        if not data.get("has_next_page") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]

    (ROOT / "handles.txt").write_text("\n".join(handles) + "\n", encoding="utf-8")
    print(f"Saved {len(handles)} accounts followed by @{account} to handles.txt")


if __name__ == "__main__":
    main()
