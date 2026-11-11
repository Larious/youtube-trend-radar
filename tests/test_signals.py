import datetime as dt

import pytest

from radar import signals

BAR = {"min_views": 20_000, "min_vpd": 3_000}


def test_breakout_is_a_small_channel_outperforming_its_size():
    s = signals.classify_video(views=60_000, age=4, subs=8_000, channel_age=900, bar=BAR)
    assert s["small"] and not s["new"] and s["breakout"]
    assert s["vpd"] == 15_000 and s["outlier"] == 7.5


@pytest.mark.parametrize("views, age, subs, channel_age", [
    (60_000, 4, 900_000, 900),    # big, established channel
    (600_000, 45, 8_000, 900),    # too old
    (30_000, 2, 45_000, 900),     # fewer views than subscribers
    (15_000, 1, 2_000, 900),      # under the views bar
])
def test_not_breakouts(views, age, subs, channel_age):
    assert not signals.classify_video(views=views, age=age, subs=subs, channel_age=channel_age, bar=BAR)["breakout"]


def test_a_new_channel_counts_even_when_it_is_not_small():
    s = signals.classify_video(views=200_000, age=5, subs=120_000, channel_age=60, bar=BAR)
    assert s["new"] and not s["small"] and s["breakout"]


STAGE = dict(probed=True, copies14=4, copy_med=50_000, old_hits=0, supply_channels=5, hit_rate=0.6, format_age=10,
             channels=3, min_views=20_000)


@pytest.mark.parametrize("change, stage", [
    ({}, "EARLY"),
    ({"channels": 10}, "MID"),
    ({"copies14": 12}, "MID"),                                              # many copies, and they still pay
    ({"copies14": 12, "copy_med": 3_000}, "SATURATED"),                     # many copies, little for each
    ({"old_hits": 3, "supply_channels": 15, "hit_rate": 0.1}, "SATURATED"),  # old format, few small winners
    ({"old_hits": 3}, "ESTABLISHED"),
    ({"format_age": 120, "hit_rate": 0.1}, "CROWDED"),
    ({"probed": False}, "UNPROBED"),
])
def test_wave_stage_labels(change, stage):
    assert signals.wave_stage(**{**STAGE, **change}) == stage


NICHE = dict(top10_vpd=50_000, min_vpd=3_000, newest_med=2_000, sat_index=50, breakout_rank=50, demand_rank=50,
             new_breakout_channels=1)


@pytest.mark.parametrize("change, status", [
    ({}, "COMPETITIVE"),
    ({"top10_vpd": 2_000}, "COLD"),
    ({"newest_med": None}, signals.PENDING),
    ({"newest_med": None, "breakout_rank": 80}, "BREAKING OUT"),
    ({"breakout_rank": 100, "sat_index": 40}, "BREAKING OUT"),
    ({"breakout_rank": 100, "sat_index": 70}, "SATURATED"),
    ({"sat_index": 30, "demand_rank": 40}, "UNSATURATED"),
    ({"sat_index": 30, "demand_rank": 20}, "COMPETITIVE"),
    ({"new_breakout_channels": 2}, "CROWDED-BUT-LIVE"),
])
def test_niche_status_labels(change, status):
    assert signals.niche_status(**{**NICHE, **change}) == status


def test_rank_and_saturation_index():
    rank = signals.rank_pct([10, 20, 30, None])
    assert [rank(10), rank(20), rank(30), rank(None)] == [0, 50, 100, None]
    assert signals.saturation_index(fill_rank=100, fresh_rank=0) == 100    # most supply, least for each upload
    assert signals.saturation_index(fill_rank=0, fresh_rank=100) == 0
    assert signals.saturation_index(None, None) == 50


def test_fill_rate_is_uploads_per_day_on_a_newest_first_page():
    now = dt.datetime(2026, 11, 5, 12, tzinfo=dt.timezone.utc)
    page = [{"published": (now - dt.timedelta(hours=h)).isoformat(), "views": 10} for h in (1, 4, 7, 10, 13)]
    assert signals.fill_rate(page, now) == (10.0, None)    # 5 uploads in 12 hours; none is 2 days old yet
    assert signals.fill_rate(page[:4], now) == (None, None)


def _video(n, title, cid, breakout=True, new=False):
    return dict(id=f"v{n}", title=title, cid=cid, published=f"2026-11-0{n}T00:00:00Z", vpd=10_000, breakout=breakout,
                new=new, small=True)


def test_clusters_group_one_format_across_channels():
    videos = [_video(1, "Why Chess Was Banned in 1920", "c1"),
              _video(2, "Why Jazz Was Banned in 1931", "c2", new=True),
              _video(3, "Why Kites Was Banned in 1950", "c3", breakout=False),
              _video(4, "The Shepherd Who Mapped a Kingdom", "c4", new=True)]
    clusters, lonely = signals.cluster_language(videos, "en")
    assert [c["shingle"] for c in clusters] == ["was banned in"]     # the most specific of the overlapping 3-grams
    c = clusters[0]
    assert (c["channels"], c["new_channels"], c["supply_channels"], c["hit_rate"]) == (2, 1, 3, 0.67)
    assert videos[0]["skeleton"] == "why X was banned in N"
    assert [v["id"] for v in lonely] == ["v4"]     # a new-channel breakout that matches no format yet


def test_one_channel_is_not_a_wave():
    videos = [_video(1, "Why Chess Was Banned in 1920", "c1"), _video(2, "Why Jazz Was Banned in 1931", "c1"),
              _video(3, "Why Kites Was Banned in 1950", "c1")]
    assert signals.cluster_language(videos, "en")[0] == []
