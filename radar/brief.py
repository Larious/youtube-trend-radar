"""The scan's outputs for one day: brief.md (the human read), signals.json, and CSVs of breakouts, clusters
and niches. LATEST.md in the output root is always the newest brief."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from . import signals, store

NICHE_ORDER = list(signals.STATUSES)
BREAKOUT_COLS = ["id", "title", "channel", "cid", "rlang", "niche", "views", "vpd", "vpd_now", "age", "subs", "born",
                 "new", "outlier", "published", "dur", "skeleton"]
CLUSTER_COLS = ["stage", "lang", "shingle", "channels", "new_channels", "growth_wk", "supply_channels", "hit_rate",
                "med_vpd", "copies14", "copy_med", "fill", "old_hits", "format_age", "first_pub", "last_bo", "is_new"]
NICHE_COLS = ["status", "prev_status", "niche", "lang", "videos", "channels", "top10_vpd", "med_vpd", "small_n",
              "small_med_vpd", "small_hit_rate", "breakout_channels", "new_breakout_channels", "fill_per_day",
              "newest_med", "fresh_win", "sat_index"]


def fi(n) -> str:
    """Integer with thousands separators; an en dash for missing."""
    return "–" if n is None else f"{int(round(n)):,}"


def cell(s, n: int = 70) -> str:
    """Safe for a Markdown table cell, cut to n characters."""
    s = re.sub(r"\s+", " ", str(s or "")).replace("|", "/").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def vlink(v: dict) -> str:
    return f"[{cell(v['title'], 80)}](https://youtu.be/{v['id']})"


def write_outputs(out_root: Path, clock, searcher, api, db, kept: dict, clusters: list[dict], lonely: list[dict],
                  joined_today: list[tuple], nrows: list[dict], extra: list[dict]) -> dict:
    today = clock.today
    out = Path(out_root) / today
    out.mkdir(parents=True, exist_ok=True)
    bos = sorted((v for v in kept.values() if v["breakout"]), key=lambda v: -v["vpd"])
    with open(out / "breakouts.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BREAKOUT_COLS, extrasaction="ignore")
        w.writeheader()
        for v in bos:
            w.writerow({**v, "age": round(v["age"], 1)})
    with open(out / "clusters.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CLUSTER_COLS + ["examples"], extrasaction="ignore")
        w.writeheader()
        for cl in clusters:
            w.writerow({**cl, "examples": " || ".join(f"{v['title'][:70]} ({v['views']:,}, {v['channel'][:20]})"
                                                       for v in sorted(cl["bos"], key=lambda v: -v["views"])[:4])})
    nrows.sort(key=lambda r: (r["lang"] != "en", r["lang"], NICHE_ORDER.index(r["status"]), -r["top10_vpd"]))
    with open(out / "niches.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=NICHE_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(nrows)

    def top_videos(cl: dict) -> list[dict]:
        return [dict(id=v["id"], title=v["title"], views=v["views"], vpd=v["vpd"], channel=v["channel"],
                     subs=v["subs"], born=v["born"]) for v in sorted(cl["bos"], key=lambda v: -v["vpd"])[:5]]

    signals_json = {
        "date": today, "niches": nrows,
        "waves": [{k: cl.get(k) for k in CLUSTER_COLS} | {"top": top_videos(cl)} for cl in clusters[:60]],
        "pre_wave": [dict(id=v["id"], title=v["title"], views=v["views"], vpd=v["vpd"], channel=v["channel"],
                          subs=v["subs"], born=v["born"], lang=v["rlang"], niche=v["niche"])
                     for v in sorted(lonely, key=lambda v: -v["vpd"])[:40]],
        "joined_today": [dict(id=vid, cluster=cl["shingle"], lang=cl["lang"]) for vid, cl in joined_today]}
    (out / "signals.json").write_text(json.dumps(signals_json, indent=1, ensure_ascii=False), encoding="utf-8")
    errors = [lg for lg in searcher.log if lg["error"]]
    (out / "search_errors.json").write_text(json.dumps(errors, indent=1, ensure_ascii=False), encoding="utf-8")

    units = sum(api.units.values())
    unit_detail = ", ".join(f"{ep} {n}" for ep, n in sorted(api.units.items()))
    from_cache = sum(1 for lg in searcher.log if lg["cached"])
    hist = store.history_days(db)
    L = [f"# Daily wave radar — {today}\n",
         f"{len(searcher.log)} searches ({from_cache} from cache, {len(errors)} errors) · {len(kept):,} long-form "
         f"videos ≤60 d · {len(bos):,} breakouts (small <50k subs or channel ≤6 mo, ≤30 d old, over the language's "
         f"views and views/day bar) · {len(clusters)} format clusters · API units {units}"
         f"{f' ({unit_detail})' if unit_detail else ''} · extra IDs {len(extra)} · history: {hist} day(s)"
         f"{' — growth columns fill in from day 8' if hist < 8 else ''}\n",
         "Everything below is a **radar signal**, not a verdict: check a format by hand (and with a fuller "
         "census) before acting on it.\n"]
    if joined_today:
        L += ["## ⚡ Pre-wave breakouts that got their first clone today\n"]
        for vid, cl in joined_today:
            L.append(f"- `{cl['lang']}` **{cl['shingle']}** — https://youtu.be/{vid}")
        L.append("")

    def wave_table(rows: list[dict], title: str) -> None:
        L.extend([f"## {title}\n",
                  "| Stage | Lang | Format skeleton | Breakout ch (new) | Δ ch/wk | Supply ch | Small hit rate | "
                  "Median v/day | Copies 14 d | Copy median | Old hits | Format age d | Top example |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"])
        for cl in rows:
            top = max(cl["bos"], key=lambda v: v["vpd"])
            L.append(f"| {cl['stage']}{' 🆕' if cl['is_new'] else ''} | {cl['lang']} | `{cl['shingle']}` | "
                     f"{cl['channels']} ({cl['new_channels']}) | {fi(cl['growth_wk'])} | {cl['supply_channels']} | "
                     f"{cl['hit_rate']:.0%} | {cl['med_vpd']:,} | {fi(cl['copies14'])} | {fi(cl['copy_med'])} | "
                     f"{fi(cl['old_hits'])} | {cl['format_age']} | "
                     f"{vlink(top)} ({top['views']:,}, {cell(top['channel'], 20)}, {fi(top['subs'])} subs) |")
        L.append("")

    by_stage = {s: [cl for cl in clusters if cl["stage"] == s] for s in signals.STAGES}
    wave_table(by_stage["EARLY"][:25], "Waves breaking out (EARLY: 2+ small/new channels outperforming their size, "
                                       "<3 in-format hits older than 60 days, <10 copies in 14 days)")
    wave_table(by_stage["MID"][:15], "Waves filling up (MID: enter only with a fresh subject)")
    wave_table(by_stage["ESTABLISHED"][:15], "Established formats still paying small channels")
    wave_table((by_stage["SATURATED"] + by_stage["CROWDED"])[:15], "Saturated / crowded formats")
    L += ["## Pre-wave: lonely breakouts on new channels (one proof, no clone yet)\n",
          "| Views | Views/day | Age d | Subs | Born | Lang | Niche | Channel | Title |",
          "|---|---|---|---|---|---|---|---|---|"]
    for v in sorted(lonely, key=lambda v: -v["vpd"])[:30]:
        L.append(f"| {v['views']:,} | {v['vpd']:,} | {v['age']:.0f} | {fi(v['subs'])} | {v['born']} | {v['rlang']} | "
                 f"{v['niche']} | {cell(v['channel'], 22)} | {vlink(v)} |")
    L += ["\n## Niche board (status is relative to the other niches in the same language today)\n",
          "Fill = uploads/day implied by YouTube's 'today, newest' page for the niche's first three seeds. "
          "Fresh median / fresh ≥1k = what the uploads sampled that way 2–7 days ago have got since (needs 2 days "
          "of history): an unbiased sample, unlike the by-views searches. Saturation index 0–100 = mean of the "
          "fill rank and (100 − fresh-median rank). BREAKING OUT = top-20% new-channel breakouts in its language "
          "and not saturated.\n",
          "| Status | Was | Niche | Lang | Videos ≤30 d | Channels | Top-10 v/day | Small-ch median v/day | "
          "Small hit rate | Breakout ch (new) | Fill/day | Fresh median | Fresh ≥1k | Sat. index |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in nrows:
        fresh_win = "–" if r["fresh_win"] is None else format(r["fresh_win"], ".0%")
        L.append(f"| {r['status']} | {r['prev_status'] or '—'} | {r['niche']} | {r['lang']} | {r['videos']} | "
                 f"{r['channels']} | {r['top10_vpd']:,} | {r['small_med_vpd']:,} | {r['small_hit_rate']:.0%} | "
                 f"{r['breakout_channels']} ({r['new_breakout_channels']}) | {fi(r['fill_per_day'])} | "
                 f"{fi(r['newest_med'])} | {fresh_win} | {r['sat_index']} |")
    L += ["\n## Top 40 breakouts by views/day\n",
          "| Views/day | Now v/day | Views | Age d | Subs | Born | Lang | Niche | Channel | Title |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for v in bos[:40]:
        L.append(f"| {v['vpd']:,} | {fi(v['vpd_now'])} | {v['views']:,} | {v['age']:.0f} | {fi(v['subs'])} | "
                 f"{v['born']} | {v['rlang']} | {v['niche']} | {cell(v['channel'], 22)} | {vlink(v)} |")
    L += [f"\nFiles in `{out}/`: signals.json, clusters.csv, niches.csv, breakouts.csv, search_errors.json."]
    body = "\n".join(L) + "\n"
    (out / "brief.md").write_text(body, encoding="utf-8")
    (Path(out_root) / "LATEST.md").write_text(body, encoding="utf-8")
    return {"day": today, "brief": str(out / "brief.md"), "breakouts": len(bos), "clusters": len(clusters),
            "early": len(by_stage["EARLY"]), "pre_wave": len(lonely), "niche_rows": len(nrows),
            "api_units": units, "searches": len(searcher.log), "search_errors": len(errors),
            "joined_today": len(joined_today), "kept": len(kept)}
