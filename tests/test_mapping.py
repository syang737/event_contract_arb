from datetime import datetime, timezone

from arb_engine.core.models import Exchange
from arb_engine.exchanges.mock_client import MockClient
from arb_engine.mapping.adjudicator import RuleAdjudicator
from arb_engine.mapping.blocking import generate_candidates, keywords
from arb_engine.mapping.models import MappingVerdict, VenueMarket
from arb_engine.mapping.scoring import score_pair
from arb_engine.mapping.store import MappingStore, TieringPolicy
from arb_engine.mapping.sync import sync_once
from arb_engine.storage.db import Database


def _dt(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


def _pm(venue_id, title, **kw):
    return VenueMarket(Exchange.POLYMARKET, venue_id, title, yes_token=f"{venue_id}_Y",
                       no_token=f"{venue_id}_N", **kw)


def _ka(venue_id, title, **kw):
    return VenueMarket(Exchange.KALSHI, venue_id, title, **kw)


# Aligned equivalent pair.
PM_DEM = _pm("0xDEM28", "Will a Democrat win the 2028 US presidential election?",
             category="politics", close_time=_dt(2028, 11, 7))
KA_DEM = _ka("PRES-2028-DEM", "2028 US presidential election Democratic winner",
             category="politics", close_time=_dt(2028, 11, 7))

# Inverted equivalent pair (Democrat-win vs Republican-win of the same race).
PM_DEM32 = _pm("0xDEM32", "Will a Democrat win the 2032 presidential election?",
               category="politics", close_time=_dt(2032, 11, 2))
KA_REP32 = _ka("PRES-2032-REP", "Will a Republican win the 2032 presidential election?",
               category="politics", close_time=_dt(2032, 11, 2))


# --------------------------------------------------------------------------- #
# Blocking
# --------------------------------------------------------------------------- #
def test_keywords_drop_stopwords_keep_digits():
    kw = keywords("Will a Democrat win the 2028 election?")
    assert "democrat" in kw and "2028" in kw and "election" in kw
    assert "the" not in kw and "will" not in kw


def test_blocking_pairs_related_markets():
    pairs = generate_candidates([PM_DEM], [KA_DEM])
    assert len(pairs) == 1
    assert pairs[0][0].venue_id == "0xDEM28" and pairs[0][1].venue_id == "PRES-2028-DEM"


def test_blocking_date_filter_excludes_far_dates():
    ka_far = _ka("PRES-2028-DEM", "2028 US presidential election Democratic winner",
                 category="politics", close_time=_dt(2029, 11, 7))  # ~1yr apart
    assert generate_candidates([PM_DEM], [ka_far]) == []


def test_blocking_strike_filter_excludes_mismatched_strikes():
    pm = _pm("0xSPX", "S&P 500 above threshold in 2026", category="economics",
             close_time=_dt(2026, 12, 31), strike=5000.0, strike_type="greater")
    ka = _ka("SPX-26", "S&P 500 above threshold 2026", category="economics",
             close_time=_dt(2026, 12, 31), strike=6000.0, strike_type="greater")
    assert generate_candidates([pm], [ka]) == []


# --------------------------------------------------------------------------- #
# Scoring + adjudication
# --------------------------------------------------------------------------- #
def test_scoring_aligned_pair_is_confident_and_aligned():
    f = score_pair(PM_DEM, KA_DEM)
    assert f.composite >= 0.6
    assert f.category_match is True
    assert f.close_delta_hours == 0.0
    assert f.pm_yes_equals_kalshi_yes is True


def test_scoring_detects_inverted_polarity():
    f = score_pair(PM_DEM32, KA_REP32)
    assert f.pm_yes_equals_kalshi_yes is False


def test_adjudicator_accepts_strong_pair():
    f = score_pair(PM_DEM, KA_DEM)
    v = RuleAdjudicator().judge(PM_DEM, KA_DEM, f)
    assert v.equivalent and v.pm_yes_equals_kalshi_yes


def test_adjudicator_rejects_strike_mismatch_guardrail():
    f = score_pair(PM_DEM, KA_DEM)
    f.strike_match = False  # force the hard guardrail
    v = RuleAdjudicator().judge(PM_DEM, KA_DEM, f)
    assert not v.equivalent and "strike" in v.reason


def test_adjudicator_rejects_low_composite():
    f = score_pair(PM_DEM, KA_DEM)
    v = RuleAdjudicator(min_confidence=0.99).judge(PM_DEM, KA_DEM, f)
    assert not v.equivalent


# --------------------------------------------------------------------------- #
# Store tiering
# --------------------------------------------------------------------------- #
def test_store_tiers_by_policy():
    db = Database("sqlite:///:memory:")
    f = score_pair(PM_DEM, KA_DEM)
    equivalent = MappingVerdict(True, True, 0.9, "ok")

    proposed_store = MappingStore(db, TieringPolicy(auto_accept=False))
    _id, status = proposed_store.record(PM_DEM, KA_DEM, equivalent, f)
    assert status == "proposed"

    auto_store = MappingStore(db, TieringPolicy(auto_accept=True, accept_threshold=0.8))
    _id, status = auto_store.record(PM_DEM, KA_DEM, equivalent, f)
    assert status == "active"  # same pair upgraded to active


# --------------------------------------------------------------------------- #
# Sync end-to-end
# --------------------------------------------------------------------------- #
async def test_sync_proposes_and_records_polarity():
    db = Database("sqlite:///:memory:")
    pm = MockClient(Exchange.POLYMARKET, catalog=[PM_DEM, PM_DEM32])
    ka = MockClient(Exchange.KALSHI, catalog=[KA_DEM, KA_REP32])

    report = await sync_once(db, pm, ka)
    assert report.candidates >= 2
    assert report.status_counts.get("proposed", 0) >= 2

    proposed = db.mappings("proposed")
    by_pair = {(m.pm_condition_id, m.ka_ticker): m for m in proposed}
    assert ("0xDEM28", "PRES-2028-DEM") in by_pair
    assert by_pair[("0xDEM28", "PRES-2028-DEM")].pm_yes_equals_kalshi_yes is True
    # Inverted pair recorded with polarity False.
    assert by_pair[("0xDEM32", "PRES-2032-REP")].pm_yes_equals_kalshi_yes is False


def test_venue_market_expiry_and_tradable():
    live = _pm("0xLIVE", "x", close_time=_dt(2099, 1, 1))
    dead = _pm("0xDEAD", "x", close_time=_dt(2000, 1, 1))
    assert not live.is_expired() and live.is_tradable()
    assert dead.is_expired() and not dead.is_tradable()
    # A terminal status is never tradable regardless of close time.
    settled = _pm("0xSET", "x", close_time=_dt(2099, 1, 1), status="settled")
    assert not settled.is_tradable()


async def test_sync_excludes_expired_markets():
    db = Database("sqlite:///:memory:")
    # Same subject on both venues but already past its close time -> not matched.
    pm_dead = _pm("0xOLD", "Will a Democrat win the 2020 election?", category="politics",
                  close_time=_dt(2020, 11, 3))
    ka_dead = _ka("PRES-2020", "2020 presidential election Democratic winner",
                  category="politics", close_time=_dt(2020, 11, 3))
    pm = MockClient(Exchange.POLYMARKET, catalog=[pm_dead])
    ka = MockClient(Exchange.KALSHI, catalog=[ka_dead])

    report = await sync_once(db, pm, ka, exclude_expired=True)
    assert report.pm_tradable == 0 and report.ka_tradable == 0
    assert report.candidates == 0
    # Turning the filter off lets the expired pair through as a candidate.
    report2 = await sync_once(db, pm, ka, exclude_expired=False)
    assert report2.candidates == 1


async def test_sync_reports_reject_reasons_and_near_misses():
    db = Database("sqlite:///:memory:")
    # Differently-worded but related -> scores below the strict threshold.
    pm = MockClient(Exchange.POLYMARKET, catalog=[
        _pm("0xTRUMP", "Will Donald Trump win the 2028 election?", category="politics",
            close_time=_dt(2028, 11, 7))])
    ka = MockClient(Exchange.KALSHI, catalog=[
        _ka("PRES-28", "2028 presidential election Trump victory", category="politics",
            close_time=_dt(2028, 11, 7))])

    report = await sync_once(db, pm, ka, adjudicator=RuleAdjudicator(min_confidence=0.9))
    assert report.status_counts.get("rejected") == 1
    assert report.reject_reasons.get("below_threshold") == 1
    assert report.near_misses and report.near_misses[0][1].startswith("composite=")


async def test_sync_retires_when_leg_delists():
    db = Database("sqlite:///:memory:")
    pm = MockClient(Exchange.POLYMARKET, catalog=[PM_DEM])
    ka = MockClient(Exchange.KALSHI, catalog=[KA_DEM])
    await sync_once(db, pm, ka)
    assert len(db.mappings("proposed")) == 1

    # The Polymarket leg settles -> no longer live -> mapping retires.
    closed = _pm("0xDEM28", PM_DEM.title, category="politics",
                 close_time=_dt(2028, 11, 7), status="closed")
    pm.set_catalog([closed])
    await sync_once(db, pm, ka)
    assert db.mappings("proposed") == []
    assert len(db.mappings("retired")) == 1
