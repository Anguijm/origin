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
import re
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


# Manual overrides for cases where Spotify's search returns nothing or the
# wrong album. Keys are (norm(artist), norm(album)). Value is either a
# single Spotify album ID or a list of IDs to concatenate in order (used
# for Minor Threat's "Complete Discography", which Spotify only carries as
# its component EPs).
MANUAL_OVERRIDES: dict[tuple[str, str], str | list[str]] = {
    ("xrayspex", "germfreeadolescents"):           "6O0hDvYYCjEoOzJdXkiaXa",
    ("theslits", "cut"):                           "6ppPT0aXOtsAlG1QQVB9E0",  # Deluxe Edition
    ("thecramps", "songsthelordtaughtus"):         "6S9rbimtTmC0v6UBWqSpay",
    ("operationivy", "energy"):                    "2Rv1kIWFeIYeq8kAtdhY6m",  # listed as self-titled
    ("publicimageltd", "metalbox"):                "5votrp9PY49suw8xnXqyrm",  # US "Second Edition"
    ("cockneyrejects", "greatesthitsvol1"):        "78VfmOQmefLVhXmkm44925",
    ("crass", "thefeedingofthe5000"):              "7BLObcmZzTBqDRaPRpOOWc",  # Crassical Collection
    ("sickofitall", "bloodsweatandnotears"):       "4toIJJY78eKd9ZLw267mN0",
    ("newyorkdolls", "newyorkdolls"):              "2xbTV0Awe4Qm5caUVuPbMr",  # 1973 debut
    ("themisfits", "misfits"):                     "51tAz06EJxwhsk8uNfWxBo",  # Static Age (closest canonical)
    ("minorthreat", "completediscography"): [
        "6Sty6rLnMTXFjKxKUZEfmy",  # First Two Seven Inches (1981)
        "6wPX4FdHqmn0aHZ84WUCW5",  # Out of Step (1984)
        "5JXGvBK6woRyyxOXro1mW2",  # Salad Days (1985)
    ],
}

# Albums confirmed absent from Spotify; treat as intentional skips rather
# than search failures.
UNAVAILABLE_ON_SPOTIFY: set[tuple[str, str]] = {
    ("frightwig", "catfarmfaboo"),
    ("thefaith", "faithvoidsplit"),
}

# Matches that look suspicious to the heuristic but are actually correct
# given Spotify's titling or the user's edition preferences. Suppress the
# flag for these.
KNOWN_OK_MATCHES: set[tuple[str, str]] = {
    ("fugazi", "repeater"),              # CD release is "Repeater + 3 Songs"
    ("themodernlovers", "themodernlovers"),  # listed as "Jonathan Richman & The Modern Lovers"
    ("flipper", "genericflipper"),       # full title "Album - Generic Flipper"
    ("fear", "therecord"),               # only the 2023 remaster exists on Spotify
    ("idles", "brutalism"),              # Five Years of Brutalism = anniversary, fits deluxe pref
}


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


def get_album(token: str, album_id: str) -> dict:
    return api("GET", f"/albums/{album_id}", token)


def resolve_albums(token: str, artist: str, album: str) -> list[dict] | None:
    """Resolve a CSV row to one or more Spotify albums, honoring manual
    overrides for known-bad search results. Returns None for unmatched and
    a sentinel empty list for albums confirmed unavailable on Spotify."""
    key = (_norm(artist), _norm(album))

    if key in UNAVAILABLE_ON_SPOTIFY:
        return []

    if key in MANUAL_OVERRIDES:
        ids = MANUAL_OVERRIDES[key]
        if isinstance(ids, str):
            ids = [ids]
        return [get_album(token, i) for i in ids]

    # Spotify currently caps /search limit at 10.
    q = f'album:"{album}" artist:"{artist}"'
    r = api("GET", "/search", token, params={"q": q, "type": "album", "limit": 10})
    pick = pick_album(r.get("albums", {}).get("items", []), artist, album)
    if pick:
        return [pick]

    q2 = f"{album} {artist}"
    r = api("GET", "/search", token, params={"q": q2, "type": "album", "limit": 10})
    pick = pick_album(r.get("albums", {}).get("items", []), artist, album)
    return [pick] if pick else None


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


def create_playlist(token: str) -> dict:
    # /users/{id}/playlists now returns 403 for app-issued tokens;
    # /me/playlists works with the playlist-modify-private scope.
    return api("POST", "/me/playlists", token, body={
        "name": PLAYLIST_NAME,
        "public": False,
        "description": PLAYLIST_DESC,
    })


def add_tracks(token: str, playlist_id: str, uris: list[str]) -> None:
    # Spotify's March 2026 migration renamed POST /playlists/{id}/tracks
    # to POST /playlists/{id}/items for new dev-mode apps. The old path
    # returns 403 Forbidden. The body shape is unchanged.
    for i in range(0, len(uris), 100):
        batch = uris[i:i + 100]
        api("POST", f"/playlists/{playlist_id}/items", token, body={"uris": batch})


_EDITION_KEYWORDS = re.compile(
    r"\b(remaster(?:ed)?|deluxe|expanded|anniversary|edition|definitive|collector'?s|version|box\s*set|reissue)\b",
    re.IGNORECASE,
)
_PAREN_OR_BRACKET = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def _strip_edition(name: str) -> str:
    cleaned = _PAREN_OR_BRACKET.sub("", name)
    cleaned = _EDITION_KEYWORDS.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" -;,:")


def is_suspicious_match(input_name: str, matched_name: str) -> bool:
    """True if the chosen album's title diverges from input beyond a simple
    edition suffix (remaster/deluxe/etc), so a human should eyeball it."""
    norm_in = _norm(input_name)
    norm_out = _norm(_strip_edition(matched_name))
    if not norm_out or norm_in == norm_out:
        return False
    if norm_out.startswith(norm_in) and len(norm_out) <= max(len(norm_in) + 3, len(norm_in) * 1.3):
        return False
    if norm_in in norm_out and len(norm_out) <= len(norm_in) * 1.3:
        return False
    return True


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
    duplicates: list[dict] = []
    flagged: list[dict] = []
    skipped_unavailable: list[dict] = []
    all_uris: list[str] = []
    seen_album_ids: set[str] = set()

    for i, row in enumerate(rows, 1):
        key = (_norm(row["artist"]), _norm(row["album"]))
        try:
            albums = resolve_albums(token, row["artist"], row["album"])
        except Exception as exc:
            print(f"  [{i:>3}/{len(rows)}] ERROR resolving {row['artist']} - {row['album']}: {exc}",
                  file=sys.stderr)
            unmatched.append({**row, "reason": f"resolve_error: {exc}"})
            continue

        if albums is None:
            print(f"  [{i:>3}/{len(rows)}] UNMATCHED: {row['artist']} - {row['album']}",
                  file=sys.stderr)
            unmatched.append({**row, "reason": "no_match"})
            continue

        if not albums:
            print(f"  [{i:>3}/{len(rows)}] SKIPPED (not on Spotify): {row['artist']} - {row['album']}",
                  file=sys.stderr)
            skipped_unavailable.append({**row, "reason": "unavailable_on_spotify"})
            continue

        for j, album in enumerate(albums):
            if album["id"] in seen_album_ids:
                print(
                    f"  [{i:>3}/{len(rows)}] DUPLICATE (already added): "
                    f"{row['artist']} - {album['name']}",
                    file=sys.stderr,
                )
                duplicates.append({
                    "input": row,
                    "spotify_album_id": album["id"],
                    "spotify_album_name": album["name"],
                })
                continue
            seen_album_ids.add(album["id"])

            tracks = get_album_tracks(token, album["id"])
            uris = [t["uri"] for t in tracks if t.get("uri", "").startswith("spotify:track:")]
            all_uris.extend(uris)

            suspicious = is_suspicious_match(row["album"], album["name"]) and key not in KNOWN_OK_MATCHES
            entry = {
                "input": row,
                "spotify_album_name": album["name"],
                "spotify_album_id": album["id"],
                "spotify_album_url": album["external_urls"]["spotify"],
                "release_date": album.get("release_date"),
                "total_tracks": len(uris),
                "suspicious": suspicious,
            }
            matched.append(entry)
            if suspicious:
                flagged.append(entry)

            flag = " [FLAGGED]" if suspicious else ""
            multi = f" (part {j + 1}/{len(albums)})" if len(albums) > 1 else ""
            print(
                f"  [{i:>3}/{len(rows)}] {row['artist']} - {album['name']} "
                f"({album.get('release_date', '?')}, {len(uris)} tracks){multi}{flag}",
                file=sys.stderr,
            )

    print(f"\nCreating playlist '{PLAYLIST_NAME}' (private)...", file=sys.stderr)
    pl = create_playlist(token)
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
        "albums_duplicate": len(duplicates),
        "albums_skipped_unavailable": len(skipped_unavailable),
        "albums_flagged": len(flagged),
        "matched_albums": matched,
        "unmatched_albums": unmatched,
        "duplicate_albums": duplicates,
        "skipped_unavailable": skipped_unavailable,
        "flagged_albums": flagged,
    }, indent=2, ensure_ascii=False))

    print("\nDone.")
    print(f"  Playlist URL       : {playlist_url}")
    print(f"  Tracks added       : {len(all_uris)}")
    print(f"  Albums matched     : {len(matched)}  (from {len(rows)} CSV rows)")
    print(f"  Albums unmatched   : {len(unmatched)}")
    print(f"  Albums duplicate   : {len(duplicates)}  (same album referenced twice in CSV)")
    print(f"  Albums unavailable : {len(skipped_unavailable)}  (confirmed not on Spotify)")
    print(f"  Albums flagged     : {len(flagged)}  (title diverges beyond edition suffix)")
    if unmatched:
        print("\nUnmatched:")
        for u in unmatched:
            print(f"    - {u['artist']} - {u['album']}  [{u.get('reason', '')}]")
    if skipped_unavailable:
        print("\nSkipped (not on Spotify):")
        for u in skipped_unavailable:
            print(f"    - {u['artist']} - {u['album']}")
    if flagged:
        print("\nFlagged for review:")
        for f_ in flagged:
            print(f"    - input:   {f_['input']['artist']} - {f_['input']['album']}")
            print(f"      picked:  {f_['spotify_album_name']}  ({f_['spotify_album_url']})")
    if duplicates:
        print("\nDuplicates (CSV row pointed at an album already added):")
        for d in duplicates:
            print(f"    - {d['input']['artist']} - {d['input']['album']}  -> already added as {d['spotify_album_name']}")
    print(f"\nFull report        : {REPORT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
