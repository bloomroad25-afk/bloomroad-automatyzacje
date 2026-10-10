#!/usr/bin/env python3
"""TEST: czy YouTube oddaje napisy (takze automatyczne) przez API.
Nic nie zmienia - tylko czyta. Zmienne: YOUTUBE_TOKEN_JSON, VIDEO_ID."""
import json
import os
import sys

import requests


def get_access_token(info):
    r = requests.post(info.get("token_uri", "https://oauth2.googleapis.com/token"), data={
        "client_id": info["client_id"],
        "client_secret": info["client_secret"],
        "refresh_token": info["refresh_token"],
        "grant_type": "refresh_token",
    }, timeout=60)
    if r.status_code != 200:
        print("Nie udalo sie odswiezyc klucza: %s %s" % (r.status_code, r.text[:300]))
        sys.exit(1)
    return r.json()["access_token"]


def main():
    raw = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
    video_id = os.environ.get("VIDEO_ID", "").strip()
    if not raw or not video_id:
        print("Brakuje YOUTUBE_TOKEN_JSON lub VIDEO_ID.")
        return 1
    token = get_access_token(json.loads(raw))
    h = {"Authorization": "Bearer " + token}

    r = requests.get("https://www.googleapis.com/youtube/v3/captions",
                     params={"part": "snippet", "videoId": video_id}, headers=h, timeout=60)
    print("captions.list ->", r.status_code)
    if r.status_code != 200:
        print(r.text[:600])
        return 1
    items = r.json().get("items", [])
    print("Liczba sciezek napisow:", len(items))
    for it in items:
        sn = it.get("snippet", {})
        print("  id=%s jezyk=%s rodzaj=%s status=%s szkic=%s" % (
            it.get("id"), sn.get("language"), sn.get("trackKind"), sn.get("status"), sn.get("isDraft")))
    if not items:
        print("YouTube nie ma jeszcze zadnych napisow dla tego filmu.")
        return 0

    pick = None
    for it in items:
        if (it.get("snippet", {}).get("language") or "").startswith("pl"):
            pick = it
            break
    pick = pick or items[0]
    d = requests.get("https://www.googleapis.com/youtube/v3/captions/" + pick["id"],
                     params={"tfmt": "srt"}, headers=h, timeout=120)
    print("captions.download ->", d.status_code)
    print(d.text[:700])
    return 0 if d.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
