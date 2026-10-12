"""
SORTOWANIE SUROWYCH KLIPOW - WERSJA AUTOMATYCZNA (jeden odcinek na uruchomienie)
==============================================================================
Uruchamiana przez GitHub Actions po zaznaczeniu "Posortuj klipy" w Notion:
  - z PAGE_ID: jeden odcinek (Make wywoluje to dla Bloomroada od razu),
  - bez PAGE_ID: tryb cykliczny - co 15 min przeglada bazy z pliku
    sortowanie_klienci.json (Drony, Pavisorte, GeoSite...) i sortuje zaznaczone.
Drony maja Cam 1 i Cam 2 w jednym wierszu Logow klipow ("Numer klipu Cam 1",
"Numer klipu (Cam 2)") - skrypt rozbija taki wiersz na dwa klipy. Kamera jest
rozpoznawana tez po nazwie folderu (np. pliki DJI w folderze "Cam 1").
Logika Dysku Google jest taka sama jak w wersji interaktywnej (sortowanie do
folderow odcinek/kamera, zmiana nazw, "nie git" do folderu "kosz", notatki dla
montazysty, kontrola ciaglosci numeracji). Zmienilo sie tylko to, skad skrypt
bierze dane i co robi na koncu:

- Nic nie pyta. Dane bierze z Notion na podstawie PAGE_ID (strona odcinka):
    * klipy: baza "Logi klipow" przefiltrowana do tego odcinka,
    * folder z surowka: link w polu "Surowka" na stronie odcinka.
- Do Dysku loguje sie tokenem z GOOGLE_TOKEN_JSON (bez przegladarki).
- Na koncu odznacza "Posortuj klipy" w Notion i wysyla krotkie podsumowanie
  (webhook Make -> Slack), takze gdy cos poszlo nie tak.
- DRY_RUN=true: nic nie jest przenoszone ani zmieniane, skrypt tylko opisuje,
  co by zrobil.

Zmienne srodowiskowe:
  PAGE_ID             ID strony odcinka w Notion (wymagane)
  NOTION_TOKEN        token integracji Notion (wymagane)
  GOOGLE_TOKEN_JSON   zawartosc token.json z logowaniem do Dysku (wymagane)
  DRY_RUN             true/false (domyslnie true - bezpiecznie)
  RESULT_WEBHOOK_URL  adres webhooka Make z podsumowaniem (opcjonalne)
  FORCE               1 = uruchom nawet gdy "Posortuj klipy" nie jest zaznaczone
  CLIPS_RELATION_PROP nazwa relacji w Logach klipow do odcinka (domyslnie "Odcinek")
"""

import io
import json
import os
import re
import sys
import unicodedata

import requests

SCOPES = ["https://www.googleapis.com/auth/drive"]
NOTION_VERSION = "2022-06-28"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Nazwy wlasciwosci porownujemy bez polskich znakow, wielkosci liter i znakow
# specjalnych, wiec "Kamera / zrodlo" = "Kamera / zrodlo" = "kamera zrodlo".
ALIASES = {
    "kamera": ["kamerazrodlo", "kamera", "zrodlo", "doczego"],
    "numer": ["numerklipu", "numerpliku"],
    "etap": ["etapodcinka", "etap"],
    "ocena": ["ocena", "status"],
    "uwagi": ["uwagi", "notatka", "notatki"],
}


# ---------------------------------------------------------------------------
# Narzedzia
# ---------------------------------------------------------------------------

def ascii_fold(text):
    """Zamienia polskie (i inne) znaki na ASCII, np. litere l z kreska na zwykle l."""
    text = str(text).replace("\u0142", "l").replace("\u0141", "L")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.encode("ascii", "ignore").decode("ascii")


def norm(text):
    return re.sub(r"[^a-z0-9]", "", ascii_fold(text).lower())


def clean_string(text):
    return "".join(filter(str.isalnum, str(text).lower()))


def clean_filename(text):
    return str(text).strip().replace(" ", "_").replace("\n", "").replace("\r", "")


def extract_folder_id(raw):
    raw = (raw or "").strip()
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", raw)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", raw)
    if m:
        return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{15,}", raw):
        return raw
    return None


def plain(rich):
    return "".join(r.get("plain_text", "") for r in (rich or []))


def find_prop(props, canonical):
    wanted = ALIASES[canonical]
    by_norm = {norm(k): v for k, v in props.items()}
    for alias in wanted:
        if alias in by_norm:
            return by_norm[alias]
    return None


def prop_text(prop):
    """Zwraca tekst z dowolnego typu wlasciwosci (select/title/text/number...)."""
    if not prop:
        return ""
    t = prop.get("type")
    if t == "select":
        return (prop.get("select") or {}).get("name", "") or ""
    if t == "status":
        return (prop.get("status") or {}).get("name", "") or ""
    if t == "title":
        return plain(prop.get("title"))
    if t == "rich_text":
        return plain(prop.get("rich_text"))
    if t == "number":
        n = prop.get("number")
        return "" if n is None else str(int(n) if float(n).is_integer() else n)
    if t == "url":
        return prop.get("url") or ""
    return ""


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

class Notion:
    def __init__(self, token, http=requests):
        self.http = http
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _call(self, method, path, **kw):
        r = self.http.request(method, "https://api.notion.com" + path,
                              headers=self.headers, timeout=60, **kw)
        if r.status_code != 200:
            hint = ""
            if r.status_code in (401, 403, 404):
                hint = (" Sprawdz token oraz czy integracja ma dostep do tej strony "
                        "(Notion: ... > Polaczenia).")
            raise RuntimeError(f"Notion {r.status_code}: {r.text[:300]}.{hint}")
        return r.json()

    def get_page(self, page_id):
        return self._call("GET", f"/v1/pages/{page_id}")

    def patch_page(self, page_id, properties):
        return self._call("PATCH", f"/v1/pages/{page_id}", json={"properties": properties})

    def query_flagged(self, database_id):
        """Strony odcinkow z zaznaczonym 'Posortuj klipy'."""
        rows, cursor = [], None
        while True:
            body = {"page_size": 100,
                    "filter": {"property": "Posortuj klipy", "checkbox": {"equals": True}}}
            if cursor:
                body["start_cursor"] = cursor
            data = self._call("POST", f"/v1/databases/{database_id}/query", json=body)
            rows.extend(data.get("results", []))
            if not data.get("has_more"):
                return rows
            cursor = data["next_cursor"]

    def query_clips(self, database_id, relation_prop, page_id):
        rows, cursor = [], None
        while True:
            body = {"page_size": 100,
                    "filter": {"property": relation_prop, "relation": {"contains": page_id}}}
            if cursor:
                body["start_cursor"] = cursor
            data = self._call("POST", f"/v1/databases/{database_id}/query", json=body)
            rows.extend(data.get("results", []))
            if not data.get("has_more"):
                return rows
            cursor = data["next_cursor"]


def parse_clip(page):
    props = page["properties"]
    numer_txt = prop_text(find_prop(props, "numer"))
    m = re.search(r"\d+", numer_txt)
    return {
        "numer": int(m.group(0)) if m else None,
        "kamera": prop_text(find_prop(props, "kamera")).strip(),
        "etap": prop_text(find_prop(props, "etap")).strip(),
        "ocena": prop_text(find_prop(props, "ocena")).strip().lower(),
        "uwagi": prop_text(find_prop(props, "uwagi")).replace("\n", " ").strip(),
    }


def number_props(props):
    """Zwraca liste (kamera lub None, wlasciwosc) dla kolumn z numerem klipu.

    Zwykla baza ma jedna kolumne "Numer klipu" (kamera jest w osobnej kolumnie).
    Drony maja dwie: "Numer klipu Cam 1" i "Numer klipu (Cam 2)" - w jednym wierszu
    numer z kazdej kamery. Wtedy kamera wynika z nazwy kolumny."""
    out = []
    for key, prop in props.items():
        n = norm(key)
        if not (n.startswith("numerklipu") or n.startswith("numerpliku")):
            continue
        m = re.search(r"cam(?:era)?(\d)", n)
        out.append((f"Cam {m.group(1)}" if m else None, prop))
    return out


def expand_clip(page):
    """Jeden wiersz Logow klipow -> lista klipow (po jednym na kamere)."""
    props = page["properties"]
    base = parse_clip(page)
    per_cam = [(cam, p) for cam, p in number_props(props) if cam]
    if not per_cam:
        return [base]
    filled = []
    for cam, prop in per_cam:
        m = re.search(r"\d+", prop_text(prop))
        if m:  # puste = ta kamera nie nagrala tego ujecia
            filled.append((cam, int(m.group(0))))
    # Stare wiersze (jeden wiersz = jedna kamera): jest jeden numer i ustawiona
    # kamera w kolumnie "Kamera / zrodlo" - wtedy ta kolumna decyduje.
    if len(filled) == 1 and base["kamera"]:
        filled = [(base["kamera"], filled[0][1])]
    out = []
    for cam, numer in filled:
        item = dict(base)
        item["numer"] = numer
        item["kamera"] = cam
        out.append(item)
    return out


def load_episode(notion, page_id):
    """Zwraca (nazwa odcinka, flaga Posortuj klipy, link surowki, lista klipow)."""
    page = notion.get_page(page_id)
    props = page["properties"]
    by_norm = {norm(k): (k, v) for k, v in props.items()}

    title = ""
    for v in props.values():
        if v.get("type") == "title":
            title = plain(v["title"]).strip()
            break

    flag_key, flag_prop = by_norm.get("posortujklipy", (None, None))
    flag = bool(flag_prop and flag_prop.get("checkbox"))

    surowka = ""
    if "surowka" in by_norm:
        surowka = prop_text(by_norm["surowka"][1]).strip()

    logs = by_norm.get("logiklipow", (None, None))[1]
    rel_ids = [r["id"] for r in (logs or {}).get("relation", [])]
    clips = []
    if rel_ids:
        first = notion.get_page(rel_ids[0])
        db_id = first["parent"].get("database_id")
        if not db_id:
            raise RuntimeError("Nie udalo sie ustalic bazy Logi klipow.")
        relation_prop = os.environ.get("CLIPS_RELATION_PROP", "Odcinek")
        clips = [c for p in notion.query_clips(db_id, relation_prop, page_id)
                 for c in expand_clip(p)]
    return title, flag, flag_key, surowka, clips


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------

def get_drive_service(token_json):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("Token Google nie ma refresh_token - wygeneruj go ponownie.")
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_all(service, query, fields):
    # W trybie podgladu foldery "do utworzenia" maja sztuczne ID (DRYRUN_...).
    # Takiego folderu nie ma na Dysku, wiec nie wolno o niego pytac - zwracamy pusto.
    if "DRYRUN_" in query:
        return []
    items, token = [], None
    while True:
        res = service.files().list(
            q=query, fields=f"nextPageToken, files({fields})", pageSize=1000,
            pageToken=token, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute(num_retries=3)
        items.extend(res.get("files", []))
        token = res.get("nextPageToken")
        if not token:
            return items


def subfolders_recursive(service, parent_id):
    out = []
    for child in list_all(service, f"'{parent_id}' in parents and mimeType='{FOLDER_MIME}' and trashed=false", "id, name"):
        child["parent"] = parent_id
        out.append(child)
        out.extend(subfolders_recursive(service, child["id"]))
    return out


def find_or_create_folder(service, name, parent_id, cache, dry_run):
    key = f"{parent_id}_{name}"
    if key in cache:
        return cache[key]
    target = clean_string(name)
    for f in list_all(service, f"'{parent_id}' in parents and mimeType='{FOLDER_MIME}' and trashed=false", "id, name"):
        if clean_string(f["name"]) == target:
            cache[key] = f["id"]
            return f["id"]
    if dry_run or str(parent_id).startswith("DRYRUN_"):
        cache[key] = f"DRYRUN_{key}"
        return cache[key]
    created = service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id", supportsAllDrives=True).execute(num_retries=3)
    cache[key] = created["id"]
    return created["id"]


def write_notes(service, folder_id, content, dry_run):
    if dry_run or str(folder_id).startswith("DRYRUN_"):
        return
    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(content.encode("utf-8")), mimetype="text/plain")
    existing = list_all(service, f"name='notatki.txt' and '{folder_id}' in parents and trashed=false", "id")
    if existing:
        service.files().update(fileId=existing[0]["id"], media_body=media,
                               supportsAllDrives=True).execute(num_retries=3)
    else:
        service.files().create(body={"name": "notatki.txt", "parents": [folder_id]},
                               media_body=media, supportsAllDrives=True).execute(num_retries=3)


VIDEO_EXT = (".mp4", ".mov")


def folder_context(folder_id, info, root_id):
    """Nazwy folderow od pliku w gore (bez folderu glownego), male litery."""
    names, seen = [], set()
    while folder_id and folder_id != root_id and folder_id in info and folder_id not in seen:
        seen.add(folder_id)
        names.append(info[folder_id]["name"].lower())
        folder_id = info[folder_id].get("parent")
    return " ".join(names)


def scan_video_files(service, folder_ids, info=None, root_id=None):
    files = []
    info = info or {}
    for fid in folder_ids:
        ctx = folder_context(fid, info, root_id)
        for f in list_all(service, f"'{fid}' in parents and trashed=false", "id, name, parents"):
            if f["name"].lower().endswith(VIDEO_EXT):
                f["ctx"] = ctx
                files.append(f)
    return files


def camera_of(f):
    hay = (f["name"] + " " + f.get("ctx", "")).lower().replace(" ", "")
    for key in ("cam1", "cam2", "cam3", "cam4"):
        if key in hay:
            return key
    if "dji" in hay:
        return "dji"
    return None


def clip_number_of(name):
    stem = name.lower().rsplit(".", 1)[0]
    nums = re.findall(r"(?<!\d)(\d{4})(?!\d)", stem)
    return int(nums[-1]) if nums else None


def drive_gaps(files):
    """Zwraca liste linii opisujacych luki w numeracji plikow na Dysku."""
    clips = {}
    for f in files:
        cam, n = camera_of(f), clip_number_of(f["name"])
        if cam and n is not None:
            clips.setdefault(cam, set()).add(n)
    lines = []
    for cam, nums in sorted(clips.items()):
        missing = sorted(set(range(min(nums), max(nums) + 1)) - nums)
        if missing:
            lines.append(f"{cam.upper()}: brakuje plikow {fmt_nums(missing)}")
    return lines


def fmt_nums(nums, limit=15):
    nums = sorted(nums)
    shown = ", ".join(f"{n:04d}" for n in nums[:limit])
    return shown + (f" i {len(nums) - limit} wiecej" if len(nums) > limit else "")


SORTED_RE = re.compile(r"^cam\d_.+_\d{4}\.[a-z0-9]+$")


def find_matching_file(files, number4, camera, episode_clean="", trash_id=None, want_trash=False):
    """Wybiera plik z Dysku dla klipu. Kolejnosc: plik tego odcinka (ponowne
    uruchomienie) > surowy plik > plik w koszu. Pliki juz posortowane do INNYCH
    odcinkow sa pomijane (te same numery moga wystapic dwa razy)."""
    cam_key = camera.lower().replace(" ", "")
    num_re = re.compile(rf"(?<!\d){number4}(?!\d)")
    best, best_rank = None, None
    for f in files:
        low = f["name"].lower()
        hay = (low + " " + f.get("ctx", "")).replace(" ", "")
        if cam_key not in hay or not num_re.search(low):
            continue
        in_trash = bool(trash_id and trash_id in f.get("parents", []))
        if episode_clean and low.startswith(f"{cam_key}_{episode_clean.lower()}_"):
            rank = 0
        elif SORTED_RE.match(low) and not in_trash:
            continue  # posortowany do innego odcinka
        else:
            rank = 1
        if in_trash:
            rank = 0 if want_trash else 2
        if best_rank is None or rank < best_rank:
            best, best_rank = f, rank
    return best


# ---------------------------------------------------------------------------
# Glowna logika
# ---------------------------------------------------------------------------

def sort_clips(service, root_id, episode, clips, dry_run, log=print):
    result = {"ok": 0, "kosz": 0, "missing": {}, "no_camera": [], "gaps": [], "renamed": 0}
    cache, notes = {}, {}
    episode = ascii_fold(episode)

    trash_id = find_or_create_folder(service, "kosz", root_id, cache, dry_run)
    subs = subfolders_recursive(service, root_id)
    info = {f["id"]: f for f in subs}
    folders = [root_id] + [f["id"] for f in subs]
    files = scan_video_files(service, folders, info, root_id)
    result["gaps"] = drive_gaps(files)
    log(f"Na Dysku znaleziono {len(files)} plikow .mp4.")

    episode_clean = clean_filename(episode)
    for clip in clips:
        numer = clip["numer"]
        if numer is None:
            continue
        camera = clip["kamera"]
        if not camera:
            result["no_camera"].append(numer)
            continue
        number4 = f"{numer:04d}"
        found = find_matching_file(files, number4, camera, episode_clean, trash_id,
                                   want_trash=(clip["ocena"] == "nie git"))
        if not found:
            result["missing"].setdefault(camera, []).append(numer)
            continue

        parents = found.get("parents", [])
        if clip["ocena"] == "nie git":
            if trash_id not in parents:
                if not dry_run:
                    service.files().update(
                        fileId=found["id"], addParents=trash_id,
                        removeParents=",".join(parents), supportsAllDrives=True).execute(num_retries=3)
                log(f"{'[PODGLAD] ' if dry_run else ''}do kosza: {found['name']}")
            result["kosz"] += 1
            continue

        episode_folder = find_or_create_folder(service, episode, root_id, cache, dry_run)
        camera_folder = find_or_create_folder(service, camera, episode_folder, cache, dry_run)
        parts = [camera.replace(" ", ""), episode_clean]
        if clip["etap"]:
            parts.append(clean_filename(clip["etap"]))
        parts.append(number4)
        ext = ".mov" if found["name"].lower().endswith(".mov") else ".mp4"
        new_name = "_".join(parts) + ext

        if camera_folder not in parents:
            if not dry_run:
                service.files().update(
                    fileId=found["id"], body={"name": new_name}, addParents=camera_folder,
                    removeParents=",".join(parents), supportsAllDrives=True).execute(num_retries=3)
            log(f"{'[PODGLAD] ' if dry_run else ''}posortowano: {new_name}")
        elif found["name"] != new_name:
            if not dry_run:
                service.files().update(fileId=found["id"], body={"name": new_name},
                                       supportsAllDrives=True).execute(num_retries=3)
            result["renamed"] += 1
            log(f"{'[PODGLAD] ' if dry_run else ''}zmieniono nazwe: {new_name}")
        result["ok"] += 1
        if clip["uwagi"]:
            notes.setdefault(episode_folder, []).append(f"[{new_name}]: {ascii_fold(clip['uwagi'])}")

    for folder_id, lines in notes.items():
        write_notes(service, folder_id, "NOTATKI DLA MONTAZYSTY:\n\n" + "\n".join(lines), dry_run)
    result["notes_folders"] = len(notes)
    return result


def build_summary(episode, result, dry_run, error=None, klient=""):
    head = f"Sortowanie klipow, {klient + ', ' if klient else ''}{episode or 'odcinek'}"
    if dry_run:
        head += " (PODGLAD, nic nie przeniesiono)"
    if error:
        return f"{head}: NIE UDALO SIE. {error}"
    parts = [f"OK {result['ok']}", f"kosz {result['kosz']}"]
    text = f"{head}: " + ", ".join(parts) + "."
    if result["missing"]:
        miss = "; ".join(f"{cam}: {fmt_nums(nums)}" for cam, nums in result["missing"].items())
        text += f" Brak na Dysku: {miss}."
    if result["no_camera"]:
        text += f" Bez kamery w Notion: {fmt_nums(result['no_camera'])}."
    if result["gaps"]:
        text += " Luki w numeracji plikow: " + "; ".join(result["gaps"]) + "."
    return text


def notify(webhook, text):
    if not webhook:
        return
    try:
        requests.post(webhook, json={"text": text}, timeout=30)
    except Exception as exc:  # powiadomienie nie moze wywrocic calego przebiegu
        print(f"Nie udalo sie wyslac powiadomienia: {exc}")


def process_episode(notion, page_id, dry_run, webhook, force=False, klient=""):
    """Sortuje klipy jednego odcinka, odznacza pole i wysyla podsumowanie. Zwraca 0/1."""
    episode, flag_key, error, result = "", None, None, None
    try:
        episode, flag, flag_key, surowka, clips = load_episode(notion, page_id)
        if not flag and not force:
            print("'Posortuj klipy' nie jest zaznaczone - nic do zrobienia.")
            return 0
        if not clips:
            raise RuntimeError("W 'Logi klipow' nie ma klipow przypisanych do tego odcinka.")
        root_id = extract_folder_id(surowka)
        if not root_id:
            raise RuntimeError("W polu 'Surowka' na stronie odcinka wpisz link do folderu z surowka na Dysku.")
        service = get_drive_service(os.environ["GOOGLE_TOKEN_JSON"])
        result = sort_clips(service, root_id, episode, clips, dry_run)
    except Exception as exc:  # noqa: BLE001 - chcemy zawsze odznaczyc pole i powiadomic
        error = str(exc)
        print(f"BLAD: {error}")

    summary = build_summary(episode, result, dry_run, error, klient)
    print(summary)
    notify(webhook, summary)

    if flag_key:  # odznacz, zeby dalo sie zaznaczyc ponownie
        try:
            notion.patch_page(page_id, {flag_key: {"checkbox": False}})
        except Exception as exc:  # noqa: BLE001
            print(f"Nie udalo sie odznaczyc pola: {exc}")
    return 1 if error else 0


def load_clients(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def poll_all(notion, config_path, dry_run, webhook):
    """Tryb cykliczny: przeglada bazy Odcinki wszystkich klientow z pliku
    sortowanie_klienci.json i sortuje kazdy odcinek z zaznaczonym 'Posortuj klipy'."""
    code = 0
    for client in load_clients(config_path):
        name, db_id = client["klient"], client["odcinki_db"]
        try:
            pages = notion.query_flagged(db_id)
        except Exception as exc:  # noqa: BLE001 - jeden zepsuty klient nie blokuje reszty
            print(f"[{name}] nie moge odczytac bazy odcinkow: {exc}")
            notify(webhook, f"Sortowanie klipow, {name}: nie moge odczytac bazy odcinkow w Notion. {exc}")
            code = 1
            continue
        print(f"[{name}] odcinkow do posortowania: {len(pages)}")
        for page in pages:
            code |= process_episode(notion, page["id"], dry_run, webhook, force=True, klient=name)
    return code


def main():
    page_id = os.environ.get("PAGE_ID", "").strip()
    dry_run = os.environ.get("DRY_RUN", "true").strip().lower() in ("1", "true", "yes", "tak")
    webhook = os.environ.get("RESULT_WEBHOOK_URL", "").strip()
    force = os.environ.get("FORCE", "").strip() in ("1", "true", "yes")
    notion = Notion(os.environ["NOTION_TOKEN"])
    if page_id:
        return process_episode(notion, page_id, dry_run, webhook, force)
    config = os.environ.get("CLIENTS_FILE", "sortowanie_klienci.json")
    return poll_all(notion, config, dry_run, webhook)


if __name__ == "__main__":
    sys.exit(main())
