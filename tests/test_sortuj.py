"""Testy na atrapach Dysku i Notion (bez dostepu do prawdziwych kont)."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import sortuj_klipy as S  # noqa: E402


class FakeDrive:
    """Minimalna atrapa Google Drive API v3 (files.list/create/update)."""

    def __init__(self):
        self.items = {}   # id -> dict(name, mime, parents, content)
        self.n = 0

    def add(self, name, parents, mime="video/mp4"):
        self.n += 1
        fid = f"id{self.n}"
        self.items[fid] = {"id": fid, "name": name, "mimeType": mime, "parents": list(parents), "content": None}
        return fid

    def files(self):
        return self

    def list(self, q, fields=None, pageSize=None, pageToken=None, **kw):
        res = [dict(v) for v in self.items.values()]
        m = re.search(r"'([^']+)' in parents", q)
        if m:
            res = [r for r in res if m.group(1) in r["parents"]]
        if "mimeType='application/vnd.google-apps.folder'" in q:
            res = [r for r in res if r["mimeType"] == S.FOLDER_MIME]
        m = re.search(r"name='([^']+)'", q)
        if m:
            res = [r for r in res if r["name"] == m.group(1)]
        return _Exec({"files": res})

    def create(self, body, fields=None, media_body=None, **kw):
        fid = self.add(body["name"], body.get("parents", []), body.get("mimeType", "text/plain"))
        if media_body is not None:
            self.items[fid]["content"] = media_body.getbytes(0, 10000).decode()
        return _Exec({"id": fid})

    def update(self, fileId, body=None, addParents=None, removeParents=None, media_body=None, **kw):
        it = self.items[fileId]
        if body and "name" in body:
            it["name"] = body["name"]
        if removeParents:
            for p in removeParents.split(","):
                if p in it["parents"]:
                    it["parents"].remove(p)
        if addParents:
            it["parents"].append(addParents)
        if media_body is not None:
            it["content"] = media_body.getbytes(0, 10000).decode()
        return _Exec({"id": fileId})

    def path(self, fid):
        it = self.items[fid]
        p = it["parents"][0] if it["parents"] else None
        return (self.path(p) + "/" if p in self.items else "") + it["name"]


class _Exec:
    def __init__(self, v):
        self.v = v

    def execute(self, num_retries=0):
        return self.v


def clip(numer, kamera, etap="", ocena="", uwagi=""):
    return {"numer": numer, "kamera": kamera, "etap": etap, "ocena": ocena, "uwagi": uwagi}


class SortTests(unittest.TestCase):
    def setUp(self):
        self.d = FakeDrive()
        self.root = self.d.add("Surowka", [], S.FOLDER_MIME)
        sub = self.d.add("karta1", [self.root], S.FOLDER_MIME)
        for n in (711, 712, 713, 715):                       # brakuje 714
            self.d.add(f"Cam1_C{n:04d}.mp4", [sub])
        for n in (1289, 1290, 1291):
            self.d.add(f"Cam2_{n}.mp4", [sub])
        self.d.add("Cam1_C0712_kopia_x10712.mp4", [sub])      # nie moze pasowac do 0712 ani 0071
        self.d.add("notatki_prywatne.txt", [sub], "text/plain")

    def run_sort(self, clips, dry=False):
        return S.sort_clips(self.d, self.root, "Rozmowa z Markiem", clips, dry, log=lambda *_: None)

    def names(self):
        return sorted(self.d.path(i) for i, v in self.d.items.items() if v["name"].endswith(".mp4"))

    def test_sortuje_i_zmienia_nazwy(self):
        r = self.run_sort([clip(711, "Cam 1", "Core"), clip(1289, "Cam 2")])
        self.assertEqual(r["ok"], 2)
        paths = self.names()
        self.assertIn("Surowka/Rozmowa z Markiem/Cam 1/Cam1_Rozmowa_z_Markiem_Core_0711.mp4", paths)
        self.assertIn("Surowka/Rozmowa z Markiem/Cam 2/Cam2_Rozmowa_z_Markiem_1289.mp4", paths)  # brak etapu = brak "__"

    def test_nie_git_idzie_do_kosza_i_nic_nie_jest_kasowane(self):
        r = self.run_sort([clip(713, "Cam 1", ocena="nie git")])
        self.assertEqual(r["kosz"], 1)
        self.assertIn("Surowka/kosz/Cam1_C0713.mp4", self.names())

    def test_brakujace_i_bez_kamery(self):
        r = self.run_sort([clip(714, "Cam 1"), clip(999, ""), clip(None, "Cam 1")])
        self.assertEqual(r["missing"], {"Cam 1": [714]})
        self.assertEqual(r["no_camera"], [999])
        self.assertEqual(r["ok"], 0)

    def test_numer_nie_pasuje_do_czesci_innego_numeru(self):
        r = self.run_sort([clip(712, "Cam 1")])
        self.assertEqual(r["ok"], 1)
        moved = [p for p in self.names() if "0712" in p and "Rozmowa" in p]
        self.assertEqual(len(moved), 1)
        self.assertTrue(any(p.endswith("Cam1_C0712_kopia_x10712.mp4") and "Rozmowa" not in p for p in self.names()))

    def test_luki_w_numeracji(self):
        r = self.run_sort([])
        self.assertTrue(any("CAM1" in g and "0714" in g for g in r["gaps"]))

    def test_podglad_nic_nie_zmienia(self):
        before = self.names()
        self.run_sort([clip(711, "Cam 1"), clip(713, "Cam 1", ocena="nie git")], dry=True)
        self.assertEqual(self.names(), before)
        self.assertFalse(any(v["name"] == "kosz" for v in self.d.items.values()))

    def test_drugie_uruchomienie_nic_nie_psuje(self):
        clips = [clip(711, "Cam 1", "Core"), clip(1290, "Cam 2", "Core")]
        self.run_sort(clips)
        after1 = self.names()
        r = self.run_sort(clips)
        self.assertEqual(self.names(), after1)
        self.assertEqual(r["ok"], 2)

    def test_notatki(self):
        self.run_sort([clip(711, "Cam 1", "Core", uwagi="dobry uj\u0119cie")])
        notes = [v for v in self.d.items.values() if v["name"] == "notatki.txt"]
        self.assertEqual(len(notes), 1)
        self.assertIn("dobry ujecie", notes[0]["content"])   # notatki zawsze ASCII
        self.run_sort([clip(711, "Cam 1", "Core", uwagi="zmienione")])   # aktualizacja, nie duplikat
        notes = [v for v in self.d.items.values() if v["name"] == "notatki.txt"]
        self.assertEqual(len(notes), 1)
        self.assertIn("zmienione", notes[0]["content"])


class AsciiTests(SortTests):
    def test_polskie_znaki_w_nazwie_odcinka_ida_do_ascii(self):
        self.run_sort_ep = S.sort_clips(self.d, self.root, "Zolw \u017c\u00f3\u0142ty \u0142\u00f3d\u017a", [clip(711, "Cam 1")], False, log=lambda *_: None)
        paths = self.names()
        self.assertIn("Surowka/Zolw zolty lodz/Cam 1/Cam1_Zolw_zolty_lodz_0711.mp4", paths)
        for p in paths:
            p.encode("ascii")
        self.assertEqual(S.ascii_fold("Za\u017c\u00f3\u0142\u0107 g\u0119\u015bl\u0105 ja\u017a\u0144"), "Zazolc gesla jazn")


class NotionParsingTests(unittest.TestCase):
    def test_nazwy_bez_polskich_znakow_i_typy(self):
        page = {"properties": {
            "Numer klipu": {"type": "title", "title": [{"plain_text": "715"}]},
            "Kamera / zrodlo": {"type": "select", "select": {"name": "Cam 1"}},
            "Etap odcinka": {"type": "select", "select": None},
            "Ocena": {"type": "select", "select": {"name": "Nie git"}},
            "Uwagi": {"type": "rich_text", "rich_text": [{"plain_text": "a\nb"}]},
        }}
        c = S.parse_clip(page)
        self.assertEqual(c, {"numer": 715, "kamera": "Cam 1", "etap": "", "ocena": "nie git", "uwagi": "a b"})

    def test_stara_nazwa_z_polskimi_znakami(self):
        page = {"properties": {"Numer klipu": {"type": "title", "title": [{"plain_text": "047"}]},
                               "Kamera / \u017ar\u00f3d\u0142o": {"type": "select", "select": {"name": "Cam 2"}}}}
        self.assertEqual(S.parse_clip(page)["kamera"], "Cam 2")

    def test_link_do_folderu(self):
        self.assertEqual(S.extract_folder_id("https://drive.google.com/drive/folders/1AbC_dEf-123456789012?usp=sharing"),
                         "1AbC_dEf-123456789012")
        self.assertEqual(S.extract_folder_id("https://drive.google.com/open?id=1AbC_dEf-123456789012"), "1AbC_dEf-123456789012")
        self.assertIsNone(S.extract_folder_id(""))
        self.assertIsNone(S.extract_folder_id("Folder u Marka"))


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, headers=None, timeout=None, json=None, **kw):
        self.calls.append((method, url, json))
        for (m, suffix), resp in self.routes.items():
            if m == method and url.endswith(suffix):
                return resp(json) if callable(resp) else FakeResp(resp)
        return FakeResp({"message": "not found"}, 404)


class FakeResp:
    def __init__(self, data, code=200):
        self._d, self.status_code, self.text = data, code, str(data)

    def json(self):
        return self._d


class LoadEpisodeTests(unittest.TestCase):
    def test_laduje_odcinek_klipy_i_paginacje(self):
        ep = {"properties": {
            "Nazwa": {"type": "title", "title": [{"plain_text": "RozmowaZMarkiem"}]},
            "Posortuj klipy": {"type": "checkbox", "checkbox": True},
            "Surowka": {"type": "rich_text", "rich_text": [{"plain_text": "https://drive.google.com/drive/folders/1AbC_dEf-123456789012"}]},
            "Logi klip\u00f3w": {"type": "relation", "relation": [{"id": "c1"}]},
        }}
        row = lambda n: {"properties": {"Numer klipu": {"type": "title", "title": [{"plain_text": str(n)}]},
                                        "Kamera / zrodlo": {"type": "select", "select": {"name": "Cam 1"}}}}
        pages = iter([{"results": [row(1), row(2)], "has_more": True, "next_cursor": "x"},
                      {"results": [row(3)], "has_more": False}])
        http = FakeHttp({
            ("GET", "/v1/pages/ep"): ep,
            ("GET", "/v1/pages/c1"): {"parent": {"database_id": "db1"}, "properties": {}},
            ("POST", "/v1/databases/db1/query"): lambda body: FakeResp(next(pages)),
        })
        title, flag, flag_key, surowka, clips = S.load_episode(S.Notion("t", http=http), "ep")
        self.assertEqual((title, flag, flag_key), ("RozmowaZMarkiem", True, "Posortuj klipy"))
        self.assertTrue(surowka.endswith("1AbC_dEf-123456789012"))
        self.assertEqual([c["numer"] for c in clips], [1, 2, 3])
        q = [c for c in http.calls if c[0] == "POST"][0][2]
        self.assertEqual(q["filter"], {"property": "Odcinek", "relation": {"contains": "ep"}})


class SummaryTests(unittest.TestCase):
    def test_podsumowanie(self):
        r = {"ok": 10, "kosz": 2, "missing": {"Cam 1": [714]}, "no_camera": [5], "gaps": ["CAM1: brakuje plikow 0714"]}
        t = S.build_summary("Odc", r, True)
        self.assertIn("PODGLAD", t)
        self.assertIn("OK 10", t)
        self.assertIn("0714", t)
        self.assertIn("NIE UDALO", S.build_summary("Odc", None, False, "blad"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
