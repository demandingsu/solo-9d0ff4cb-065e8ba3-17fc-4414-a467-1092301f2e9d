"""Live-server smoke test.

Runs against an already-started uvicorn (default http://127.0.0.1:8000).
Covers: late-point rewrite/renumbering, gap truncation, idempotency and
conflict rejection, the adjacent-version diff API (increments, capacity
change, pure re-upload, legacy 'unknown'), and concurrent uploads/queries
with snapshot consistency.
"""

import concurrent.futures
import sys
import time
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

# Unique per run so the smoke test is repeatable against a persistent DB:
# fixed cell names would make later runs hit idempotent re-upload semantics.
RUN = f"SMOKE-{uuid.uuid4().hex[:8]}"


def upload(client, cell, rows, header="timestamp,current"):
    text = header + "\n" + "".join(f"{ts},{cur}\n" for ts, cur in rows)
    return client.post(
        f"{BASE}/cells/{cell}/samples",
        files={"file": ("batch.csv", text, "text/csv")},
    )


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"  PASS: {msg}")


def main():
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{BASE}/health")
        check(r.status_code == 200, "health endpoint")

        # --- 1. late point rewrites/renumbers cycles --------------------
        cell = f"{RUN}-LATE"
        r = upload(c, cell, [(0, 0), (10, 1), (20, 0), (30, -1), (40, 0), (50, 1)])
        check(r.status_code == 200, "first batch accepted")
        check(r.json()["total_cycles"] == 1, "one closed cycle, tail ignored")
        v1 = r.json()["version"]

        r = upload(c, cell, [(60, 0), (70, -1), (80, 0)])
        check(r.status_code == 200, "late batch accepted")
        check(r.json()["version"] == v1 + 1, "report version bumped")
        rep = c.get(f"{BASE}/cells/{cell}/report").json()
        check(rep["total_cycles"] == 2, "late point closed second cycle")
        check([x["cycle_no"] for x in rep["cycles"]] == [1, 2], "cycles renumbered 1..N")
        check(
            abs(rep["cycles"][0]["charge_ah"] - 1 * 10 / 3600) < 1e-12,
            "charge Ah from left-value integral",
        )

        # --- 2. gap > 60 s truncates ------------------------------------
        cell = f"{RUN}-GAP"
        r = upload(c, cell, [(0, 0), (10, 2), (71, 0), (80, -3), (90, 0)])
        rep = c.get(f"{BASE}/cells/{cell}/report").json()
        check(rep["total_cycles"] == 0, "61 s gap truncates the sequence")

        r = upload(c, cell, [(0, 0), (60, 2), (120, 0), (130, -1), (140, 0)])
        rep = c.get(f"{BASE}/cells/{cell}/report").json()
        check(rep["total_cycles"] == 1, "exactly-60 s intervals are kept")

        # --- 3. idempotency + conflict whole-batch rejection ------------
        cell = f"{RUN}-IDEM"
        rows = [(0, 0), (10, 1.5), (20, 0), (30, -2.5), (40, 0)]
        upload(c, cell, rows)
        r = upload(c, cell, rows)
        check(r.json()["inserted"] == 0 and r.json()["duplicate"] == 5,
              "identical re-upload is idempotent")
        before = c.get(f"{BASE}/cells/{cell}/report").json()
        r = upload(c, cell, [(10, 9.9), (99, 1)])
        check(r.status_code == 409, "different value at same ts -> 409")
        after = c.get(f"{BASE}/cells/{cell}/report").json()
        check(after["version"] == before["version"], "rejected batch keeps old version")
        check(after["total_cycles"] == before["total_cycles"], "old report unchanged")

        # --- 4. adjacent-version diff API -------------------------------
        cell = f"{RUN}-DIFF"
        r = upload(c, cell, [(0, 0), (10, 1), (20, 1), (30, 0), (40, -1), (50, 0)])
        check(r.status_code == 200, "diff: first upload")
        v1 = r.json()["version"]
        d = c.get(f"{BASE}/cells/{cell}/versions/{v1}/diff").json()
        check(d["from_version"] == 0 and d["to_version"] == v1,
              "diff: v1 compares against empty baseline v0")
        check(d["to_increment"]["status"] == "known"
              and d["to_increment"]["inserted"] == 6
              and len(d["to_increment"]["samples"]) == 6,
              "diff: v1 increment lists all six new samples")
        check(d["from_increment"]["status"] == "known"
              and d["from_increment"]["samples"] == [],
              "diff: baseline increment is a known empty state")
        check(d["summary"]["added"] == 1, "diff: baseline shows one added cycle")

        # late non-zero point keeps the span, changes only the capacity
        r = upload(c, cell, [(15, 2.0)])
        check(r.status_code == 200 and r.json()["version"] == v1 + 1,
              "diff: late point bumps the version")
        d = c.get(f"{BASE}/cells/{cell}/versions/{v1 + 1}/diff").json()
        check(len(d["capacity_changed"]) == 1
              and not d["capacity_changed"][0]["span_changed"],
              "diff: late point classified as capacity change, same span")
        check(d["to_increment"]["inserted"] == 1
              and d["to_increment"]["samples"][0]["ts"] == 15,
              "diff: increment contains exactly the late point")

        # pure re-upload: new version, known-empty increment, no cycle change
        r = upload(c, cell, [(0, 0), (10, 1), (20, 1), (30, 0), (40, -1), (50, 0)])
        v3 = r.json()["version"]
        check(r.json()["inserted"] == 0, "diff: re-upload inserts nothing")
        d = c.get(f"{BASE}/cells/{cell}/versions/{v3}/diff").json()
        check(d["to_increment"]["status"] == "known"
              and d["to_increment"]["samples"] == []
              and d["to_increment"]["inserted"] == 0,
              "diff: pure re-upload has a known-empty (not unknown) increment")
        check(d["summary"]["unchanged"] == 1 and d["summary"]["added"] == 0,
              "diff: pure re-upload changes no cycle")

        # failed upload must not create a diff-able version
        r = upload(c, cell, [(10, 9.9)])
        check(r.status_code == 409, "diff: conflicting batch rejected")
        check(c.get(f"{BASE}/cells/{cell}/versions/{v3 + 1}/diff").status_code
              == 404, "diff: no version exists after a rejected upload")
        check(c.get(f"{BASE}/cells/NO-SUCH-CELL/versions/1/diff").status_code
              == 404, "diff: unknown cell returns 404")
        check(c.get(f"{BASE}/cells/{cell}/versions/0/diff").status_code
              in (400, 422), "diff: non-positive version rejected")

        # --- 5. concurrency: 8 writers, parallel readers ----------------
        cell = f"{RUN}-CONC"

        def writer(n):
            base = n * 1000
            with httpx.Client(timeout=30) as wc:
                return upload(
                    wc, cell,
                    [(base, 0), (base + 10, 1), (base + 20, 0),
                     (base + 30, -1), (base + 40, 0)],
                ).status_code

        def reader(_):
            bad = []
            with httpx.Client(timeout=30) as rc:
                for _ in range(40):
                    rr = rc.get(f"{BASE}/cells/{cell}/report")
                    if rr.status_code == 404:
                        continue
                    rep = rr.json()
                    nos = [x["cycle_no"] for x in rep["cycles"]]
                    if rep["total_cycles"] != len(rep["cycles"]) or nos != list(
                        range(1, rep["total_cycles"] + 1)
                    ):
                        bad.append(rep)
                    # the diff of the reported version must be readable in
                    # the same committed state and stay internally consistent
                    dd = rc.get(
                        f"{BASE}/cells/{cell}/versions/{rep['version']}/diff"
                    )
                    if dd.status_code != 200:
                        bad.append({"diff_status": dd.status_code})
                    else:
                        body = dd.json()
                        if body["to_version"] != rep["version"]:
                            bad.append({"stale_diff": body["to_version"]})
            return bad

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            f_w = [pool.submit(writer, n) for n in range(8)]
            f_r = [pool.submit(reader, n) for n in range(8)]
            codes = [f.result() for f in f_w]
            inconsistencies = [x for f in f_r for x in f.result()]
        check(codes == [200] * 8, f"all 8 concurrent writers committed (got {codes})")
        check(not inconsistencies, "every query saw one internally consistent state")
        final = c.get(f"{BASE}/cells/{cell}/report").json()
        check(final["total_cycles"] == 8, "final report contains all 8 cycles")

        cells = c.get(f"{BASE}/cells").json()
        check(any(x["cell_id"] == cell for x in cells), "cell list endpoint")

    print("\nALL LIVE CHECKS PASSED")


if __name__ == "__main__":
    for attempt in range(20):
        try:
            httpx.get(f"{BASE}/health", timeout=2)
            break
        except httpx.HTTPError:
            time.sleep(0.5)
    main()
