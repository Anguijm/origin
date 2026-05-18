#!/usr/bin/env python3
"""Build a Spotify playlist containing every track from a list of albums.

Reads csv/rolling-stone-100-punk.csv (columns: title, artist, album).
For each row, finds the album on Spotify, fetches its full tracklist,
and adds every track in album-internal order to a new private playlist
named "Rolling Stone 100 Greatest Punk Albums".

Edition tie-breaking when multiple Spotify releases match an album:
remaster > deluxe / expanded / anniversary > plain studio. Live editions
are picked only when no non-live edition matches the same title.

Auth: requires env vars
  SPOTIFY_CLIENT_ID
  SPOTIFY_CLIENT_SECRET
  SPOTIFY_REFRESH_TOKEN

Output: writes outputs/report.json with per-album resolution, the full
unmatched list, the new playlist's URL, and the total track count.
"""

from __future__ import annotations

import base64
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"
PLAYLIST_NAME = "Rolling Stone 100 Greatest Punk Albums"
PLAYLIST_DESC = (
    "Every track from every album on Rolling Stone's 100 Greatest Punk Albums "
    "list, in ranking order."
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "csv" / "rolling-stone-100-punk.csv"
REPORT_PATH = REPO_ROOT / "outputs" / "report.json"


def require_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing env var: {name}")
    return v


def get_access_token() -> str:
    cid = require_env("SPOTIFY_CLIENT_ID")
    csec = require_env("SPOTIFY_CLIENT_SECRET")
    rtok = require_env("SPOTIFY_REFRESH_TOKEN")
    creds = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": rtok,
    }).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())["access_token"]


def api(method: str, path: str, token: str, *, params=None, body=None):
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")

    backoff = 1.0
    for _ in range(6):
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry = float(e.headers.get("Retry-After", backoff))
                time.sleep(retry + 1)
                continue
            if 500 <= e.code < 600:
                time.sleep(backoff)
                backoff *= 2
                continue
            err = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code} on {method} {path}: {err}") from None
    raise RuntimeError(f"Exhausted retries: {method} {path}")


def _norm(s: str) -> str:
    return "".join(c.lower() for c in s if c.isalnum())


def edition_score(album_name: str) -> int:
    n = album_name.lower()
    if "remaster" in n:
        return 3
    if any(k in n for k in ("deluxe", "expanded", "anniversary")):
        return 2
    return 1


def is_live_edition(album_name: str) -> bool:
    n = album_name.lower()
    return (
        "(live" in n
        or "[live" in n
        or "live at" in n
        or " live in " in n
        or n.endswith(" live")
        or " - live" in n
    )


def pick_album(candidates: list[dict], target_artist: str, target_album: str) -> dict | None:
    t_artist = _norm(target_artist)
    t_album = _norm(target_album)

    matched = []
    for a in candidates:
        if a.get("album_type") == "compilation":
            continue
        if not any(
            _norm(ar["name"]) == t_artist or t_artist in _norm(ar["name"]) or _norm(ar["name"]) in t_artist
            for ar in a.get("artists", [])
        ):
            continue
        cand_name = _norm(a["name"])
        if t_album in cand_name or cand_name.startswith(t_album):
            matched.append(a)

    if not matched:
        return None

    non_live = [a for a in matched if not is_live_edition(a["name"])]
    pool = non_live if non_live else matched

    pool.sort(
        key=lambda a: (
            edition_score(a["name"]),
            a.get("total_tracks", 0),
            a.get("release_date", ""),
        ),
        reverse=True,
    )
    return pool[0]


def search_album(token: str, artist: str, album: str) -> dict | None:
    q = f'album:"{album}" artist:"{artist}"'
    r = api("GET", "/search", token, params={"q": q, "type": "album", "limit": 20})
    pick = pick_album(r.get("albums", {}).get("items", []), artist, album)
    if pick:
        return pick

    q2 = f"{album} {artist}"
    r = api("GET", "/search", token, params={"q": q2, "type": "album", "limit": 30})
    return pick_album(r.get("albums", {}).get("items", []), artist, album)


def get_album_tracks(token: str, album_id: str) -> list[dict]:
    out = []
    offset = 0
    while True:
        r = api("GET", f"/albums/{album_id}/tracks", token,
                params={"limit": 50, "offset": offset})
        out.extend(r["items"])
        if not r.get("next"):
            return out
        offset += 50


def create_playlist(token: str, user_id: str) -> dict:
    return api("POST", f"/users/{user_id}/playlists", token, body={
        "name": PLAYLIST_NAME,
        "public": False,
        "description": PLAYLIST_DESC,
    })


def add_tracks(token: str, playlist_id: str, uris: list[str]) -> None:
    for i in range(0, len(uris), 100):
        batch = uris[i:i + 100]
        api("POST", f"/playlists/{playlist_id}/tracks", token, body={"uris": batch})


def load_rows() -> list[dict]:
    with open(CSV_PATH, newline="") as f:
        return [
            {"title": r["title"].strip(), "artist": r["artist"].strip(), "album": r["album"].strip()}
            for r in csv.DictReader(f)
            if r.get("artist") and r.get("album")
        ]


def main() -> None:
    token = get_access_token()
    me = api("GET", "/me", token)
    user_id = me["id"]
    print(f"Authenticated as Spotify user: {user_id}", file=sys.stderr)

    rows = load_rows()
    print(f"Loaded {len(rows)} album rows from CSV", file=sys.stderr)

    matched: list[dict] = []
    unmatched: list[dict] = []
    all_uris: list[str] = []

    for i, row in enumerate(rows, 1):
        try:
            album = search_album(token, row["artist"], row["album"])
        except Exception as exc:
            print(f"  [{i:>3}/{len(rows)}] ERROR searching {row['artist']} - {row['album']}: {exc}",
                  file=sys.stderr)
            unmatched.append({**row, "reason": f"search_error: {exc}"})
            continue

        if not album:
            print(f"  [{i:>3}/{len(rows)}] UNMATCHED: {row['artist']} - {row['album']}",
                  file=sys.stderr)
            unmatched.append({**row, "reason": "no_match"})
            continue

        tracks = get_album_tracks(token, album["id"])
        uris = [t["uri"] for t in tracks if t.get("uri", "").startswith("spotify:track:")]
        all_uris.extend(uris)

        matched.append({
            "input": row,
            "spotify_album_name": album["name"],
            "spotify_album_id": album["id"],
            "spotify_album_url": album["external_urls"]["spotify"],
            "release_date": album.get("release_date"),
            "total_tracks": len(uris),
        })
        print(
            f"  [{i:>3}/{len(rows)}] {row['artist']} - {album['name']} "
            f"({album.get('release_date', '?')}, {len(uris)} tracks)",
            file=sys.stderr,
        )

    print(f"\nCreating playlist '{PLAYLIST_NAME}' (private)...", file=sys.stderr)
    pl = create_playlist(token, user_id)
    playlist_url = pl["external_urls"]["spotify"]

    batches = (len(all_uris) + 99) // 100
    print(f"Adding {len(all_uris)} tracks in {batches} batches of ≤100...", file=sys.stderr)
    add_tracks(token, pl["id"], all_uris)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps({
        "playlist_name": PLAYLIST_NAME,
        "playlist_url": playlist_url,
        "playlist_id": pl["id"],
        "user_id": user_id,
        "total_tracks_added": len(all_uris),
        "albums_matched": len(matched),
        "albums_unmatched": len(unmatched),
        "matched_albums": matched,
        "unmatched_albums": unmatched,
    }, indent=2, ensure_ascii=False))

    print("\nDone.")
    print(f"  Playlist URL    : {playlist_url}")
    print(f"  Tracks added    : {len(all_uris)}")
    print(f"  Albums matched  : {len(matched)} / {len(rows)}")
    print(f"  Albums unmatched: {len(unmatched)}")
    for u in unmatched:
        print(f"    - {u['artist']} - {u['album']} ({u.get('reason', '')})")
    print(f"  Full report     : {REPORT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
