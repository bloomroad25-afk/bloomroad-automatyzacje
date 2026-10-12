"""
PORZADKI: w bazie Odcinki zostaja max 2 opublikowane odcinki na klienta.
=========================================================================
Uruchamiana raz w tygodniu (poniedzialek) przez GitHub Actions (porzadki.yml).

Dla kazdego klienta z pliku porzadki_klienci.json:
  1. bierze odcinki ze statusem "Opublikowane", sortuje od najnowszego
     (po "Data publikacji", a gdy jej brak - po dacie utworzenia strony),
  2. zostawia LIMIT najnowszych (domyslnie 2),
  3. kazdy starszy odcinek:
       a. folder odcinka na Dysku (podfolder o nazwie odcinka w folderze z pola
          "Surowka") dostaje nazwe "DO USUNIECIA - <nazwa odcinka>",
       b. strona odcinka i jej wiersze z "Logi klipow" ida do kosza Notion
          (do przywrocenia przez 30 dni).
Pliki na Dysku NIE sa ruszane ani kasowane - zmienia sie tylko nazwa folderu.
Jesli zmiana nazwy folderu sie nie uda (np. brak dostepu), odcinek zostaje w
Notion, a w raporcie jest powod.

Zmienne srodowiskowe:
  NOTION_TOKEN, GOOGLE_TOKEN_JSON  - jak przy sortowaniu klipow
  RESULT_WEBHOOK_URL               - raport na Slacka (tylko gdy cos sie stalo)
  DRY_RUN                          - true (domyslnie) = tylko raport, nic nie zmienia
  LIMIT                            - ile opublikowanych zostawic (domyslnie 2)
  MAX_PER_RUN                      - maks. odcinkow do usuniecia na klienta w jednym
                                     przebiegu (domyslnie 5, zabezpieczenie)
  CLIENTS_FILE                     - domyslnie porzadki_klienci.json
"""

import json
import os
import sys

import sortuj_klipy as S

PREFIX = "DO USUNIECIA - "


def title_of(page):
    for v in page["properties"].values():
        if v.get("type") == "title":
            return S.plain(v["title"]).strip()
    return ""


def publish_key(page):
    """Data publikacji (tekst ISO); gdy jej brak, data utworzenia strony."""
    prop = None
    for k, v in page["properties"].items():
        if S.norm(k) == "datapublikacji":
            prop = v
            break
    date = ((prop or {}).get("date") or {}).get("start") or ""
    return date or page.get("created_time", "")


def to_remove(pages, limit):
    """Strony starsze niz LIMIT najnowszych (najstarsze pierwsze)."""
    ordered = sorted(pages, key=publish_key, reverse=True)
    return list(reversed(ordered[limit:]))


def surowka_of(page):
    for k, v in page["properties"].items():
        if S.norm(k) == "surowka":
            return S.prop_text(v).strip()
    return ""


def rename_episode_folder(service, root_id, episode, dry_run):
    """Zwraca tekst opisujacy wynik. Rzuca wyjatek przy bledzie Dysku."""
    wanted = {S.clean_string(episode), S.clean_string(S.ascii_fold(episode))}
    folders = S.list_all(service, f"'{root_id}' in parents and mimeType='{S.FOLDER_MIME}' and trashed=false", "id, name")
    for f in folders:
        if f["name"].startswith(PREFIX):
            continue
        if S.clean_string(f["name"]) in wanted:
            new_name = PREFIX + f["name"]
            if not dry_run:
                service.files().update(fileId=f["id"], body={"name": new_name},
                                       supportsAllDrives=True).execute(num_retries=3)
            return f"folder -> {new_name}"
    return "folderu odcinka nie znaleziono na Dysku"


def clip_rows(notion, page):
    """Wiersze Logow klipow przypisane do odcinka."""
    by_norm = {S.norm(k): v for k, v in page["properties"].items()}
    ids = [r["id"] for r in (by_norm.get("logiklipow") or {}).get("relation", [])]
    if not ids:
        return []
    first = notion.get_page(ids[0])
    db_id = first["parent"].get("database_id")
    if not db_id:
        return []
    return notion.query_clips(db_id, os.environ.get("CLIPS_RELATION_PROP", "Odcinek"), page["id"])


def archive(notion, page_id):
    notion.patch_raw(page_id, {"archived": True})


def clean_client(notion, service_factory, client, limit, max_per_run, dry_run):
    """Zwraca liste linii raportu dla klienta (pusta = nic do zrobienia)."""
    name, db_id = client["klient"], client["odcinki_db"]
    pages = notion.query_published(db_id)
    old = to_remove(pages, limit)
    lines = []
    if len(old) > max_per_run:
        lines.append(f"{name}: do usuniecia jest {len(old)} odcinkow, biore tylko {max_per_run} najstarszych "
                     f"(zabezpieczenie MAX_PER_RUN), reszta w kolejnych przebiegach.")
        old = old[:max_per_run]
    for page in old:
        episode = title_of(page) or page["id"]
        drive_msg, drive_error = "brak linku Surowka, folderu nie ruszam", None
        root_id = S.extract_folder_id(surowka_of(page))
        if root_id:
            try:
                drive_msg = rename_episode_folder(service_factory(), root_id, episode, dry_run)
            except Exception as exc:  # noqa: BLE001 - nie usuwamy z Notion, gdy Dysk zawiodl
                drive_error = str(exc)[:200]
        if drive_error:
            lines.append(f"{name}: {episode}: ZOSTAJE w Notion, blad Dysku: {drive_error}")
            continue
        n_rows = 0
        try:
            rows = clip_rows(notion, page)
            n_rows = len(rows)
            if not dry_run:
                for row in rows:
                    archive(notion, row["id"])
                archive(notion, page["id"])
        except Exception as exc:  # noqa: BLE001
            lines.append(f"{name}: {episode}: {drive_msg}, ale Notion nie zadzialal: {str(exc)[:200]}")
            continue
        lines.append(f"{name}: {episode}: {drive_msg}; do kosza Notion: odcinek + {n_rows} wierszy klipow.")
    return lines


def main():
    dry_run = os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes", "tak")
    webhook = os.environ.get("RESULT_WEBHOOK_URL", "").strip()
    limit = int(os.environ.get("LIMIT", "2") or 2)
    max_per_run = int(os.environ.get("MAX_PER_RUN", "5") or 5)
    config = os.environ.get("CLIENTS_FILE", "porzadki_klienci.json")
    notion = S.Notion(os.environ["NOTION_TOKEN"])

    cache = {}

    def service_factory():
        if "s" not in cache:
            cache["s"] = S.get_drive_service(os.environ["GOOGLE_TOKEN_JSON"])
        return cache["s"]

    report, code = [], 0
    for client in S.load_clients(config):
        try:
            report.extend(clean_client(notion, service_factory, client, limit, max_per_run, dry_run))
        except Exception as exc:  # noqa: BLE001 - jeden klient nie blokuje reszty
            report.append(f"{client['klient']}: BLAD porzadkow: {str(exc)[:200]}")
            code = 1
    if not report:
        print("Nic do posprzatania.")
        return code
    text = "Porzadki" + (" (PODGLAD, nic nie zmieniono)" if dry_run else "") + ":\n" + "\n".join(report)
    print(text)
    S.notify(webhook, text)
    return code


if __name__ == "__main__":
    sys.exit(main())
