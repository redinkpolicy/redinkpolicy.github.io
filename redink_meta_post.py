#!/usr/bin/env python3
"""
Red Ink — Meta (Facebook Page + Instagram) Reel poster via the Graph API.

Why this exists: the browser file-picker upload for video stalls on Buffer/TikTok/X.
The Graph API instead fetches the video from a PUBLIC URL (no file picker), so it works.
This is the autonomous FB/IG video path. Organic posting is free.

Setup: see META-GRAPH-API-SETUP.md. You provide a token in meta-secrets.json; this
script never prints it. Instagram account MUST be a *Business* account (not Creator).

Usage:
    python3 redink_meta_post.py --self-test
        Validates the token and auto-fills page_id + ig_user_id in meta-secrets.json.

    python3 redink_meta_post.py --video-url <PUBLIC_MP4_URL> \
        --caption-file fc/E15.txt --platforms ig,fb
        Publishes one Reel to Instagram and/or Facebook.

    # caption may be given inline instead of a file:
    python3 redink_meta_post.py --video-url <URL> --caption "..." --platforms ig

Requires: requests  (pip install --break-system-packages requests)
"""
import os, sys, json, time, argparse

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run: pip install --break-system-packages requests")

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HERE, "meta-secrets.json")
GRAPH_VERSION = "v21.0"           # stable; v23/v24/v25 also work — bump if Meta asks
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"


# ---------- secrets ----------
def load_secrets():
    # In GitHub Actions (or any CI) the token comes from the META_TOKEN env secret.
    env_tok = os.environ.get("META_TOKEN", "").strip()
    if env_tok:
        return {"access_token": env_tok, "page_id": "", "ig_user_id": ""}
    # Otherwise read the local file (your machine).
    if not os.path.exists(SECRETS):
        sys.exit(f"No META_TOKEN env var and missing {SECRETS}. See META-GRAPH-API-SETUP.md.")
    with open(SECRETS) as f:
        s = json.load(f)
    if not s.get("access_token") or "PASTE_YOUR_TOKEN" in s["access_token"]:
        sys.exit("meta-secrets.json has no real access_token yet.")
    return s

def save_secrets(s):
    with open(SECRETS, "w") as f:
        json.dump(s, f, indent=2)


# ---------- helpers ----------
def _check(r, what):
    """Raise a clean error if a Graph call failed; never echo the token."""
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
            msg = f"{err.get('type')}: {err.get('message')} (code {err.get('code')})"
        except Exception:
            msg = r.text[:300]
        raise RuntimeError(f"[{what}] HTTP {r.status_code} — {msg}")
    return r.json()

def discover_ids(token):
    """Find the Page (id + page-scoped token) and the linked IG Business id."""
    data = _check(requests.get(f"{GRAPH}/me/accounts",
                  params={"access_token": token, "fields": "id,name,access_token"}),
                  "discover pages")
    pages = data.get("data", [])
    if not pages:
        raise RuntimeError("Token can see no Pages. Check scopes/role on the app.")
    page = pages[0]                       # Red Ink Policy is the only Page
    page_id = page["id"]
    page_token = page.get("access_token", token)
    ig = _check(requests.get(f"{GRAPH}/{page_id}",
               params={"access_token": page_token, "fields": "instagram_business_account"}),
               "discover IG account")
    ig_id = (ig.get("instagram_business_account") or {}).get("id")
    return page_id, page_token, ig_id

def page_token(s):
    """Return a Page-scoped token (preferred for Page + IG publishing)."""
    pid, ptok, _ = discover_ids(s["access_token"])
    return pid, ptok


# ---------- Instagram ----------
def post_instagram_reel(s, video_url, caption, ptok):
    ig_id = s.get("ig_user_id")
    if not ig_id:
        raise RuntimeError("ig_user_id not set — run --self-test first.")
    # 1) create the REELS container (Meta fetches the video from video_url)
    c = _check(requests.post(f"{GRAPH}/{ig_id}/media",
        data={"media_type": "REELS", "video_url": video_url,
              "caption": caption, "access_token": ptok}), "IG create container")
    creation_id = c["id"]
    # 2) poll until the container finishes processing
    for _ in range(40):                   # ~up to 6-7 min
        st = _check(requests.get(f"{GRAPH}/{creation_id}",
            params={"fields": "status_code,status", "access_token": ptok}), "IG poll")
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"IG container {code}: {st.get('status')}")
        time.sleep(10)
    else:
        raise RuntimeError("IG container never reached FINISHED (timed out).")
    # 3) publish
    pub = _check(requests.post(f"{GRAPH}/{ig_id}/media_publish",
        data={"creation_id": creation_id, "access_token": ptok}), "IG publish")
    return pub.get("id")


# ---------- Facebook Page ----------
def post_facebook_reel(s, video_url, caption, pid, ptok):
    # 1) start an upload session
    start = _check(requests.post(f"{GRAPH}/{pid}/video_reels",
        data={"upload_phase": "start", "access_token": ptok}), "FB reel start")
    video_id = start["video_id"]
    upload_url = start["upload_url"]
    # 2) hand Meta the hosted URL to pull from (no local file streamed)
    up = requests.post(upload_url,
        headers={"Authorization": f"OAuth {ptok}", "file_url": video_url})
    _check(up, "FB reel upload-by-url")
    # 3) finish + publish
    fin = _check(requests.post(f"{GRAPH}/{pid}/video_reels",
        params={"access_token": ptok},
        data={"upload_phase": "finish", "video_id": video_id,
              "video_state": "PUBLISHED", "description": caption}), "FB reel finish")
    return video_id if fin.get("success") else None


# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="validate token, auto-fill page_id + ig_user_id")
    ap.add_argument("--video-url", help="PUBLIC https URL to the .mp4")
    ap.add_argument("--caption")
    ap.add_argument("--caption-file")
    ap.add_argument("--platforms", default="ig,fb", help="comma list: ig,fb")
    a = ap.parse_args()
    s = load_secrets()

    if a.self_test:
        pid, ptok, ig_id = discover_ids(s["access_token"])
        s["page_id"], s["ig_user_id"] = pid, ig_id
        save_secrets(s)
        print(f"OK. Page id: {pid}   IG business id: {ig_id or 'NONE — is IG a *Business* acct linked to the Page?'}")
        print("IDs saved to meta-secrets.json. Ready to post." if ig_id else
              "Fix the IG link, then re-run --self-test.")
        return

    if not a.video_url:
        sys.exit("--video-url is required (a PUBLIC https URL to the mp4).")
    caption = a.caption
    if a.caption_file:
        caption = open(a.caption_file).read().strip()
    if not caption:
        sys.exit("Provide --caption or --caption-file.")

    plats = [p.strip() for p in a.platforms.split(",") if p.strip()]
    pid, ptok = page_token(s)
    if "ig" in plats:
        try:
            print("IG reel published, media id:", post_instagram_reel(s, a.video_url, caption, ptok))
        except Exception as e:
            print("IG FAILED:", e)
    if "fb" in plats:
        try:
            print("FB reel published, video id:", post_facebook_reel(s, a.video_url, caption, pid, ptok))
        except Exception as e:
            print("FB FAILED:", e)


if __name__ == "__main__":
    main()
