"""Read-only views of a radar database: the daily report, a cluster's history, table stats, and the
sampling-bias check. None of them touch the network, so they all run on the bundled sample database."""

from __future__ import annotations

import sqlite3
import statistics

from . import signals, store
from .brief import cell, fi

STAGE_ORDER = {s: i for i, s in enumerate(("EARLY", "MID", "ESTABLISHED", "CROWDED", "SATURATED", "UNPROBED"))}
STATUS_ORDER = {s: i for i, s in enumerate(signals.STATUSES)}

# Niche board for one day, with yesterday's status from a window function.
NICHE_SQL = """
WITH board AS (
    SELECT *, LAG(status) OVER (PARTITION BY niche, lang ORDER BY day) AS prev_status
    FROM niche_days
)
SELECT status, prev_status, niche, lang, videos, channels, top10_vpd, small_hit_rate,
       breakout_channels, new_breakout_channels, fill_per_day, newest_med, sat_index
FROM board
WHERE day = :day
"""

# Staged clusters for one day, with the breakout-channel count a week earlier.
WAVES_SQL = """
SELECT cd.key, cd.stage, c.lang, c.shingle, cd.channels, cd.new_channels,
       cd.channels - (SELECT w.channels FROM cluster_days w
                      WHERE w.key = cd.key AND w.day <= date(:day, '-7 days')
                      ORDER BY w.day DESC LIMIT 1) AS growth_wk,
       cd.supply_channels, cd.hit_rate, cd.med_vpd, cd.copies14, cd.copy_med, c.first_seen
FROM cluster_days cd
JOIN clusters c ON c.key = cd.key
WHERE cd.day = :day AND cd.stage != 'UNPROBED'
"""

# Each cluster's best breakout so far: unnest the stored ID list, join the latest snapshot on or before the day.
TOP_EXAMPLE_SQL = """
WITH latest AS (
    SELECT id, views, ROW_NUMBER() OVER (PARTITION BY id ORDER BY day DESC) AS rn
    FROM snaps WHERE day <= :day
),
members AS (
    SELECT cd.key, v.id, v.title, ch.title AS channel, ch.subs, l.views,
           ROW_NUMBER() OVER (PARTITION BY cd.key ORDER BY l.views DESC) AS rk
    FROM cluster_days cd, json_each(cd.ids) AS j
    JOIN videos v ON v.id = j.value
    LEFT JOIN channels ch ON ch.cid = v.cid
    JOIN latest l ON l.id = v.id AND l.rn = 1
    WHERE cd.day = :day
)
SELECT key, id, title, channel, subs, views FROM members WHERE rk = 1
"""

# Breakouts ranked by current velocity: views gained since the previous snapshot, per day.
RISERS_SQL = """
WITH velocity AS (
    SELECT id, day, views,
           views - LAG(views) OVER w AS gained,
           julianday(day) - julianday(LAG(day) OVER w) AS gap_days
    FROM snaps
    WINDOW w AS (PARTITION BY id ORDER BY day)
)
SELECT v.id, v.title, v.lang, v.niche, ch.title AS channel, ch.subs, s.views,
       CAST(s.gained / s.gap_days AS INTEGER) AS vpd_now, v.breakout_first
FROM velocity s
JOIN videos v ON v.id = s.id
LEFT JOIN channels ch ON ch.cid = v.cid
WHERE s.day = :day AND v.breakout_first IS NOT NULL AND s.gained IS NOT NULL
ORDER BY vpd_now DESC
LIMIT :limit
"""

PREWAVE_SQL = """
SELECT l.id, l.lang, l.first_seen, l.joined, v.title, v.niche, ch.title AS channel, ch.subs,
       (SELECT views FROM snaps WHERE id = l.id AND day <= :day ORDER BY day DESC LIMIT 1) AS views
FROM lonely l
JOIN videos v ON v.id = l.id
LEFT JOIN channels ch ON ch.cid = v.cid
WHERE l.first_seen <= :day AND (l.joined IS NULL OR l.joined >= :day)
ORDER BY (l.joined IS NOT NULL AND l.joined = :day) DESC, views DESC
LIMIT 15
"""

# Small-channel videos measured once at 2-7 days old: the latest snapshot inside that age window per video,
# tagged by whether the video came in through the fresh 'uploaded today' sample.
BIAS_SQL = """
WITH measured AS (
    SELECT v.id, v.lang, v.niche, s.views, ch.subs,
           CAST(julianday(s.day) - julianday(substr(v.published, 1, 10)) AS INTEGER) AS age,
           f.id IS NOT NULL AS in_fresh,
           ROW_NUMBER() OVER (PARTITION BY v.id ORDER BY s.day DESC) AS rn
    FROM videos v
    JOIN snaps s ON s.id = v.id
    JOIN channels ch ON ch.cid = v.cid
    LEFT JOIN (SELECT DISTINCT id FROM fresh) f ON f.id = v.id
    WHERE ch.subs < :small_subs
      AND julianday(s.day) - julianday(substr(v.published, 1, 10)) BETWEEN :lo AND :hi
)
SELECT in_fresh, lang, niche, views, subs, age FROM measured WHERE rn = 1
"""


def latest_day(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MAX(day) FROM niche_days").fetchone()[0]


def _rows(conn: sqlite3.Connection, sql: str, **params) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.row_factory = None


def daily(conn: sqlite3.Connection, day: str | None = None) -> str:
    """The day's radar rebuilt from stored facts: niche board, staged waves, risers and pre-wave breakouts."""
    day = day or latest_day(conn)
    if not day:
        return "The database has no scans yet.\n"
    snaps, first_seen, new_bo, fresh, hist = conn.execute(
        "SELECT (SELECT COUNT(*) FROM snaps WHERE day = :d), (SELECT COUNT(*) FROM videos WHERE first_seen = :d), "
        "(SELECT COUNT(*) FROM videos WHERE breakout_first = :d), (SELECT COUNT(*) FROM fresh WHERE day = :d), "
        "(SELECT COUNT(DISTINCT day) FROM snaps WHERE day <= :d)", {"d": day}).fetchone()
    L = [f"# Radar report — {day}\n",
         f"Rebuilt from the database: {snaps:,} view snapshots taken this day · {first_seen:,} videos seen for the "
         f"first time · {new_bo:,} new breakouts · {fresh:,} uploads added to the fresh sample · {hist} day(s) of "
         "history.\n",
         "These are radar signals, not verdicts.\n"]

    niches = sorted(_rows(conn, NICHE_SQL, day=day),
                    key=lambda r: (r["lang"] != "en", r["lang"], STATUS_ORDER.get(r["status"], 99), -r["top10_vpd"]))
    L += ["## Niche board\n",
          "| Status | Was | Niche | Lang | Videos | Top-10 v/day | Small hit rate | Breakout ch (new) | Fill/day | "
          "Fresh median | Sat. index |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in niches:
        L.append(f"| {r['status']} | {r['prev_status'] or '—'} | {r['niche']} | {r['lang']} | {r['videos']} | "
                 f"{fi(r['top10_vpd'])} | {r['small_hit_rate']:.0%} | {r['breakout_channels']} "
                 f"({r['new_breakout_channels']}) | {fi(r['fill_per_day'])} | {fi(r['newest_med'])} | "
                 f"{r['sat_index']} |")

    tops = {r["key"]: r for r in _rows(conn, TOP_EXAMPLE_SQL, day=day)}
    waves = sorted(_rows(conn, WAVES_SQL, day=day), key=lambda r: (STAGE_ORDER.get(r["stage"], 99), -r["channels"]))
    L += ["\n## Format waves (probed clusters)\n",
          "| Stage | Lang | Format skeleton | Breakout ch (new) | Δ ch/wk | Supply ch | Small hit rate | "
          "Median v/day | Copies 14 d | Copy median | First seen | Top example |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in waves:
        t = tops.get(r["key"])
        example = (f"{cell(t['title'], 60)} ({fi(t['views'])} views, {cell(t['channel'], 20)}, {fi(t['subs'])} subs)"
                   if t else "")
        L.append(f"| {r['stage']} | {r['lang']} | `{r['shingle']}` | {r['channels']} ({r['new_channels']}) | "
                 f"{fi(r['growth_wk'])} | {r['supply_channels']} | {r['hit_rate']:.0%} | {fi(r['med_vpd'])} | "
                 f"{fi(r['copies14'])} | {fi(r['copy_med'])} | {r['first_seen']} | {example} |")

    L += ["\n## Breakouts by current velocity (views/day since the previous snapshot)\n",
          "| Now v/day | Views | Lang | Niche | Channel | Subs | Broke out | Title |",
          "|---|---|---|---|---|---|---|---|"]
    for r in _rows(conn, RISERS_SQL, day=day, limit=15):
        L.append(f"| {fi(r['vpd_now'])} | {fi(r['views'])} | {r['lang']} | {r['niche']} | {cell(r['channel'], 22)} | "
                 f"{fi(r['subs'])} | {r['breakout_first']} | {cell(r['title'], 70)} |")

    L += ["\n## Pre-wave breakouts (new channels, no matching format yet)\n",
          "| | First seen | Lang | Niche | Views | Channel | Subs | Title |", "|---|---|---|---|---|---|---|---|"]
    for r in _rows(conn, PREWAVE_SQL, day=day):
        flag = "⚡ first clone today" if r["joined"] == day else ""
        L.append(f"| {flag} | {r['first_seen']} | {r['lang']} | {r['niche']} | {fi(r['views'])} | "
                 f"{cell(r['channel'], 22)} | {fi(r['subs'])} | {cell(r['title'], 70)} |")
    return "\n".join(L) + "\n"


def history(conn: sqlite3.Connection, words: str) -> str:
    """Day-by-day metrics for clusters whose skeleton contains `words`."""
    keys = conn.execute("SELECT key FROM clusters WHERE shingle LIKE ? ORDER BY first_seen, key LIMIT 10",
                        (f"%{words}%",)).fetchall()
    if not keys:
        return f"No cluster skeleton contains '{words}'.\n"
    L = []
    for (key,) in keys:
        L += [f"## {key}\n", "| Day | Stage | Breakout ch (new) | Breakout videos | Supply ch | Median v/day | "
              "Small hit rate | Copies 14 d | Copy median |", "|---|---|---|---|---|---|---|---|---|"]
        for d, stage, ch, new, vids, supply, med, hit, copies, copy_med in conn.execute(
                "SELECT day, stage, channels, new_channels, videos, supply_channels, med_vpd, hit_rate, copies14, "
                "copy_med FROM cluster_days WHERE key=? ORDER BY day", (key,)):
            L.append(f"| {d} | {stage} | {ch} ({new}) | {vids} | {supply} | {fi(med)} | {hit:.0%} | {fi(copies)} | "
                     f"{fi(copy_med)} |")
        L.append("")
    return "\n".join(L)


def stats(conn: sqlite3.Connection) -> str:
    L = ["| Table | Rows |", "|---|---|"]
    for t in store.TABLES:
        L.append(f"| {t} | {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:,} |")
    lo, hi, n = conn.execute("SELECT MIN(day), MAX(day), COUNT(DISTINCT day) FROM snaps").fetchone()
    L.append(f"\nSnapshots cover {n} day(s): {lo} to {hi}.")
    return "\n".join(L) + "\n"


def sampling_bias(conn: sqlite3.Connection, bars: dict, lo: int = 2, hi: int = 7, min_group: int = 20) -> dict:
    """How often small-channel videos clear the breakout bar in the views-sorted searches versus the fresh
    'uploaded today' sample, each video measured once at lo-hi days old (calendar days)."""
    samples: dict[str, list[tuple]] = {"views_sorted": [], "fresh": []}
    for r in _rows(conn, BIAS_SQL, small_subs=signals.SMALL_SUBS, lo=lo, hi=hi):
        bar = bars.get(r["lang"], bars["en"])
        hit = signals.classify_video(views=r["views"], age=max(r["age"], 0.5), subs=r["subs"], channel_age=None,
                                     bar=bar)["breakout"]
        samples["fresh" if r["in_fresh"] else "views_sorted"].append((r["niche"], r["lang"], r["views"], hit))
    out = {}
    for name, rows in samples.items():
        groups: dict[tuple, list[bool]] = {}
        for niche, lang, _, hit in rows:
            groups.setdefault((niche, lang), []).append(hit)
        rates = sorted(sum(g) / len(g) for g in groups.values() if len(g) >= min_group)
        out[name] = {
            "videos": len(rows), "hits": sum(r[3] for r in rows),
            "hit_rate": round(sum(r[3] for r in rows) / len(rows), 3) if rows else None,
            "median_views": int(statistics.median(r[2] for r in rows)) if rows else None,
            "groups": len(rates), "group_median": round(statistics.median(rates), 2) if rates else None,
            "group_min": round(rates[0], 2) if rates else None, "group_max": round(rates[-1], 2) if rates else None,
            "groups_60pct_plus": round(sum(x >= 0.6 for x in rates) / len(rates), 2) if rates else None}
    out["window_days"], out["min_group"] = (lo, hi), min_group
    return out


def format_bias(b: dict) -> str:
    lo, hi = b["window_days"]
    pct = lambda x: "–" if x is None else f"{x:.0%}"   # noqa: E731
    L = [f"Small-channel videos (<{signals.SMALL_SUBS:,} subs), each measured once at {lo}–{hi} days old.\n",
         "| Sample | Videos | Clear the breakout bar | Median views |", "|---|---|---|---|"]
    for name, label in (("views_sorted", "Views-sorted searches"), ("fresh", "'Uploaded today' sample")):
        s = b[name]
        L.append(f"| {label} | {s['videos']:,} | {pct(s['hit_rate'])} | {fi(s['median_views'])} |")
    L.append(f"\nPer niche × language (groups with {b['min_group']}+ videos):")
    for name, label in (("views_sorted", "views-sorted"), ("fresh", "fresh sample")):
        s = b[name]
        if s["groups"]:
            L.append(f"- {label}: {s['groups']} groups, median hit rate {pct(s['group_median'])} "
                     f"(range {pct(s['group_min'])}–{pct(s['group_max'])}), {pct(s['groups_60pct_plus'])} of groups "
                     "at 60% or more")
        else:
            L.append(f"- {label}: no group is large enough")
    return "\n".join(L) + "\n"
