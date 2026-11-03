"""SQLite store for election snapshots: data/election/election-<year>.sqlite (committed by the job).

Every poll of the SOS feed is logged in `polls`; a snapshot row is written only when the feed's version or
LastUpdatedTime changed, so the store holds the real sequence of updates rather than 5-minute duplicates.
Statewide race totals are stored for every snapshot (small). County-level rows are stored as deltas: a
county/race row is written only when its votes or precinct counts differ from the last row stored for it,
which keeps the county history to a few tens of thousands of rows over a night.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    fetched_at TEXT PRIMARY KEY, version TEXT, source_updated TEXT, changed INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, fetched_at TEXT, source_updated TEXT, version TEXT,
    counties_reporting INTEGER, counties_total INTEGER, precincts_reporting INTEGER, precincts_total INTEGER,
    polling_reporting INTEGER, polling_total INTEGER);
CREATE TABLE IF NOT EXISTS races (
    key TEXT PRIMARY KEY, sos_id INTEGER, sos_name TEXT, section TEXT, title TEXT, chamber TEXT,
    competitive INTEGER DEFAULT 0, open_seat INTEGER DEFAULT 0, region TEXT, report_section TEXT, note TEXT,
    meta TEXT);
CREATE TABLE IF NOT EXISTS candidates (
    race_key TEXT, cand_id TEXT, name TEXT, party TEXT, incumbent INTEGER, ballot_order INTEGER,
    PRIMARY KEY (race_key, cand_id));
CREATE TABLE IF NOT EXISTS race_votes (
    snapshot_id INTEGER, race_key TEXT, cand_id TEXT, votes INTEGER, early_votes INTEGER,
    PRIMARY KEY (snapshot_id, race_key, cand_id));
CREATE TABLE IF NOT EXISTS race_reporting (
    snapshot_id INTEGER, race_key TEXT, total INTEGER, precincts_reporting INTEGER, precincts_total INTEGER,
    counties_reporting INTEGER, counties_total INTEGER,
    PRIMARY KEY (snapshot_id, race_key));
CREATE TABLE IF NOT EXISTS county_votes (
    snapshot_id INTEGER, race_key TEXT, county TEXT, cand_id TEXT, votes INTEGER, early_votes INTEGER,
    precincts_reporting INTEGER, precincts_total INTEGER,
    PRIMARY KEY (snapshot_id, race_key, county, cand_id));
CREATE TABLE IF NOT EXISTS county_turnout (
    snapshot_id INTEGER, county TEXT, code TEXT, registered INTEGER, votes_cast INTEGER,
    precincts_reporting INTEGER, precincts_total INTEGER, polling_reporting INTEGER, polling_total INTEGER,
    PRIMARY KEY (snapshot_id, county));
CREATE TABLE IF NOT EXISTS calls (
    race_key TEXT PRIMARY KEY, winner_cand_id TEXT, called_by TEXT, called_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS local_votes (
    fetched_at TEXT, race_key TEXT, cand_id TEXT, votes INTEGER, early_votes INTEGER, eday_votes INTEGER,
    precincts_reporting INTEGER, precincts_total INTEGER, run_time TEXT, digest TEXT,
    PRIMARY KEY (fetched_at, race_key, cand_id));
CREATE TABLE IF NOT EXISTS local_polls (
    fetched_at TEXT, county TEXT, ok INTEGER, pending INTEGER, contests INTEGER, matched INTEGER, note TEXT,
    PRIMARY KEY (fetched_at, county));
CREATE INDEX IF NOT EXISTS ix_local_votes_race ON local_votes (race_key, fetched_at);
CREATE INDEX IF NOT EXISTS ix_race_votes_race ON race_votes (race_key, snapshot_id);
CREATE INDEX IF NOT EXISTS ix_county_votes_race ON county_votes (race_key, county, snapshot_id);
CREATE INDEX IF NOT EXISTS ix_county_turnout_county ON county_turnout (county, snapshot_id);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # -- metadata --------------------------------------------------------------------------------------------
    def upsert_races(self, races: list[dict[str, Any]]) -> None:
        for r in races:
            self.db.execute(
                """INSERT INTO races (key, sos_id, sos_name, section, title, chamber, competitive, open_seat, region,
                                      report_section, note, meta)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET sos_id=excluded.sos_id, sos_name=excluded.sos_name,
                       section=excluded.section, title=excluded.title, chamber=excluded.chamber,
                       competitive=excluded.competitive, open_seat=excluded.open_seat, region=excluded.region,
                       report_section=excluded.report_section, note=excluded.note, meta=excluded.meta""",
                (r["key"], r.get("sos_id"), r.get("sos_name"), r.get("section"), r.get("title"), r.get("chamber"),
                 int(bool(r.get("competitive"))), int(bool(r.get("open_seat"))), r.get("region"),
                 r.get("report_section"), r.get("note"), json.dumps(r.get("meta") or {})))
            for c in r.get("candidates") or []:
                self.db.execute(
                    """INSERT INTO candidates (race_key, cand_id, name, party, incumbent, ballot_order) VALUES (?,?,?,?,?,?)
                       ON CONFLICT(race_key, cand_id) DO UPDATE SET name=excluded.name, party=excluded.party,
                           incumbent=excluded.incumbent, ballot_order=excluded.ballot_order""",
                    (r["key"], c["id"], c["name"], c.get("party"), int(bool(c.get("incumbent"))), c.get("order")))
        self.db.commit()

    # -- snapshots -------------------------------------------------------------------------------------------
    def last_snapshot(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()

    def log_poll(self, fetched_at: str, version: str, source_updated: str | None, changed: bool, note: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO polls VALUES (?,?,?,?,?)", (fetched_at, version, source_updated, int(changed), note))
        self.db.commit()

    def changed_since_last(self, snap: dict[str, Any]) -> bool:
        last = self.last_snapshot()
        if last is None:
            return True
        return (last["version"], last["source_updated"]) != (snap["version"], snap["source_updated"])

    def write_snapshot(self, snap: dict[str, Any], keyed: dict[str, dict[str, Any]]) -> int:
        """`keyed` maps race key -> normalized race (from the snapshot, restricted to tracked races).
        Returns the new snapshot id."""
        rep = snap["reporting"]
        cur = self.db.execute(
            """INSERT INTO snapshots (fetched_at, source_updated, version, counties_reporting, counties_total,
                                      precincts_reporting, precincts_total, polling_reporting, polling_total)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (snap["fetched_at"], snap["source_updated"], snap["version"], rep["counties"][0], rep["counties"][1],
             rep["precincts"][0], rep["precincts"][1], rep["polling"][0], rep["polling"][1]))
        sid = int(cur.lastrowid)

        # statewide totals for every tracked race
        for key, r in keyed.items():
            for c in r["candidates"]:
                self.db.execute("INSERT OR REPLACE INTO race_votes VALUES (?,?,?,?,?)",
                                (sid, key, c["id"], c["votes"], c["early_votes"]))

        # county level: deltas only
        name_to_key = {r["name"]: key for key, r in keyed.items()}
        per_race_prec: dict[str, list[int]] = {k: [0, 0, 0, 0] for k in keyed}   # pr, pt, counties reporting, counties total
        last_rows = self._last_county_rows()
        last_turnout = self._last_turnout_rows()
        for county, crec in snap["counties"].items():
            t_new = (crec["registered"], crec["votes_cast"], crec["precincts"][0], crec["precincts"][1], crec["polling"][0], crec["polling"][1])
            if last_turnout.get(county) != t_new:
                self.db.execute("INSERT OR REPLACE INTO county_turnout VALUES (?,?,?,?,?,?,?,?,?)",
                                (sid, county, crec["code"], *t_new))
            for rname, rr in crec["races"].items():
                key = name_to_key.get(rname)
                if not key:
                    continue
                pr, pt = rr["precincts"]
                agg = per_race_prec[key]
                agg[0] += pr; agg[1] += pt; agg[3] += 1
                if pr and pt and pr >= pt:
                    agg[2] += 1
                for cid, (v, ev) in rr["votes"].items():
                    new = (v, ev, pr, pt)
                    if last_rows.get((key, county, cid)) != new:
                        self.db.execute("INSERT OR REPLACE INTO county_votes VALUES (?,?,?,?,?,?,?,?)",
                                        (sid, key, county, cid, v, ev, pr, pt))
        for key, r in keyed.items():
            pr, pt, cr, ct = per_race_prec[key]
            self.db.execute("INSERT OR REPLACE INTO race_reporting VALUES (?,?,?,?,?,?,?)",
                            (sid, key, r["total"], pr, pt, cr, ct))
        self.db.commit()
        return sid

    def _last_county_rows(self) -> dict[tuple[str, str, str], tuple[int, int, int, int]]:
        """Latest stored (votes, early, pr, pt) per (race, county, candidate)."""
        rows = self.db.execute(
            """SELECT race_key, county, cand_id, votes, early_votes, precincts_reporting, precincts_total
               FROM county_votes cv
               WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM county_votes c2
                                    WHERE c2.race_key = cv.race_key AND c2.county = cv.county AND c2.cand_id = cv.cand_id)""").fetchall()
        return {(r[0], r[1], r[2]): (r[3], r[4], r[5], r[6]) for r in rows}

    def _last_turnout_rows(self) -> dict[str, tuple[int, ...]]:
        rows = self.db.execute(
            """SELECT county, registered, votes_cast, precincts_reporting, precincts_total, polling_reporting, polling_total
               FROM county_turnout ct
               WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM county_turnout c2 WHERE c2.county = ct.county)""").fetchall()
        return {r[0]: tuple(r[1:]) for r in rows}

    # -- reads for the exporter ------------------------------------------------------------------------------
    def snapshots(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM snapshots ORDER BY id").fetchall()

    def races(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM races ORDER BY key").fetchall()

    def candidates(self, race_key: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM candidates WHERE race_key=? ORDER BY ballot_order, name", (race_key,)).fetchall()

    def race_series(self, race_key: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT s.id, s.fetched_at, s.source_updated, rv.cand_id, rv.votes, rv.early_votes,
                      rr.total, rr.precincts_reporting, rr.precincts_total, rr.counties_reporting, rr.counties_total
               FROM race_votes rv JOIN snapshots s ON s.id = rv.snapshot_id
               LEFT JOIN race_reporting rr ON rr.snapshot_id = rv.snapshot_id AND rr.race_key = rv.race_key
               WHERE rv.race_key = ? ORDER BY s.id, rv.cand_id""", (race_key,)).fetchall()

    def latest_county_votes(self, race_key: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT county, cand_id, votes, early_votes, precincts_reporting, precincts_total, snapshot_id
               FROM county_votes cv
               WHERE race_key = ? AND snapshot_id = (SELECT MAX(snapshot_id) FROM county_votes c2
                     WHERE c2.race_key = cv.race_key AND c2.county = cv.county AND c2.cand_id = cv.cand_id)
               ORDER BY county, cand_id""", (race_key,)).fetchall()

    def latest_turnout(self) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT county, code, registered, votes_cast, precincts_reporting, precincts_total, polling_reporting, polling_total, snapshot_id
               FROM county_turnout ct
               WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM county_turnout c2 WHERE c2.county = ct.county)
               ORDER BY county""").fetchall()

    def calls(self) -> dict[str, sqlite3.Row]:
        return {r["race_key"]: r for r in self.db.execute("SELECT * FROM calls").fetchall()}

    def set_call(self, race_key: str, winner_cand_id: str, called_by: str, called_at: str, note: str = "") -> None:
        self.db.execute("INSERT OR REPLACE INTO calls VALUES (?,?,?,?,?)", (race_key, winner_cand_id, called_by, called_at, note))
        self.db.commit()

    # -- county-run (local) races ----------------------------------------------------------------------------
    def write_local(self, fetched_at: str, races: list[dict[str, Any]], status: dict[str, Any]) -> int:
        """Store each matched local race when its parsed block changed since the last stored one. Also keeps
        the race/candidate metadata rows so the exporter treats them like any other race."""
        n = 0
        for county, s in status.items():
            self.db.execute("INSERT OR REPLACE INTO local_polls VALUES (?,?,?,?,?,?,?)",
                            (fetched_at, county, int(bool(s.get("ok"))), int(bool(s.get("pending"))), s.get("contests", 0), s.get("matched", 0),
                             s.get("note") or s.get("error")))
        for r in races:
            last = self.db.execute("SELECT digest FROM local_votes WHERE race_key=? ORDER BY fetched_at DESC LIMIT 1", (r["key"],)).fetchone()
            self.upsert_races([{"key": r["key"], "sos_id": None, "sos_name": r["contest"], "section": "Local", "title": r["title"],
                                "chamber": "city" if r["key"].startswith("austin") else "county", "competitive": r["competitive"],
                                "open_seat": r["open_seat"], "region": r["county"], "report_section": "Local",
                                "note": r.get("note"), "meta": {"county": r["county"], "source": r["source"], "runoff_possible": r.get("runoff_possible")},
                                "candidates": [{"id": c["id"], "name": c["name"], "party": c["party"], "incumbent": False, "order": i + 1}
                                               for i, c in enumerate(r["candidates"])]}])
            if last and last["digest"] == r["digest"]:
                continue
            for c in r["candidates"]:
                self.db.execute("INSERT OR REPLACE INTO local_votes VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (fetched_at, r["key"], c["id"], c["votes"], c["early_votes"], c["eday_votes"], r["precincts"][0], r["precincts"][1],
                                 r.get("run_time"), r["digest"]))
            n += 1
        self.db.commit()
        return n

    def local_series(self, race_key: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM local_votes WHERE race_key=? ORDER BY fetched_at, cand_id", (race_key,)).fetchall()

    def local_status(self) -> list[sqlite3.Row]:
        return self.db.execute("""SELECT * FROM local_polls lp WHERE fetched_at = (SELECT MAX(fetched_at) FROM local_polls l2 WHERE l2.county = lp.county)
                                  ORDER BY county""").fetchall()

    def counts(self) -> dict[str, int]:
        out = {}
        for t in ("polls", "snapshots", "races", "candidates", "race_votes", "county_votes", "county_turnout", "calls", "local_votes", "local_polls"):
            out[t] = self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        return out
