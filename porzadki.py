"""
PORZADKI: archiwum odcinkow opublikowanych ponad DNI dni temu (domyslnie 30).
=============================================================================
Uruchamiana raz w tygodniu (poniedzialek) przez GitHub Actions (porzadki.yml).

Dla kazdego klienta z pliku porzadki_klienci.json:
  1. bierze odcinki ze statusem "Opublikowane", ktorych "Data publikacji" jest
     starsza niz DNI dni (odcinek bez daty publikacji zostaje - jest w raporcie),
  2. dla kazdego takiego odcinka:
       a. Dysk: szuka folderu TYLKO tego odcinka - folder z pola "Surowka" albo
          jego podfolder, ktorego nazwa pasuje do nazwy odcinka i ktorego nie
          podaje zaden inny odcinek. Taki folder idzie do kosza Google (30 dni
          na przywrocenie). Wspolnych folderow (surowka kilku odcinkow, assety,
          przebitki) skrypt NIE rusza.
       b. Notion: na dole strony odcinka dopisuje notatke z danymi z panelu
          (URL, daty, status, uwagi...), wiersze z "Logi klipow" ida do kosza
          Notion, a sama strona jest przenoszona na strone "Archiwum odcinkow"
          obok bazy Odcinki (strona archiwum powstaje sama przy pierwszym razie).
Jesli Dysk zwroci blad (np. brak uprawnien), odcinek zostaje w panelu i jest
probowany znowu za tydzien.

Zmienne srodowiskowe:
  NOTION_TOKEN, GOOGLE_TOKEN_JSON  - jak przy sortowaniu klipow
  RESULT_WEBHOOK_URL               - raport na Slacka (tylko gdy cos sie stalo)
  DRY_RUN                          - true (domyslnie) = tylko raport, nic nie zmienia
  DNI                              - po ilu dniach od publikacji archiwizowac (30)
  MAX_PER_RUN                      - maks. odcinkow na klienta w jednym przebiegu (10)
  CLIENTS_FILE                     - domyslnie porzadki_klienci.json
"""

import datetime as dt
import json
import os
import re
import sys

import sortuj_klipy as S

ARCHIVE_TITLE = "Archiwum odcink\u00f3w"
MOVE_VERSION = "2026-03-11"      # endpoint /move wymaga nowej wersji API Notion
FOLDER_RE = re.compile(r"(?:/folders/|[?&]id=)([a-zA-Z0-9_-]{15,})")
SKIP_PROPS = {"logiklipow"}      # relacje do kosza - nie przepisujemy do notatki
MIN_MATCH = 6                    # min. dlugosc nazwy odcinka do dopasowania "zawiera"


# ---------------------------------------------------------------------------
# Wybor odcinkow
# ---------------------------------------------------------------------------

def title_of(page):
    for v in page["properties"].values():
        if v.get("type") == "title":
            return S.plain(v["title"]).strip()
    return ""


def publish_date(page):
    """Data publikacji jako date albo None."""
    for k, v in page["properties"].items():
        if S.norm(k) == "datapublikacji":
            start = ((v.get("date") or {}).get("start") or "")[:10]
            try:
                return dt.date.fromisoformat(start)
            except ValueError:
                return None
    return None


def split_old(pages, days, today):
    """(do archiwum - najstarsze pierwsze, bez daty publikacji)."""
    limit = today - dt.timedelta(days=days)
    old, no_date = [], []
    for p in pages:
        d = publish_date(p)
        if d is None:
            no_date.append(p)
        elif d < limit:
            old.append(p)
    old.sort(key=publish_date)
    return old, no_date


# ---------------------------------------------------------------------------
# Dysk
# ---------------------------------------------------------------------------

def surowka_raw(page):
    for k, v in page["properties"].items():
        if S.norm(k) == "surowka":
            if v.get("type") == "rich_text":
                # linki moga byc tylko w href, nie w tekscie
                parts = []
                for r in v.get("rich_text") or []:
                    parts.append(r.get("plain_text", ""))
                    parts.append((r.get("href") or ""))
                return " ".join(parts)
            return S.prop_text(v)
    return ""


def folder_ids(text):
    seen = []
    for fid in FOLDER_RE.findall(text or ""):
        if fid not in seen:
            seen.append(fid)
    return seen


def name_matches(folder_name, episode):
    f, e = S.norm(folder_name), S.norm(episode)
    if not f or not e:
        return False
    return f == e or (len(e) >= MIN_MATCH and e in f)


def is_no_access(exc):
    status = getattr(getattr(exc, "resp", None), "status", None)
    return str(status) in ("403", "404")


def find_episode_folder(service, linked_ids, episode):
    """Folder tylko tego odcinka (dict z id i name) albo None."""
    for fid in linked_ids:
        try:
            meta = service.files().get(fileId=fid, fields="id, name, mimeType, trashed",
                                       supportsAllDrives=True).execute(num_retries=3)
        except Exception as exc:  # noqa: BLE001 - brak dostepu = nie ruszamy tego folderu
            if is_no_access(exc):
                continue
            raise
        if meta.get("trashed"):
            continue
        if meta.get("mimeType") == S.FOLDER_MIME and name_matches(meta.get("name", ""), episode):
            return meta
        subs = S.list_all(service, f"'{fid}' in parents and mimeType='{S.FOLDER_MIME}' and trashed=false",
                          "id, name")
        for sub in subs:
            if name_matches(sub["name"], episode):
                return sub
    return None


def handle_drive(service_factory, page, episode, other_ids, dry_run):
    """Tekst wyniku. Rzuca wyjatek przy bledzie Dysku."""
    linked = folder_ids(surowka_raw(page))
    if not linked:
        return "brak linku do folderu w Surowce, Dysku nie ruszam"
    service = service_factory()
    folder = find_episode_folder(service, linked, episode)
    if not folder:
        return "nie ma folderu tylko tego odcinka (wspolny folder albo inna nazwa), Dysku nie ruszam"
    if folder["id"] in other_ids:
        return f"folder \"{folder['name']}\" podaje tez inny odcinek, Dysku nie ruszam"
    if not dry_run:
        service.files().update(fileId=folder["id"], body={"trashed": True},
                               supportsAllDrives=True).execute(num_retries=3)
    return f"folder \"{folder['name']}\" do kosza Google"


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

def query_all(notion, database_id):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion._call("POST", f"/v1/databases/{database_id}/query", json=body)
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            return rows
        cursor = data["next_cursor"]


def prop_value(prop):
    t = prop.get("type")
    if t == "date":
        d = prop.get("date") or {}
        return " - ".join(x for x in (d.get("start"), d.get("end")) if x)
    if t == "checkbox":
        return "tak" if prop.get("checkbox") else ""
    if t == "rich_text":
        parts = []
        for r in prop.get("rich_text") or []:
            txt, href = r.get("plain_text", ""), r.get("href")
            parts.append(f"{txt} ({href})" if href and href not in txt else txt)
        return "".join(parts)
    if t == "multi_select":
        return ", ".join(o.get("name", "") for o in prop.get("multi_select") or [])
    if t == "people":
        return ", ".join(p.get("name", "") or "" for p in prop.get("people") or [])
    if t == "files":
        return ", ".join(f.get("name", "") for f in prop.get("files") or [])
    return S.prop_text(prop)


def snapshot_lines(page, today):
    lines = []
    for k, v in page["properties"].items():
        if S.norm(k) in SKIP_PROPS or v.get("type") in ("relation", "rollup", "formula", "title"):
            continue
        val = prop_value(v).strip()
        if val:
            lines.append(f"{k}: {val}")
    head = f"Dane z panelu (zapisane przy archiwizacji {today.strftime('%d.%m.%Y')})"
    return head, lines


def snapshot_blocks(head, lines):
    blocks = [{"object": "block", "type": "heading_3",
               "heading_3": {"rich_text": [{"type": "text", "text": {"content": head}}]}}]
    chunk = ""
    for line in lines:
        piece = line[:1900] + "\n"
        if chunk and len(chunk) + len(piece) > 1900:
            blocks.append(_para(chunk))
            chunk = ""
        chunk += piece
    if chunk:
        blocks.append(_para(chunk))
    return blocks


def _para(text):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text.rstrip()}}]}}


def find_archive(notion, db_id, cache):
    """(parent_id, archive_id or None). Rzuca wyjatek, gdy baza nie lezy na stronie."""
    if db_id in cache:
        return cache[db_id]
    db = notion.get_database(db_id)
    parent = db.get("parent") or {}
    if parent.get("type") != "page_id":
        raise RuntimeError("baza Odcinki nie lezy bezposrednio na stronie, nie wiem gdzie zrobic archiwum")
    pid = parent["page_id"]
    archive_id, cursor = None, None
    while archive_id is None:
        path = f"/v1/blocks/{pid}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        data = notion._call("GET", path)
        for b in data.get("results", []):
            if b.get("type") == "child_page" and (b["child_page"].get("title") or "").strip() == ARCHIVE_TITLE:
                archive_id = b["id"]
                break
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    cache[db_id] = (pid, archive_id)
    return cache[db_id]


def ensure_archive(notion, db_id, cache):
    pid, archive_id = find_archive(notion, db_id, cache)
    if archive_id:
        return archive_id
    body = {"parent": {"page_id": pid},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": ARCHIVE_TITLE}}]}},
            "children": [_para("Odcinki opublikowane ponad miesiac temu. Przenosi je tu automat "
                               "porzadki.py (GitHub, co poniedzialek). Kazdy ma na dole notatke "
                               "z danymi, ktore mial w panelu.")]}
    archive_id = notion._call("POST", "/v1/pages", json=body)["id"]
    cache[db_id] = (pid, archive_id)
    return archive_id


def move_page(notion, page_id, parent_id):
    headers = dict(notion.headers)
    headers["Notion-Version"] = MOVE_VERSION
    r = notion.http.request("POST", f"https://api.notion.com/v1/pages/{page_id}/move", headers=headers,
                            timeout=60, json={"parent": {"type": "page_id", "page_id": parent_id}})
    if r.status_code != 200:
        raise RuntimeError(f"Notion move {r.status_code}: {r.text[:300]}")
    return r.json()


def clip_rows(notion, page):
    by_norm = {S.norm(k): v for k, v in page["properties"].items()}
    ids = [r["id"] for r in (by_norm.get("logiklipow") or {}).get("relation", [])]
    if not ids:
        return []
    first = notion.get_page(ids[0])
    db_id = first["parent"].get("database_id")
    if not db_id:
        return []
    return notion.query_clips(db_id, os.environ.get("CLIPS_RELATION_PROP", "Odcinek"), page["id"])


# ---------------------------------------------------------------------------
# Przebieg
# ---------------------------------------------------------------------------

def clean_client(notion, service_factory, client, days, max_per_run, dry_run, today, cache):
    """Zwraca liste linii raportu dla klienta (pusta = nic do zrobienia)."""
    name, db_id = client["klient"], client["odcinki_db"]
    all_pages = query_all(notion, db_id)
    published = [p for p in all_pages if S.norm(S.prop_text(p["properties"].get("Status"))) == "opublikowane"]
    old, no_date = split_old(published, days, today)
    lines = [f"{name}: {title_of(p) or p['id']}: Opublikowane bez Daty publikacji - zostaje w panelu."
             for p in no_date]
    if not old:
        return lines
    if len(old) > max_per_run:
        lines.append(f"{name}: do archiwum jest {len(old)} odcinkow, biore {max_per_run} najstarszych, "
                     f"reszta za tydzien.")
        old = old[:max_per_run]
    try:
        pid, archive_id = find_archive(notion, db_id, cache)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"{name}: nie moge znalezc miejsca na archiwum: {str(exc)[:200]}")
        return lines
    if not archive_id:
        lines.append(f"{name}: strona \"{ARCHIVE_TITLE}\" jeszcze nie istnieje - powstanie przy pierwszym "
                     f"prawdziwym przebiegu.")
    for page in old:
        episode = title_of(page) or page["id"]
        others = set()
        for p in all_pages:
            if p["id"] != page["id"]:
                others.update(folder_ids(surowka_raw(p)))
        try:
            drive_msg = handle_drive(service_factory, page, episode, others, dry_run)
        except Exception as exc:  # noqa: BLE001 - przy bledzie Dysku odcinek zostaje w panelu
            lines.append(f"{name}: {episode}: ZOSTAJE w panelu, blad Dysku: {str(exc)[:200]}")
            continue
        try:
            rows = clip_rows(notion, page)
            if not dry_run:
                head, snap = snapshot_lines(page, today)
                notion._call("PATCH", f"/v1/blocks/{page['id']}/children",
                             json={"children": snapshot_blocks(head, snap)})
                for row in rows:
                    notion.patch_raw(row["id"], {"archived": True})
                move_page(notion, page["id"], ensure_archive(notion, db_id, cache))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{name}: {episode}: {drive_msg}; Notion nie zadzialal: {str(exc)[:200]}")
            continue
        lines.append(f"{name}: {episode} (publikacja {publish_date(page):%d.%m}): {drive_msg}; "
                     f"strona -> {ARCHIVE_TITLE}, do kosza Notion {len(rows)} wierszy klipow.")
    return lines


def main():
    dry_run = os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes", "tak")
    webhook = os.environ.get("RESULT_WEBHOOK_URL", "").strip()
    days = int(os.environ.get("DNI", "30") or 30)
    max_per_run = int(os.environ.get("MAX_PER_RUN", "10") or 10)
    config = os.environ.get("CLIENTS_FILE", "porzadki_klienci.json")
    notion = S.Notion(os.environ["NOTION_TOKEN"])
    today = dt.date.today()

    drive = {}

    def service_factory():
        if "s" not in drive:
            drive["s"] = S.get_drive_service(os.environ["GOOGLE_TOKEN_JSON"])
        return drive["s"]

    report, code, cache = [], 0, {}
    for client in S.load_clients(config):
        try:
            report.extend(clean_client(notion, service_factory, client, days, max_per_run,
                                       dry_run, today, cache))
        except Exception as exc:  # noqa: BLE001 - jeden klient nie blokuje reszty
            report.append(f"{client['klient']}: BLAD porzadkow: {str(exc)[:200]}")
            code = 1
    if not report:
        print("Nic do posprzatania.")
        return code
    text = ("Porzadki - archiwum odcinkow" + (" (PODGLAD, nic nie zmieniono)" if dry_run else "")
            + ":\n" + "\n".join(report))
    print(text)
    S.notify(webhook, text)
    return code


if __name__ == "__main__":
    sys.exit(main())
