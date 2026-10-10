#!/usr/bin/env python3
"""
Pobieranie napisow YouTube (Bloomroad) - za darmo, przez oficjalne API YouTube.

Co robi (uruchamiane co godzine przez GitHub Actions):
  1. Pyta Notion o odcinki z zaznaczonym polem "Czeka na napisy".
  2. Dla kazdego bierze ID filmu z pola URL i pyta YouTube o napisy po polsku.
  3. Jesli napisy sa: zapisuje je w bazie "Napisy YouTube (automat)" w Notion
     (jeden wiersz = jeden film, nazwa = ID filmu), potem odznacza
     "Czeka na napisy" i zaznacza "Ponow napisy". Dalej pracuje Make.
  4. Jesli napisow jeszcze nie ma: nic nie robi, sprawdzi za godzine.

Zmienne srodowiskowe:
  NOTION_TOKEN          token integracji Notion
  YOUTUBE_TOKEN_JSON    klucz Google z uprawnieniem youtube.force-ssl
  DATA_SOURCE_ID        (opcjonalne) baza odcinkow
  NAPISY_DATA_SOURCE_ID (opcjonalne) baza z napisami
  DRY_RUN               "true" = tylko sprawdza, nic nie zapisuje
"""

import json
import os
import re
import sys

import requests

NOTION_VERSION = "2025-09-03"
DEFAULT_DATA_SOURCE_ID = "8acda0c0-34ea-4d34-ba8b-66813e207849"
DEFAULT_NAPISY_DATA_SOURCE_ID = "5266ccfa-636c-42bc-b6d3-aa334ef74050"
WAIT_PROP = "Czeka na napisy"
RETRY_PROP = "Ponow napisy"
URL_PROP = "URL"
TITLE_PROP = "Nazwa"
TEXT_PROP = "Tekst"
CHUNK = 1900          # limit Notion to 2000 znakow na kawalek tekstu
MAX_CHUNKS = 100      # limit Notion: 100 kawalkow w jednym polu


def video_id_from_url(url):
    m = re.search(r"(?:youtu\.be/|v=|/shorts/|/live/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


def srt_to_lines(srt):
    """SRT -> linie 'sekunda tekst' (tak jak wczesniej dawal Supadata)."""
    cues = []
    for block in re.split(r"\n\s*\n", srt.replace("\r\n", "\n").strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        for i, l in enumerate(lines):
            m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->", l.strip())
            if m:
                start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                text = re.sub(r"\s+", " ", " ".join(lines[i + 1:])).strip()
                if text:
                    cues.append((start, text))
                break
    cues.sort(key=lambda c: c[0])
    out, prev = [], None
    for start, text in cues:
        if text == prev:
            continue
        out.append("%d %s" % (start, text))
        prev = text
    return out


def chunk_lines(lines, size=CHUNK):
    chunks, cur = [], ""
    for line in lines:
        piece = line + "\n"
        if cur and len(cur) + len(piece) > size:
            chunks.append(cur)
            cur = ""
        while len(piece) > size:          # bardzo dluga linia - tnij
            chunks.append(piece[:size])
            piece = piece[size:]
        cur += piece
    if cur:
        chunks.append(cur)
    return chunks


class YouTubeClient:
    def __init__(self, token_info, http=requests):
        self.http = http
        self.info = token_info
        self.access = None

    def _token(self):
        if self.access:
            return self.access
        r = self.http.post(self.info.get("token_uri", "https://oauth2.googleapis.com/token"), data={
            "client_id": self.info["client_id"],
            "client_secret": self.info["client_secret"],
            "refresh_token": self.info["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=60)
        if r.status_code != 200:
            raise RuntimeError("Nie udalo sie odswiezyc klucza YouTube (%s): %s. "
                               "Wygeneruj nowy klucz (google-klucz-youtube.html) i podmien sekret "
                               "YOUTUBE_TOKEN_JSON." % (r.status_code, r.text[:200]))
        self.access = r.json()["access_token"]
        return self.access

    def srt_for_video(self, video_id):
        """Tekst SRT albo None, gdy YouTube nie ma jeszcze polskich napisow."""
        h = {"Authorization": "Bearer " + self._token()}
        r = self.http.get("https://www.googleapis.com/youtube/v3/captions",
                          params={"part": "snippet", "videoId": video_id}, headers=h, timeout=60)
        if r.status_code != 200:
            raise RuntimeError("YouTube captions.list %s: %s" % (r.status_code, r.text[:300]))
        tracks = []
        for it in r.json().get("items", []):
            sn = it.get("snippet", {})
            if (sn.get("language") or "").lower().startswith("pl") and not sn.get("isDraft"):
                tracks.append(it)
        if not tracks:
            return None
        tracks.sort(key=lambda t: 0 if t["snippet"].get("trackKind") == "standard" else 1)
        d = self.http.get("https://www.googleapis.com/youtube/v3/captions/" + tracks[0]["id"],
                          params={"tfmt": "srt"}, headers=h, timeout=120)
        if d.status_code != 200 or not d.text.strip():
            return None
        return d.text


class NotionClient:
    def __init__(self, token, http=requests):
        self.http = http
        self.headers = {
            "Authorization": "Bearer " + token,
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _call(self, method, path, **kw):
        r = self.http.request(method, "https://api.notion.com" + path,
                              headers=self.headers, timeout=60, **kw)
        if r.status_code != 200:
            hint = ""
            if r.status_code in (401, 403, 404):
                hint = (" Sprawdz token oraz czy integracja ma dostep do bazy "
                        "(Notion: ... > Polaczenia).")
            raise RuntimeError("Notion %s: %s.%s" % (r.status_code, r.text[:300], hint))
        return r.json()

    def waiting_pages(self, data_source_id):
        pages, cursor = [], None
        while True:
            body = {"page_size": 100,
                    "filter": {"property": WAIT_PROP, "checkbox": {"equals": True}}}
            if cursor:
                body["start_cursor"] = cursor
            data = self._call("POST", "/v1/data_sources/%s/query" % data_source_id, json=body)
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")

    def find_row(self, napisy_ds, video_id):
        body = {"page_size": 1,
                "filter": {"property": TITLE_PROP, "title": {"equals": video_id}}}
        data = self._call("POST", "/v1/data_sources/%s/query" % napisy_ds, json=body)
        res = data.get("results", [])
        return res[0]["id"] if res else None

    def create_row(self, napisy_ds, video_id, chunks):
        props = {
            TITLE_PROP: {"title": [{"text": {"content": video_id}}]},
            TEXT_PROP: {"rich_text": [{"text": {"content": c}} for c in chunks]},
        }
        body = {"parent": {"type": "data_source_id", "data_source_id": napisy_ds},
                "properties": props}
        return self._call("POST", "/v1/pages", json=body)["id"]

    def mark_ready(self, page_id):
        props = {WAIT_PROP: {"checkbox": False}, RETRY_PROP: {"checkbox": True}}
        return self._call("PATCH", "/v1/pages/%s" % page_id, json={"properties": props})


def page_title(page):
    prop = page.get("properties", {}).get(TITLE_PROP, {})
    return "".join(t.get("plain_text", "") for t in prop.get("title", [])).strip() or page.get("id", "?")


def page_url(page):
    prop = page.get("properties", {}).get(URL_PROP, {})
    return (prop.get("url") or "").strip()


def run(notion, yt, data_source_id, napisy_ds, dry_run, out=print):
    pages = notion.waiting_pages(data_source_id)
    out("Odcinkow czekajacych na napisy: %d" % len(pages))
    ready = 0
    for page in pages:
        title = page_title(page)
        vid = video_id_from_url(page_url(page))
        if not vid:
            out("  %s: brak poprawnego URL, pomijam" % title)
            continue
        have_row = notion.find_row(napisy_ds, vid)
        if have_row:
            srt = "juz zapisane"
        else:
            srt = yt.srt_for_video(vid)
            if not srt:
                out("  %s: napisow jeszcze nie ma" % title)
                continue
        ready += 1
        if dry_run:
            out("  %s: napisy SA (%s) - podglad, nic nie zmieniam" % (title, srt if not have_row else "juz zapisane"))
            if not have_row:
                out("     pierwsze linie: " + " | ".join(srt_to_lines(srt)[:3]))
            continue
        if not have_row:
            lines = srt_to_lines(srt)
            chunks = chunk_lines(lines)
            if not chunks or len(chunks) > MAX_CHUNKS:
                out("  %s: napisy puste albo za dlugie (%d kawalkow), pomijam" % (title, len(chunks)))
                ready -= 1
                continue
            notion.create_row(napisy_ds, vid, chunks)
        notion.mark_ready(page["id"])
        out("  %s: napisy zapisane - Make zrobi reszte" % title)
    return ready


def main():
    token = os.environ.get("NOTION_TOKEN", "").strip()
    yt_raw = os.environ.get("YOUTUBE_TOKEN_JSON", "").strip()
    if not token or not yt_raw:
        print("Brakuje NOTION_TOKEN lub YOUTUBE_TOKEN_JSON (sekrety w GitHub).")
        return 1
    ds = os.environ.get("DATA_SOURCE_ID", "").strip() or DEFAULT_DATA_SOURCE_ID
    nds = os.environ.get("NAPISY_DATA_SOURCE_ID", "").strip() or DEFAULT_NAPISY_DATA_SOURCE_ID
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() == "true"
    run(NotionClient(token), YouTubeClient(json.loads(yt_raw)), ds, nds, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
