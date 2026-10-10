#!/usr/bin/env python3
"""
Czekanie na napisy YouTube (Bloomroad).

Co robi:
  1. Pyta Notion o odcinki z zaznaczonym polem "Czeka na napisy".
  2. Dla kazdego sprawdza w Supadata, czy YouTube ma juz napisy (po polsku).
  3. Jesli napisy sa: odznacza "Czeka na napisy" i zaznacza "Ponow napisy".
     To jest dokladnie to samo, co klikniecie reczne - Make sam wtedy
     generuje szkic opisu i rolki i wysyla wiadomosci na Slacka.
  4. Jesli napisow jeszcze nie ma: nic nie robi, sprawdzi znowu przy
     nastepnym uruchomieniu (co ok. 30 minut).

Zmienne srodowiskowe:
  NOTION_TOKEN        token integracji Notion (ten sam co przy sortowaniu klipow)
  SUPADATA_API_KEY    klucz API Supadata
  DATA_SOURCE_ID      (opcjonalne) ID bazy odcinkow w Notion
  DRY_RUN             "true" = tylko sprawdza i pisze wynik, nic nie zmienia w Notion
"""

import os
import sys

import requests

NOTION_VERSION = "2025-09-03"
DEFAULT_DATA_SOURCE_ID = "8acda0c0-34ea-4d34-ba8b-66813e207849"
WAIT_PROP = "Czeka na napisy"
RETRY_PROP = "Ponow napisy"
URL_PROP = "URL"
TITLE_PROP = "Nazwa"
SUPADATA_URL = "https://api.supadata.ai/v1/transcript"


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
                        "odcinkow (Notion: ... > Polaczenia).")
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

    def mark_captions_ready(self, page_id):
        props = {WAIT_PROP: {"checkbox": False}, RETRY_PROP: {"checkbox": True}}
        return self._call("PATCH", "/v1/pages/%s" % page_id, json={"properties": props})


def page_title(page):
    prop = page.get("properties", {}).get(TITLE_PROP, {})
    return "".join(t.get("plain_text", "") for t in prop.get("title", [])).strip() or page.get("id", "?")


def page_url(page):
    prop = page.get("properties", {}).get(URL_PROP, {})
    return (prop.get("url") or "").strip()


def captions_ready(video_url, api_key, http=requests):
    """True gdy YouTube ma napisy. False gdy jeszcze ich nie ma.
    Wyjatek tylko przy zlym kluczu (401/403), zeby blad byl widoczny."""
    r = http.get(SUPADATA_URL,
                 params={"url": video_url, "text": "false", "mode": "native", "lang": "pl"},
                 headers={"x-api-key": api_key}, timeout=90)
    if r.status_code in (401, 403):
        raise RuntimeError("Supadata odrzucil klucz API (%s). Sprawdz sekret SUPADATA_API_KEY." % r.status_code)
    if r.status_code != 200:
        return False
    try:
        content = r.json().get("content")
    except ValueError:
        return False
    return bool(content)


def run(notion, api_key, data_source_id, dry_run, http=requests, out=print):
    pages = notion.waiting_pages(data_source_id)
    out("Odcinkow czekajacych na napisy: %d" % len(pages))
    ready = 0
    for page in pages:
        title = page_title(page)
        url = page_url(page)
        if not url:
            out("  %s: brak URL, pomijam" % title)
            continue
        if captions_ready(url, api_key, http=http):
            ready += 1
            if dry_run:
                out("  %s: napisy SA (podglad, nic nie zmieniam)" % title)
            else:
                notion.mark_captions_ready(page["id"])
                out("  %s: napisy SA - uruchamiam opis i rolki" % title)
        else:
            out("  %s: napisow jeszcze nie ma" % title)
    return ready


def main():
    token = os.environ.get("NOTION_TOKEN", "").strip()
    api_key = os.environ.get("SUPADATA_API_KEY", "").strip()
    if not token or not api_key:
        print("Brakuje NOTION_TOKEN lub SUPADATA_API_KEY (sekrety w GitHub).")
        return 1
    data_source_id = os.environ.get("DATA_SOURCE_ID", "").strip() or DEFAULT_DATA_SOURCE_ID
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() == "true"
    run(NotionClient(token), api_key, data_source_id, dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
