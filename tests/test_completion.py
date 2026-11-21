from datetime import datetime, timedelta

from nse_scanner.decisions import build_board, coverage_is_complete, strategy_coverage
from nse_scanner.constituents import (
    active_nifty,
    apply_cash_validation,
    parse_constituent_csv,
    refresh_official_lists,
    save_constituent_cache,
    save_manual_nifty,
    load_constituent_cache,
)
from nse_scanner.engine import (
    choose_candidates,
    effective_reversal_protection,
    effective_setup3_inclusion,
    reversal_allows_signals,
)
from nse_scanner.groww_client import GrowwClient, classify_probe_failure
from nse_scanner.historical import build_validation_report
from nse_scanner.readiness import readiness
from nse_scanner.settings import load_settings
from scanner_rules import SECTOR_NAMES


def _csv(symbols):
    rows = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    rows.extend(f"Company,Industry,{symbol},EQ,INE000" for symbol in symbols)
    return "\n".join(rows)


def _fetcher(bodies):
    def fetch(url):
        if url not in bodies:
            return False, "HTTP 404"
        return True, bodies[url]
    return fetch


def _bodies(nifty_symbols):
    from nse_scanner.constituents import NIFTY50_URL, SECTOR_URLS
    bodies = {NIFTY50_URL: _csv(nifty_symbols)}
    for name, url in SECTOR_URLS.items():
        bodies[url] = _csv([f"{name[:4].upper().replace(' ', '')}{i}" for i in range(5)])
    return bodies


def test_official_nifty_list_is_cached_and_a_change_is_recorded():
    now = datetime(2026, 11, 21, 10, 0)
    first = [f"S{i:02d}" for i in range(50)]
    report = refresh_official_lists(now, fetcher=_fetcher(_bodies(first)), force=True)
    assert report["nifty50"]["usable"] is True
    assert report["nifty50"]["count"] == 50
    assert report["sectors"]["ready"] is True
    changed = first[:-1] + ["NEWCO"]
    later = refresh_official_lists(now + timedelta(hours=13), fetcher=_fetcher(_bodies(changed)), force=True)
    assert later["nifty50"]["added"] == ["NEWCO"]
    assert later["nifty50"]["removed"] == ["S49"]


def test_invalid_or_outdated_nifty_list_is_not_used():
    now = datetime(2026, 11, 21, 10, 0)
    symbols = [f"S{i:02d}" for i in range(50)]
    refresh_official_lists(now, fetcher=_fetcher(_bodies(symbols)), force=True)
    short = _bodies(symbols[:49])
    rejected = refresh_official_lists(now, fetcher=_fetcher(short), force=True)
    assert rejected["nifty50"]["usable"] is True
    assert rejected["nifty50"]["count"] == 50
    assert "rejected" in rejected["nifty50"]["detail"].lower() or "Expected 50" in load_constituent_cache()["nifty50"]["last_error"]
    cache = load_constituent_cache()
    cache["nifty50"]["fetched_at"] = "2026-10-01 10:00:00"
    cache["nifty50"]["last_error"] = ""
    save_constituent_cache(cache)
    stale_symbols, detail, origin = active_nifty(load_constituent_cache(), now, 168)
    assert stale_symbols == []
    assert origin == "none"
    assert "not used" in detail.lower()
    ok, message = save_manual_nifty(symbols, now)
    assert ok, message
    fallback, fallback_detail, fallback_origin = active_nifty(load_constituent_cache(), now, 168)
    assert fallback_origin == "manual_csv"
    assert fallback == symbols
    assert "Manual CSV fallback" in fallback_detail


def test_nifty_symbols_missing_from_groww_block_the_list():
    report = {"nifty50": {"usable": True, "symbols": ["RELIANCE", "MISSING"], "count": 2, "detail": "fresh"}}
    checked = apply_cash_validation(report, {"RELIANCE"})
    assert checked["nifty50"]["usable"] is False
    assert checked["nifty50"]["symbols"] == []
    assert "MISSING" in checked["nifty50"]["detail"]


def test_parse_requires_a_symbol_column():
    try:
        parse_constituent_csv("Name,Series\nABC,EQ\n")
    except ValueError as exc:
        assert "Symbol" in str(exc)
    else:
        raise AssertionError("A file without Symbol was accepted.")


def test_sector_method_uses_verified_members_and_keeps_sources():
    settings = load_settings()
    settings["sector_enabled"] = True
    settings["sector_source"] = "official_constituents"
    settings["price_movers_enabled"] = True
    settings["nse_rank_enabled"] = False
    moves = [{"sector": name, "move_pct": float(index + 1)} for index, name in enumerate(SECTOR_NAMES)]
    members = {}
    rows = []
    for name in SECTOR_NAMES:
        sector_rows = []
        for index in range(6):
            row = {"symbol": f"{name[:3].upper()}{index}", "name": name, "change_pct": 1 + index * 0.1, "sector": name}
            sector_rows.append(row)
            rows.append(row)
        blocked = {"symbol": f"{name[:3].upper()}X", "name": name, "change_pct": 6, "sector": name}
        sector_rows.append(blocked)
        rows.append(blocked)
        members[name] = sector_rows
    market = {"status": "BULLISH", "direction": "LONG", "complete": True}
    chosen, notes = choose_candidates(settings, market, rows, moves, members)
    symbols = {row["symbol"] for row in chosen}
    assert "INDX" not in symbols
    assert "IND5" in symbols
    shared = next(row for row in chosen if row["symbol"] == "IND5")
    assert "Method 2" in shared["sources"]
    assert notes["sector"] == "Sector method produced its eligible stocks."
    incomplete_moves = [row for row in moves if row["sector"] != "Media"]
    _chosen, blocked_notes = choose_candidates(settings, market, rows, incomplete_moves, members)
    assert "Media" in blocked_notes["sector"]


def test_reversal_protection_stays_on_until_approved():
    market = {"complete": True, "direction": "SHORT", "status": "BEARISH"}
    allowed, message = reversal_allows_signals("LONG", market, True)
    assert allowed is False
    assert "protection" in message.lower()
    allowed, _message = reversal_allows_signals("LONG", market, False)
    assert allowed is True
    assert effective_reversal_protection({}) is False
    assert effective_reversal_protection({
        "breadth_reversal_protection": True,
        "breadth_reversal_protection_change_approved": True,
    }) is True
    assert effective_setup3_inclusion({}) is False
    assert effective_setup3_inclusion({
        "setup3_include_cutoff_candle": True,
        "setup3_include_1100_approved": True,
    }) is True


def test_official_list_can_satisfy_readiness_without_the_old_upload_flag():
    settings = load_settings()
    settings.update({
        "last_groww_test": {"ok": True},
        "last_firebase_test": {"ok": True},
        "nifty50_approved": False,
        "fno_universe_approved": True,
        "fno_universe_hash": "abc",
        "sector_enabled": True,
        "sector_source": "official_constituents",
        "sector_choice_saved": True,
    })
    from nse_scanner.secrets import update_secrets
    update_secrets(groww_api_key="key", groww_api_secret="secret", firebase_service_account={"type": "service_account", "private_key": "x"})
    report = readiness(
        settings, "abc", ["RELIANCE"], False,
        nifty_meta={"usable": True, "detail": "Official list from today."},
        sector_status={"ready": True, "detail": "All 13 official sector lists are usable."},
    )
    assert report["ready"] is True


def _bullish():
    return {
        "status": "BULLISH",
        "direction": "LONG",
        "complete": True,
        "reason": "Both groups are bullish.",
        "nifty50": {"qualified": True, "adv_pct": 70},
        "fno": {"qualified": True, "adv_pct": 65},
    }


def test_final_decisions_cover_buy_wait_excluded_blocked_and_no_trade():
    market = _bullish()
    selected = [{
        "symbol": "RELIANCE", "name": "Reliance", "ltp": 101, "prev_close": 100,
        "change_pct": 1.0, "sources": ["Method 1"], "ranks": ["Method 1 #1"],
        "current_price_status": "live",
    }]
    signal = [{
        "symbol": "RELIANCE", "origin": "live", "setup": "SETUP 1",
        "candle_time": "2026-11-20 09:20:00", "open": 101, "high": 101.5, "low": 100, "close": 100.4,
        "detail": "Inside the previous range",
    }]
    excluded = [{
        "symbol": "TCS", "name": "TCS", "ltp": 110, "prev_close": 100, "change_pct": 10,
        "sources": [], "current_price_status": "live",
    }]
    bought = build_board(market, selected, excluded, signal, {}, "2026-11-20", "2026-11-20 09:25:00", True, True)
    by_symbol = {row["Symbol"]: row for row in bought["rows"]}
    assert by_symbol["RELIANCE"]["Final Decision"] == "BUY"
    assert by_symbol["TCS"]["Final Decision"] == "EXCLUDED"
    assert by_symbol["RELIANCE"]["OI"] == "N/A"
    waiting = build_board(market, selected, [], [], {}, "2026-11-20", locked=True)
    assert waiting["rows"][0]["Final Decision"] == "WAIT"
    bearish = dict(market, status="BEARISH", direction="SHORT")
    shorted = build_board(bearish, [{
        "symbol": "INFY", "name": "Infosys", "ltp": 99, "prev_close": 100, "change_pct": -1,
        "sources": ["Method 1"], "ranks": ["Method 1 #1"], "current_price_status": "live",
    }], [], signal and [{**signal[0], "symbol": "INFY", "setup": "SETUP 2"}], {}, "2026-11-20", locked=True)
    assert shorted["rows"][0]["Final Decision"] == "SHORT"
    neutral = dict(market, status="NEUTRAL", direction=None)
    assert build_board(neutral, selected, [], [], {}, "2026-11-20", locked=True)["banner"] == "NO TRADE"
    blocked = dict(market, complete=False)
    assert build_board(blocked, selected, [], [], {}, "2026-11-20", locked=True)["banner"] == "BLOCKED"
    assert build_board(market, selected, [], signal, {}, "2026-11-20", locked=False)["rows"] == []
    assert coverage_is_complete(strategy_coverage(True)) is False


def test_historical_validation_report_passes_without_claiming_profit():
    report = build_validation_report()
    assert report["live"] is False
    assert report["passed"] is True
    assert report["failed_count"] == 0
    assert "Not calculated" in report["profitability"]
    assert "11:00" in report["setup3_discrepancy"]


def test_permission_probe_does_not_treat_forbidden_data_as_a_failed_login():
    client = GrowwClient({})
    client.api = type("Api", (), {
        "SEGMENT_CASH": "CASH",
        "SEGMENT_FNO": "FNO",
        "EXCHANGE_NSE": "NSE",
        "CANDLE_INTERVAL_MIN_5": "5minute",
    })()
    client.authenticate = lambda: (True, "Groww authentication succeeded.")

    def forbidden(*_args, **_kwargs):
        raise RuntimeError("Access forbidden for this request")

    client._call = forbidden
    results = {row["name"]: row for row in client.probe_permissions()}
    assert results["Authentication"]["ok"] is True
    assert results["Live LTP"]["category"] == "permission_denied"
    assert results["Live quote"]["category"] == "permission_denied"
    assert results["Historical 5-minute candles"]["category"] == "permission_denied"
    assert results["Open interest"]["category"] == "not_tested"
    assert classify_probe_failure("Groww API key is not configured.") == "authentication"
    client.authenticate = lambda: (False, "Groww API key is not configured.")
    failed = client.probe_permissions()
    assert failed[0]["ok"] is False
    assert all(row["category"] == "not_tested" for row in failed[1:])


def _fno_rows(moves):
    return [{"symbol": symbol, "change_pct": change, "ltp": 100, "previous_close": 100, "fresh": True} for symbol, change in moves]


def test_sector_gap_names_energy_and_defence_and_does_not_rank_a_partial_list():
    from nse_scanner.providers import method_two_block, sector_index_gap
    from scanner_rules import SECTOR_NAMES

    matches = {name: ["NIFTY" + name[:4].upper()] for name in SECTOR_NAMES if name not in ("Energy", "India Defence")}
    missing = sector_index_gap(matches)
    assert missing == ["Energy", "India Defence"]
    text = method_two_block(missing, True)
    assert "cannot identify the top two sectors" in text
    assert "Energy" in text and "India Defence" in text
    assert method_two_block([], True) == ""


def test_method_three_uses_the_nse_order_and_stays_blocked_without_a_provider():
    from nse_scanner.engine import choose_candidates
    from nse_scanner.providers import OI_STATUS

    market = {"status": "BULLISH", "direction": "LONG", "nifty50": {}, "fno": {}}
    fno = _fno_rows([("AAA", 4.0), ("BBB", 3.0), ("CCC", 1.0), ("DDD", 6.0), ("EEE", 5.0)])
    blocked, notes = choose_candidates({"sector_enabled": False}, market, fno, [], {})
    assert all("Method 3" not in row["sources"] for row in blocked)
    assert "BLOCKED" in notes["nse_rank"]
    assert notes["oi"] == OI_STATUS
    nse = [
        {"symbol": "CCC", "change_pct": 1.0, "rank": 1, "source": "mock_nse", "universe": "NSE cash equities"},
        {"symbol": "BBB", "change_pct": 3.0, "rank": 2, "source": "mock_nse"},
        {"symbol": "DDD", "change_pct": 6.0, "rank": 3, "source": "mock_nse"},
        {"symbol": "EEE", "change_pct": 5.0, "rank": 4, "source": "mock_nse"},
    ]
    picked, notes = choose_candidates(
        {"sector_enabled": False, "nse_ranking_rows": nse}, market, fno, [], {},
    )
    by_symbol = {row["symbol"]: row for row in picked}
    assert "DDD" not in by_symbol
    assert "Method 3 #1" in by_symbol["CCC"]["ranks"]
    assert "Method 1" in by_symbol["BBB"]["sources"] and "Method 3" in by_symbol["BBB"]["sources"]
    assert "Method 3" not in by_symbol["AAA"]["sources"]
    assert "supplied NSE-wide ranking" in notes["nse_rank"]


def test_bearish_sector_ranking_picks_the_weakest_two_and_five_stocks():
    from nse_scanner.engine import choose_candidates
    from scanner_rules import SECTOR_NAMES

    market = {"status": "BEARISH", "direction": "SHORT", "nifty50": {}, "fno": {}}
    moves = [{"sector": name, "move_pct": -(index + 1) / 10, "source": "Groww index"} for index, name in enumerate(SECTOR_NAMES)]
    members = {}
    fno = []
    for name in SECTOR_NAMES[-2:]:
        members[name] = []
        changes = [-0.2, -0.4, -0.6, -0.8, -1.0, -6.0]
        for rank, change in enumerate(changes):
            row = {"symbol": f"{name[:3].replace(' ', '')}{rank}", "change_pct": change, "fresh": True, "sector": name}
            members[name].append(row)
            fno.append(row)
    picked, notes = choose_candidates(
        {"sector_enabled": True, "sector_source": "official_constituents"},
        market, fno, moves, members,
    )
    assert notes["sector"] == "Sector method produced its eligible stocks."
    defence = [row for row in picked if "Method 2 India Defence #" in " ".join(row["ranks"])]
    mid = [row for row in picked if "Method 2 Mid Select #" in " ".join(row["ranks"])]
    assert len(defence) == 5
    assert len(mid) == 5
    assert all(abs(row["change_pct"]) <= 5 for row in defence + mid)


def test_coverage_does_not_claim_complete_while_methods_are_blocked():
    from nse_scanner.decisions import coverage_is_complete, strategy_coverage

    rows = strategy_coverage(False)
    statuses = {row["requirement"]: row["status"] for row in rows}
    assert statuses["NSE-wide top five gainers or losers"] == "BLOCKED — DATA SOURCE REQUIRED"
    assert statuses["All 13 sector rankings"] == "BLOCKED — DATA SOURCE REQUIRED"
    assert statuses["Genuine OI-spurt definition"] == "NEEDS CLIENT CLARIFICATION"
    assert statuses["Post-lock reversal behaviour"] == "NEEDS CLIENT CLARIFICATION"
    assert statuses["Live Groww prices, candles and open interest"] == "IMPLEMENTED BUT LIVE DATA NOT VERIFIED"
    assert coverage_is_complete(rows) is False
    ready = strategy_coverage(True)
    ready_status = {row["requirement"]: row["status"] for row in ready}
    assert ready_status["All 13 sector rankings"] == "IMPLEMENTED AND TESTED"
    assert coverage_is_complete(ready) is False

