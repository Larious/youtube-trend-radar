"""End to end, offline: the real scan replayed for five days against the synthetic YouTube in radar.offline."""

import sqlite3

import pytest

from radar import cli, report, store
from radar.offline import replay


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("replay")
    summaries = replay(tmp / "radar.sqlite", tmp / "runs", days=5, quiet=True)
    return tmp, summaries


def test_the_planted_signals_are_found(replayed):
    tmp, summaries = replayed
    last = summaries[-1]["day"]
    conn = sqlite3.connect(tmp / "radar.sqlite")
    stages = dict(conn.execute("SELECT c.shingle, cd.stage FROM cluster_days cd JOIN clusters c ON c.key = cd.key "
                               "WHERE cd.day = ?", (last,)).fetchall())
    assert stages == {"was banned in": "MID", "the untold story": "ESTABLISHED", "keep you poor": "SATURATED",
                      "if the earth": "EARLY", "esta prohibido en": "EARLY"}
    assert summaries[-1]["joined_today"] == 1        # the pre-wave breakout got its first clone on the last day
    statuses = {(n, lang): s for n, lang, s in conn.execute("SELECT niche, lang, status FROM niche_days WHERE day = ?",
                                                            (last,))}
    assert statuses[("money", "en")] == "SATURATED" and statuses[("money", "es")] == "COLD"


def test_the_wave_moves_from_early_to_mid(replayed):
    tmp, _ = replayed
    conn = sqlite3.connect(tmp / "radar.sqlite")
    stages = [s for (s,) in conn.execute("SELECT stage FROM cluster_days WHERE key = 'en|was banned in' ORDER BY day")]
    assert stages[0] == "EARLY" and stages[-1] == "MID"


def test_the_filters_drop_live_kids_gameplay_and_wrong_language_videos(replayed):
    tmp, _ = replayed
    conn = sqlite3.connect(tmp / "radar.sqlite")
    titles = {t for (t,) in conn.execute("SELECT title FROM videos")}
    assert not titles & {"LIVE: Ask a Historian Anything", "Counting Coins for Kids",
                         "Space Engine Gameplay: Flying to Titan", "The History of Spain in Ten Minutes"}
    assert conn.execute("SELECT MIN(dur) FROM videos").fetchone()[0] >= 6 * 60


def test_offline_reports_run_on_the_replayed_database(replayed, capsys):
    tmp, _ = replayed
    conn = store.connect_readonly(tmp / "radar.sqlite")
    assert "## Niche board" in report.daily(conn)
    assert "| MID |" in report.history(conn, "banned")
    bias = report.sampling_bias(conn, {"en": {"min_views": 20_000, "min_vpd": 3_000},
                                       "es": {"min_views": 10_000, "min_vpd": 1_500}})
    assert bias["views_sorted"]["hit_rate"] > 3 * bias["fresh"]["hit_rate"]   # the views-sorted sample flatters
    cli.main(["stats", "--db", str(tmp / "radar.sqlite")])
    assert "| snaps |" in capsys.readouterr().out
    assert (tmp / "runs" / "LATEST.md").read_text(encoding="utf-8").startswith("# Daily wave radar")


def test_the_bundled_sample_database_opens():
    conn = store.connect_readonly(cli.SAMPLE_DB)
    assert "## Format waves" in report.daily(conn)
