"""Testy porzadkow (archiwum) na atrapach - bez prawdziwych kont."""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import porzadki as P  # noqa: E402

TODAY = dt.date(2026, 11, 25)


def page(pid, name, status="Opublikowane", pub=None, surowka=""):
    props = {
        "Nazwa": {"type": "title", "title": [{"plain_text": name}]},
        "Status": {"type": "select", "select": {"name": status}},
        "Data publikacji": {"type": "date", "date": {"start": pub} if pub else None},
        "Surowka": {"type": "rich_text", "rich_text": [{"plain_text": surowka, "href": None}] if surowka else []},
        "URL": {"type": "url", "url": "https://youtu.be/abc"},
        "Logi klipow": {"type": "relation", "relation": []},
    }
    return {"id": pid, "properties": props}


class Exec:
    def __init__(self, value=None, exc=None):
        self.value, self.exc = value, exc

    def execute(self, num_retries=0):
        if self.exc:
            raise self.exc
        return self.value


class HttpErr(Exception):
    def __init__(self, status):
        super().__init__(f"http {status}")
        self.resp = type("R", (), {"status": status})()


class FakeDrive:
    def __init__(self, folders, children=None, denied=()):
        self.folders = folders            # id -> name
        self.children = children or {}    # id -> [ids]
        self.denied = set(denied)
        self.trashed = []

    def files(self):
        return self

    def get(self, fileId, **kw):
        if fileId in self.denied:
            return Exec(exc=HttpErr(404))
        return Exec({"id": fileId, "name": self.folders[fileId],
                     "mimeType": P.S.FOLDER_MIME, "trashed": False})

    def list(self, q, **kw):
        parent = q.split("'")[1]
        return Exec({"files": [{"id": c, "name": self.folders[c]} for c in self.children.get(parent, [])]})

    def update(self, fileId, body, **kw):
        self.trashed.append(fileId)
        return Exec({})


def link(fid):
    return f"https://drive.google.com/drive/u/1/folders/{fid}"


class SelectTest(unittest.TestCase):
    def test_only_older_than_30_days(self):
        pages = [page("a", "Stary", pub="2026-10-14"), page("b", "Nowy", pub="2026-11-11"),
                 page("c", "Granica", pub="2026-10-26"), page("d", "BezDaty")]
        old, no_date = P.split_old(pages, 30, TODAY)
        self.assertEqual([p["id"] for p in old], ["a"])
        self.assertEqual([p["id"] for p in no_date], ["d"])

    def test_datetime_publication(self):
        old, _ = P.split_old([page("a", "X", pub="2026-10-14T17:00:00.000Z")], 30, TODAY)
        self.assertEqual(len(old), 1)


class DriveTest(unittest.TestCase):
    def test_shared_root_is_never_trashed(self):
        # wspolny folder "Surowki Drony" bez podfolderu o nazwie odcinka
        drive = FakeDrive({"root_folder_id_000000000": "Surowki Drony"})
        p = page("a", "DronyDoSmogu", pub="2026-10-01", surowka=link("root_folder_id_000000000"))
        msg = P.handle_drive(lambda: drive, p, "DronyDoSmogu", set(), dry_run=False)
        self.assertEqual(drive.trashed, [])
        self.assertIn("Dysku nie ruszam", msg)

    def test_subfolder_with_episode_name_is_trashed(self):
        drive = FakeDrive({"root_folder_id_000000000": "Surowki", "sub_folder_id_000000000": "DronyDoSmogu", "x_folder_id_000000000": "Inny"},
                          children={"root_folder_id_000000000": ["x_folder_id_000000000", "sub_folder_id_000000000"]})
        p = page("a", "DronyDoSmogu", surowka=link("root_folder_id_000000000"))
        P.handle_drive(lambda: drive, p, "DronyDoSmogu", set(), dry_run=False)
        self.assertEqual(drive.trashed, ["sub_folder_id_000000000"])

    def test_linked_folder_named_like_episode(self):
        drive = FakeDrive({"ep_folder_id_000000000": "Jaki dron na 2027"})
        p = page("a", "JakiDronNa2027", surowka=link("ep_folder_id_000000000"))
        P.handle_drive(lambda: drive, p, "JakiDronNa2027", set(), dry_run=False)
        self.assertEqual(drive.trashed, ["ep_folder_id_000000000"])

    def test_folder_used_by_other_episode_is_kept(self):
        drive = FakeDrive({"ep_folder_id_000000000": "JakiDronNa2027"})
        p = page("a", "JakiDronNa2027", surowka=link("ep_folder_id_000000000"))
        msg = P.handle_drive(lambda: drive, p, "JakiDronNa2027", {"ep_folder_id_000000000"}, dry_run=False)
        self.assertEqual(drive.trashed, [])
        self.assertIn("inny odcinek", msg)

    def test_dry_run_trashes_nothing(self):
        drive = FakeDrive({"ep_folder_id_000000000": "JakiDronNa2027"})
        p = page("a", "JakiDronNa2027", surowka=link("ep_folder_id_000000000"))
        msg = P.handle_drive(lambda: drive, p, "JakiDronNa2027", set(), dry_run=True)
        self.assertEqual(drive.trashed, [])
        self.assertIn("do kosza", msg)

    def test_no_access_folder_is_skipped(self):
        drive = FakeDrive({"ep_folder_id_000000000": "X"}, denied={"ep_folder_id_000000000"})
        p = page("a", "JakiDronNa2027", surowka=link("ep_folder_id_000000000"))
        msg = P.handle_drive(lambda: drive, p, "JakiDronNa2027", set(), dry_run=False)
        self.assertEqual(drive.trashed, [])
        self.assertIn("Dysku nie ruszam", msg)

    def test_short_name_needs_exact_match(self):
        self.assertFalse(P.name_matches("Odcinek 12 - surowka", "Odc"))
        self.assertTrue(P.name_matches("odc", "Odc"))


class SnapshotTest(unittest.TestCase):
    def test_snapshot_keeps_panel_data(self):
        p = page("a", "X", pub="2026-10-14", surowka=link("abcdefghijklmnop"))
        head, lines = P.snapshot_lines(p, TODAY)
        text = "\n".join(lines)
        self.assertIn("25.11.2026", head)
        self.assertIn("Data publikacji: 2026-10-14", text)
        self.assertIn("URL: https://youtu.be/abc", text)
        self.assertIn("Status: Opublikowane", text)
        self.assertNotIn("Logi klipow", text)
        blocks = P.snapshot_blocks(head, lines)
        self.assertEqual(blocks[0]["type"], "heading_3")


if __name__ == "__main__":
    unittest.main()
