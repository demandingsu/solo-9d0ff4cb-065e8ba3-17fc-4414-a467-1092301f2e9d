"""HTTP + database tests for the adjacent-version diff API.

Covers: baseline diff, renumber-only (not a change), capacity changes,
pure idempotent re-upload (new version, empty known increment), failed
upload leaving no version/increment, legacy versions without increment
records ('unknown', never 'empty'), split/merge classification over real
persisted versions, and snapshot consistency under concurrent uploads.
"""

import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# One process-wide database shared with test_integration: the app singleton
# is created once per process; distinct cell IDs keep the suites isolated.
# The first module imported creates and cleans the file, later modules reuse.
_DB = os.environ.setdefault(
    "BATTERY_DB_PATH",
    os.path.join(tempfile.mkdtemp(prefix="battery-shared-"), "shared.db"),
)
if not os.path.exists(_DB):
    for _suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_DB + _suffix)
        except FileNotFoundError:
            pass

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.db import Database, _to_storage  # noqa: E402

# Re-point the shared app singleton at the shared database.
app.db = Database(_DB)


def csv_text(rows, header="timestamp,current\n"):
    return header + "".join(f"{ts},{cur}\n" for ts, cur in rows)


def upload(client, cell_id, rows, header="timestamp,current\n"):
    return client.post(
        f"/cells/{cell_id}/samples",
        files={"file": ("samples.csv", csv_text(rows, header), "text/csv")},
    )


def raw_conn():
    conn = sqlite3.connect(_DB)
    conn.row_factory = sqlite3.Row
    return conn


def seed_version(cell_id, version, cycles, with_batch=True, inserted=0,
                 duplicate=0, increment_rows=None):
    """Insert a report version directly, bypassing the state machine.

    Used for structural split/merge cases that additive samples cannot
    produce, and for simulating legacy versions without increment rows.
    """
    with raw_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO cells(cell_id) VALUES(?)", (cell_id,)
        )
        conn.execute(
            "INSERT INTO report_versions(cell_id, version, total_cycles)"
            " VALUES(?,?,?)",
            (cell_id, version, len(cycles)),
        )
        conn.executemany(
            "INSERT INTO cycles(cell_id, version, cycle_no, charge_ah,"
            " discharge_ah, started_at, ended_at) VALUES(?,?,?,?,?,?,?)",
            [(cell_id, version, c[0], c[1], c[2], c[3], c[4]) for c in cycles],
        )
        if with_batch:
            conn.execute(
                "INSERT INTO upload_batches(cell_id, version, inserted,"
                " duplicate, increment_status) VALUES(?,?,?,?,'known')",
                (cell_id, version, inserted, duplicate),
            )
            for ts, cur in (increment_rows or []):
                conn.execute(
                    "INSERT INTO sample_increments(cell_id, version, ts,"
                    " current_ma) VALUES(?,?,?,?)",
                    (cell_id, version, ts, _to_storage(cur)),
                )


class BaselineDiffTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        self.cell = "CELL-DIFF-BASE"

    def test_first_version_against_empty_baseline(self):
        rows = [(0, 0.0), (10, 2.0), (20, 0.0), (30, -3.0), (40, 0.0)]
        r = upload(self.c, self.cell, rows)
        self.assertEqual(r.status_code, 200, r.text)

        d = self.c.get(f"/cells/{self.cell}/versions/1/diff")
        self.assertEqual(d.status_code, 200, d.text)
        body = d.json()
        self.assertEqual(body["from_version"], 0)
        self.assertEqual(body["to_version"], 1)
        self.assertEqual(len(body["added"]), 1)
        self.assertEqual(body["added"][0]["started_at"], 0)
        self.assertEqual(body["summary"]["added"], 1)
        # increment of v1 carries exactly the five uploaded samples
        inc = body["to_increment"]
        self.assertEqual(inc["status"], "known")
        self.assertEqual(inc["inserted"], 5)
        self.assertEqual(len(inc["samples"]), 5)
        self.assertEqual(inc["samples"][0], {"ts": 0, "current_a": 0.0})
        # baseline increment is a known empty state
        self.assertEqual(body["from_increment"]["status"], "known")
        self.assertEqual(body["from_increment"]["samples"], [])


class RenumberOnlyTests(unittest.TestCase):
    def test_adding_earlier_cycle_renumbers_old_ones_without_changing_them(self):
        c = TestClient(app)
        cell = "CELL-DIFF-RENUM"
        # two independent cycles (the 160 s gap keeps them separate)
        first = [
            (300, 0), (310, 1), (320, 0), (330, -1), (340, 0),
            (500, 0), (510, 1), (520, 0), (530, -1), (540, 0),
        ]
        upload(c, cell, first)
        # a new cycle earlier than both (160 s gap keeps it independent):
        # the two old cycles keep span/capacity but cycle_no shifts 1,2 -> 2,3
        late = [(100, 0), (110, 1), (120, 0), (130, -1), (140, 0)]
        r = upload(c, cell, late)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 2)

        body = c.get(f"/cells/{cell}/versions/2/diff").json()
        self.assertEqual(body["summary"]["added"], 1)
        self.assertEqual(body["summary"]["unchanged"], 2)
        self.assertEqual(body["summary"]["renumber_only"], 2)
        # the added one is the new earliest cycle
        self.assertEqual(body["added"][0]["started_at"], 100)
        # unchanged pairs preserve spans/capacities, only numbers moved
        for pair in body["unchanged"]:
            self.assertEqual(
                (pair["old"]["started_at"], pair["old"]["ended_at"]),
                (pair["new"]["started_at"], pair["new"]["ended_at"]),
            )
            self.assertEqual(pair["old"]["cycle_no"] + 1, pair["new"]["cycle_no"])
            self.assertAlmostEqual(
                pair["old"]["charge_ah"], pair["new"]["charge_ah"]
            )
        # nothing structurally changed
        self.assertEqual(body["split"], [])
        self.assertEqual(body["merged"], [])
        self.assertEqual(body["capacity_changed"], [])
        self.assertEqual(body["removed"], [])


class CapacityChangeTests(unittest.TestCase):
    def test_late_sample_changes_capacity_same_span(self):
        c = TestClient(app)
        cell = "CELL-DIFF-CAP"
        first = [(0, 0), (10, 1), (20, 1), (30, 0), (40, -1), (50, 0)]
        upload(c, cell, first)
        # extra non-zero point inside the charge segment (no zero boundary):
        # span of the cycle stays [0, 50], only the Ah value changes
        upload(c, cell, [(15, 2.0)])

        body = c.get(f"/cells/{cell}/versions/2/diff").json()
        self.assertEqual(len(body["capacity_changed"]), 1)
        chg = body["capacity_changed"][0]
        self.assertFalse(chg["span_changed"])
        self.assertEqual(chg["old"]["started_at"], chg["new"]["started_at"])
        self.assertGreater(chg["charge_delta_ah"], 0.0)
        self.assertEqual(body["to_increment"]["inserted"], 1)
        self.assertEqual(body["to_increment"]["samples"][0]["ts"], 15)


class PureReuploadTests(unittest.TestCase):
    def test_pure_reupload_creates_version_with_empty_known_increment(self):
        c = TestClient(app)
        cell = "CELL-DIFF-IDEM"
        rows = [(0, 0), (10, 1.5), (20, 0), (30, -2.5), (40, 0)]
        upload(c, cell, rows)
        r = upload(c, cell, rows)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"], 2)
        self.assertEqual(r.json()["inserted"], 0)

        body = c.get(f"/cells/{cell}/versions/2/diff").json()
        inc = body["to_increment"]
        self.assertEqual(inc["status"], "known")
        self.assertEqual(inc["inserted"], 0)
        self.assertEqual(inc["samples"], [])  # known-empty, not unknown
        # cycles are identical in every respect
        self.assertEqual(body["summary"]["unchanged"], 1)
        self.assertEqual(body["summary"]["renumber_only"], 0)
        for key in ("added", "removed", "split", "merged", "capacity_changed"):
            self.assertEqual(body[key], [])
        # an increment row really is absent at the sample level
        with raw_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM sample_increments"
                " WHERE cell_id=? AND version=2",
                (cell,),
            ).fetchone()["n"]
            self.assertEqual(n, 0)
            b = conn.execute(
                "SELECT inserted FROM upload_batches WHERE cell_id=? AND"
                " version=2",
                (cell,),
            ).fetchone()
            self.assertEqual(b["inserted"], 0)


class FailedUploadTests(unittest.TestCase):
    def test_failed_upload_leaves_no_version_or_increment(self):
        c = TestClient(app)
        cell = "CELL-DIFF-FAIL"
        upload(c, cell, [(0, 0), (10, 1), (20, 0), (30, -1), (40, 0)])
        before = c.get(f"/cells/{cell}/report").json()

        # conflicting value -> whole batch rejected
        r = upload(c, cell, [(10, 9.9), (99, 1)])
        self.assertEqual(r.status_code, 409)

        after = c.get(f"/cells/{cell}/report").json()
        self.assertEqual(after["version"], before["version"])

        with raw_conn() as conn:
            versions = [
                row["version"]
                for row in conn.execute(
                    "SELECT version FROM report_versions WHERE cell_id=?",
                    (cell,),
                )
            ]
            batches = [
                row["version"]
                for row in conn.execute(
                    "SELECT version FROM upload_batches WHERE cell_id=?",
                    (cell,),
                )
            ]
            increments = [
                row["version"]
                for row in conn.execute(
                    "SELECT DISTINCT version FROM sample_increments"
                    " WHERE cell_id=?",
                    (cell,),
                )
            ]
        self.assertEqual(versions, [1])
        self.assertEqual(batches, [1])
        self.assertEqual(increments, [1])

        # malformed CSV also creates nothing
        r2 = c.post(
            f"/cells/{cell}/samples",
            files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")},
        )
        self.assertEqual(r2.status_code, 400)
        with raw_conn() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM report_versions WHERE cell_id=?",
                (cell,),
            ).fetchone()["n"]
            self.assertEqual(n, 1)


class LegacyUnknownTests(unittest.TestCase):
    def test_legacy_version_without_batch_is_unknown_not_empty(self):
        c = TestClient(app)
        cell = "CELL-DIFF-LEGACY"
        # simulate two versions written by an older deployment that had no
        # upload_batches / sample_increments tables populated
        seed_version(
            cell, 1, [(1, 1.0, 1.0, 0, 40)], with_batch=False
        )
        seed_version(
            cell, 2, [(1, 1.0, 1.0, 0, 40), (2, 2.0, 2.0, 100, 140)],
            with_batch=False,
        )

        body = c.get(f"/cells/{cell}/versions/2/diff").json()
        frm, to = body["from_increment"], body["to_increment"]
        self.assertEqual(frm["status"], "unknown")
        self.assertIsNone(frm["samples"])
        self.assertIsNone(frm["inserted"])
        self.assertEqual(to["status"], "unknown")
        self.assertIsNone(to["samples"])
        # unknown increment must not block cycle comparison
        self.assertEqual(body["summary"]["added"], 1)
        self.assertEqual(body["summary"]["unchanged"], 1)

    def test_legacy_then_modern_upload_mixes_unknown_and_known(self):
        c = TestClient(app)
        cell = "CELL-DIFF-LEGACY2"
        seed_version(cell, 1, [(1, 1.0, 1.0, 0, 40)], with_batch=False)
        # a normal upload becomes v2 with a tracked increment
        r = upload(
            c,
            cell,
            [
                (0, 0), (10, 1), (20, 0), (30, -1), (40, 0),
                (200, 0), (210, 1), (220, 0), (230, -1), (240, 0),
            ],
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 2)

        body = c.get(f"/cells/{cell}/versions/2/diff").json()
        self.assertEqual(body["from_increment"]["status"], "unknown")
        self.assertIsNone(body["from_increment"]["samples"])
        self.assertEqual(body["to_increment"]["status"], "known")
        # 10 genuinely new samples
        self.assertEqual(body["to_increment"]["inserted"], 10)
        self.assertEqual(len(body["to_increment"]["samples"]), 10)


class StructuralSeedTests(unittest.TestCase):
    def test_split_classification_via_persisted_versions(self):
        c = TestClient(app)
        cell = "CELL-DIFF-SPLIT"
        seed_version(
            cell, 1,
            [(1, 4.0, 2.0, 0, 80)],
            inserted=5, increment_rows=[(0, 0.0)],
        )
        seed_version(
            cell, 2,
            [(1, 2.0, 2.0, 0, 40), (2, 2.0, 0.0, 40, 80)],
            inserted=2, increment_rows=[(25, 0.0), (35, 0.0)],
        )
        body = c.get(f"/cells/{cell}/versions/2/diff").json()
        self.assertEqual(len(body["split"]), 1)
        split = body["split"][0]
        self.assertEqual(split["from"]["started_at"], 0)
        self.assertEqual(split["from"]["ended_at"], 80)
        self.assertEqual(
            [x["started_at"] for x in split["into"]], [0, 40]
        )
        self.assertEqual(body["summary"]["split"], 1)
        self.assertEqual(body["added"], [])
        self.assertEqual(body["removed"], [])
        self.assertEqual(body["to_increment"]["inserted"], 2)

    def test_merge_classification_via_persisted_versions(self):
        c = TestClient(app)
        cell = "CELL-DIFF-MERGE"
        seed_version(
            cell, 1,
            [(1, 1.0, 1.0, 0, 40), (2, 1.0, 1.0, 40, 80)],
            inserted=9,
        )
        seed_version(
            cell, 2,
            [(1, 2.0, 2.0, 0, 80)],
            inserted=3,
        )
        body = c.get(f"/cells/{cell}/versions/2/diff").json()
        self.assertEqual(len(body["merged"]), 1)
        merge = body["merged"][0]
        self.assertEqual(
            [x["ended_at"] for x in merge["from"]], [40, 80]
        )
        self.assertEqual(merge["into"]["ended_at"], 80)
        self.assertEqual(body["summary"]["merged"], 1)


class HttpValidationTests(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(app)
        self.cell = "CELL-DIFF-HTTP"
        upload(self.c, self.cell, [(0, 0), (10, 1), (20, 0), (30, -1), (40, 0)])

    def test_unknown_cell_404(self):
        r = self.c.get("/cells/CELL-NOPE-DIFF/versions/1/diff")
        self.assertEqual(r.status_code, 404)

    def test_missing_version_404(self):
        r = self.c.get(f"/cells/{self.cell}/versions/99/diff")
        self.assertEqual(r.status_code, 404)

    def test_non_positive_version_400_or_422(self):
        r = self.c.get(f"/cells/{self.cell}/versions/0/diff")
        self.assertIn(r.status_code, (400, 422))


class SnapshotConcurrencyTests(unittest.TestCase):
    def test_diff_reads_one_committed_state_under_concurrent_uploads(self):
        cell = "CELL-DIFF-CONC"
        errors = []
        barrier = threading.Barrier(8)

        def writer(n):
            try:
                barrier.wait()
                cli = TestClient(app)
                base = 5000 + n * 1000
                r = upload(
                    cli,
                    cell,
                    [
                        (base, 0.0), (base + 10, 1.0), (base + 20, 0.0),
                        (base + 30, -1.0), (base + 40, 0.0),
                    ],
                )
                if r.status_code != 200:
                    errors.append(f"writer {n}: {r.status_code} {r.text}")
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        def reader():
            try:
                barrier.wait()
                cli = TestClient(app)
                for _ in range(60):
                    rep = cli.get(f"/cells/{cell}/report")
                    if rep.status_code == 404:
                        continue
                    version = rep.json()["version"]
                    rr = cli.get(f"/cells/{cell}/versions/{version}/diff")
                    if rr.status_code != 200:
                        errors.append(f"diff status {rr.status_code}")
                        continue
                    body = rr.json()
                    # from/to must be adjacent and the increment must match
                    # the to-version that the report snapshot showed
                    self.assertEqual(body["to_version"], version)
                    self.assertEqual(body["from_version"], version - 1)
                    # totals consistent with the classification
                    s = body["summary"]
                    self.assertEqual(s["new_total"], len(rep.json()["cycles"]))
                    # increment status must never be missing/ambiguous
                    self.assertIn(
                        body["to_increment"]["status"], ("known", "unknown")
                    )
            except Exception as exc:  # pragma: no cover
                errors.append(str(exc))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
