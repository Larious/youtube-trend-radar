"""SQLite storage: the schema, and the reads and writes the scan makes.

Grain of each table:
  videos        one row per video ever kept (dimension): title, channel, language, niche, first seen,
                first day it qualified as a breakout
  channels      one row per channel (dimension): creation date; subscribers and video count refreshed on
                every sighting
  snaps         one row per video per day: its view count (fact). Velocity = difference between snapshots.
  clusters      one row per title-format cluster (dimension): first and last day seen
  cluster_days  one row per cluster per day: stage and wave metrics (daily aggregate)
  niche_days    one row per niche x language per day: demand, supply, saturation index and status
                (daily aggregate)
  fresh         membership of the 'uploaded today' sample: which uploads were sampled for which niche and day
  lonely        pre-wave breakouts that matched no format, and the day they got their first clone
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (id TEXT PRIMARY KEY, title TEXT, cid TEXT, channel TEXT, lang TEXT, niche TEXT,
    published TEXT, dur INTEGER, first_seen TEXT, breakout_first TEXT);
CREATE TABLE IF NOT EXISTS snaps (id TEXT, day TEXT, views INTEGER, PRIMARY KEY (id, day));
CREATE TABLE IF NOT EXISTS channels (cid TEXT PRIMARY KEY, title TEXT, created TEXT, subs INTEGER, videos INTEGER,
    first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS clusters (key TEXT PRIMARY KEY, lang TEXT, shingle TEXT, first_seen TEXT, last_seen TEXT,
    first_video TEXT);
CREATE TABLE IF NOT EXISTS cluster_days (key TEXT, day TEXT, stage TEXT, channels INTEGER, new_channels INTEGER,
    videos INTEGER, supply_channels INTEGER, med_vpd INTEGER, hit_rate REAL, copies14 INTEGER, copy_med INTEGER,
    ids TEXT, PRIMARY KEY (key, day));
CREATE TABLE IF NOT EXISTS niche_days (niche TEXT, lang TEXT, day TEXT, status TEXT, videos INTEGER, channels INTEGER,
    top10_vpd INTEGER, med_vpd INTEGER, small_n INTEGER, small_med_vpd INTEGER, small_hit_rate REAL,
    breakout_channels INTEGER, new_breakout_channels INTEGER, fill_per_day REAL, newest_med INTEGER,
    sat_index INTEGER, PRIMARY KEY (niche, lang, day));
CREATE TABLE IF NOT EXISTS fresh (niche TEXT, lang TEXT, id TEXT, day TEXT, PRIMARY KEY (niche, lang, id));
CREATE TABLE IF NOT EXISTS lonely (id TEXT PRIMARY KEY, lang TEXT, skeleton TEXT, first_seen TEXT, joined TEXT);
"""
TABLES = ("videos", "channels", "snaps", "clusters", "cluster_days", "niche_days", "fresh", "lonely")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and create if needed) a radar database for writing."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing radar database read-only (reports never modify it)."""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"no database at {p}")
    return sqlite3.connect(f"file:{p.resolve()}?mode=ro", uri=True)


# ---------------------------------------------------------------- videos, channels, snapshots
def recent_breakouts(conn: sqlite3.Connection, since: str) -> list[str]:
    """Videos that first broke out on or after `since`: re-measured daily even if search stops returning them."""
    return [r[0] for r in conn.execute("SELECT id FROM videos WHERE breakout_first >= ?", (since,))]


def prev_snapshot(conn: sqlite3.Connection, vid: str, before: str) -> tuple[str, int] | None:
    """The latest (day, views) for a video before `before`."""
    return conn.execute("SELECT day, views FROM snaps WHERE id=? AND day<? ORDER BY day DESC LIMIT 1",
                        (vid, before)).fetchone()


def save_snapshot(conn: sqlite3.Connection, vid: str, day: str, views: int) -> None:
    conn.execute("INSERT OR REPLACE INTO snaps VALUES (?,?,?)", (vid, day, views))


def save_video(conn: sqlite3.Connection, v: dict, day: str) -> None:
    """Insert once; first_seen keeps the first day the radar kept this video."""
    conn.execute("INSERT OR IGNORE INTO videos VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                 (v["id"], v["title"], v["cid"], v["channel"], v["rlang"], v["niche"], v["published"], v["dur"], day))


def mark_breakout(conn: sqlite3.Connection, vid: str, day: str) -> None:
    conn.execute("UPDATE videos SET breakout_first=COALESCE(breakout_first, ?) WHERE id=?", (day, vid))


def save_channel(conn: sqlite3.Connection, cid: str, ch: dict, day: str) -> None:
    """Upsert: keep first_seen, refresh subscribers, video count and last_seen."""
    conn.execute("INSERT INTO channels VALUES (?,?,?,?,?,?,?) ON CONFLICT(cid) DO UPDATE SET subs=excluded.subs, "
                 "videos=excluded.videos, last_seen=excluded.last_seen",
                 (cid, ch["title"], ch["created"], ch["subs"], ch["videos"], day, day))


# ---------------------------------------------------------------- clusters
def cluster_first_seen(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT first_seen FROM clusters WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def cluster_channels_on_or_before(conn: sqlite3.Connection, key: str, day: str) -> int | None:
    """A cluster's breakout-channel count on the latest day on or before `day` (for week-on-week growth)."""
    row = conn.execute("SELECT channels FROM cluster_days WHERE key=? AND day<=? ORDER BY day DESC LIMIT 1",
                       (key, day)).fetchone()
    return row[0] if row else None


def save_cluster_day(conn: sqlite3.Connection, cl: dict, day: str) -> None:
    """Register the cluster (first sighting wins) and write today's metrics. `first_video` holds the publish
    date of the oldest video in the format."""
    conn.execute("INSERT OR IGNORE INTO clusters VALUES (?,?,?,?,?,?)",
                 (cl["key"], cl["lang"], cl["shingle"], day, day, cl["first_pub"]))
    conn.execute("UPDATE clusters SET last_seen=? WHERE key=?", (day, cl["key"]))
    conn.execute("INSERT OR REPLACE INTO cluster_days VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (cl["key"], day, cl["stage"], cl["channels"], cl["new_channels"], len(cl["bos"]),
                  cl["supply_channels"], cl["med_vpd"], cl["hit_rate"], cl["copies14"], cl["copy_med"],
                  json.dumps([v["id"] for v in cl["bos"]][:40])))


def open_lonely(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT id FROM lonely WHERE joined IS NULL")]


def join_lonely(conn: sqlite3.Connection, vid: str, day: str) -> None:
    conn.execute("UPDATE lonely SET joined=? WHERE id=?", (day, vid))


def save_lonely(conn: sqlite3.Connection, v: dict, day: str) -> None:
    conn.execute("INSERT OR IGNORE INTO lonely VALUES (?,?,?,?,NULL)", (v["id"], v["rlang"], v["skeleton"], day))


# ---------------------------------------------------------------- fresh sample, niche board
def save_fresh(conn: sqlite3.Connection, niche: str, lang: str, vid: str, day: str) -> None:
    conn.execute("INSERT OR IGNORE INTO fresh VALUES (?,?,?,?)", (niche, lang, vid, day))


def fresh_between(conn: sqlite3.Connection, lo: str, hi: str) -> list[tuple[str, str, str]]:
    """(niche, lang, id) of the fresh sample taken between two days, inclusive."""
    return conn.execute("SELECT niche, lang, id FROM fresh WHERE day BETWEEN ? AND ?", (lo, hi)).fetchall()


def prev_niche(conn: sqlite3.Connection, niche: str, lang: str, before: str) -> tuple[str, int] | None:
    return conn.execute("SELECT status, new_breakout_channels FROM niche_days WHERE niche=? AND lang=? AND day<? "
                        "ORDER BY day DESC LIMIT 1", (niche, lang, before)).fetchone()


def save_niche_day(conn: sqlite3.Connection, r: dict, day: str) -> None:
    conn.execute("INSERT OR REPLACE INTO niche_days VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (r["niche"], r["lang"], day, r["status"], r["videos"], r["channels"], r["top10_vpd"], r["med_vpd"],
                  r["small_n"], r["small_med_vpd"], r["small_hit_rate"], r["breakout_channels"],
                  r["new_breakout_channels"], r["fill_per_day"], r["newest_med"], r["sat_index"]))


def history_days(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(DISTINCT day) FROM niche_days").fetchone()[0]
