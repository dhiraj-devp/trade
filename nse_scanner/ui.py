"""English Streamlit dashboard. Live pages read the worker; they do not invent prices."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from nse_scanner.constituents import (
    active_nifty,
    active_sectors,
    load_constituent_cache,
    refresh_official_lists,
    save_manual_nifty,
    save_manual_sectors,
)
from nse_scanner.control import start_worker, stop_worker, worker_running
from nse_scanner.groww_client import GrowwClient
from nse_scanner.historical import (
    REVERSAL_DIFFERENCE,
    SETUP3_DISCREPANCY,
    parse_candle_text,
    run_historical,
    write_validation_report,
)
from nse_scanner.paths import lists_dir, runtime_dir, templates_dir
from nse_scanner.readiness import readiness
from nse_scanner.secrets import load_secrets, secret_status, update_secrets
from nse_scanner.settings import CORE_FIELDS, DEFAULTS, core_changes, load_settings, save_settings
from nse_scanner.state import read_json, read_status
from nse_scanner.store import FirestoreStore
from nse_scanner.universe import derive_fno_equity_universe, load_sector_csv, load_symbol_csv, stable_hash
from scanner_rules import (
    APPROVED_BREADTH_DENOMINATOR,
    ALTERNATE_BREADTH_DENOMINATOR,
    SECTOR_INDEX_SEARCH_KEYS,
    SECTOR_NAMES,
    classify_market,
    eligible,
)
from scanner_rules import Candle, explain_setups


PAGES = [
    "Dashboard",
    "Strategy Checklist",
    "History",
    "Settings",
]


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        html, body, [class*="css"] { font-family: "Segoe UI", sans-serif; }
        .block-container { padding-top: 1.2rem; max-width: 1200px; }
        div.stButton > button { min-height: 44px; }
        [data-testid="stDataFrame"] thead tr th { position: sticky; top: 0; z-index: 1; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render() -> None:
    st.set_page_config(page_title="NSE F&O Scanner", page_icon=":chart_with_upwards_trend:", layout="wide")
    inject_styles()
    st.title("NSE F&O Intraday Scanner")
    st.caption("Signals only. This application does not place orders.")
    status = read_status()
    if status.get("scanner_state") not in ("running", "waiting", "closed"):
        st.info("Live scanning is idle until Groww, Firebase and the approved symbol lists are ready. Open Settings to finish setup.")
    page = st.sidebar.radio("Sections", PAGES, label_visibility="collapsed")
    st.sidebar.caption("Market clock: Asia/Kolkata")
    {
        "Dashboard": page_dashboard,
        "Strategy Checklist": page_checklist,
        "History": page_history,
        "Settings": page_settings,
    }[page](status)


def _market_block(market: dict | None, title: str) -> None:
    st.subheader(title)
    if not market:
        st.info("No live breadth snapshot is available.")
        return
    labels = {
        "BULLISH": "MARKET BULLISH",
        "BEARISH": "MARKET BEARISH",
        "NEUTRAL": "MARKET NEUTRAL",
        "OPPOSITE": "MARKET OPPOSITE",
    }
    st.markdown(f"### {labels.get(market.get('status'), market.get('status'))}")
    st.write(market.get("reason") or "")
    if not market.get("complete", True) and market.get("status") == "NEUTRAL" and not market.get("nifty50", {}).get("total"):
        return
    left, right = st.columns(2)
    for column, key, label in ((left, "nifty50", "Nifty 50"), (right, "fno", "Securities in F&O")):
        group = market.get(key) or {}
        with column:
            st.markdown(f"**{label}**")
            st.metric("Advances", f"{group.get('advances', 0):,}")
            st.metric("Declines", f"{group.get('declines', 0):,}")
            st.caption(
                f"Unchanged {group.get('unchanged', 0):,} · Advances {group.get('adv_pct', 0):.1f}% · "
                f"Declines {group.get('dec_pct', 0):.1f}%"
            )
            st.caption(f"Denominator: {group.get('denominator', APPROVED_BREADTH_DENOMINATOR).replace('_', ' ')}")


def _paint(value: str) -> str:
    colors = {
        "BUY": "background-color: #e5f6e8; color: #146c2e",
        "SHORT": "background-color: #fde8e8; color: #9b1c1c",
        "WAIT": "background-color: #f4f4f5; color: #3f3f46",
        "BLOCKED": "background-color: #fff4d6; color: #854d0e",
        "EXCLUDED": "background-color: #f4f4f5; color: #52525b",
        "NO TRADE": "background-color: #fff4d6; color: #854d0e",
    }
    return colors.get(value, "")


def page_dashboard(status: dict) -> None:
    board = status.get("board") or {}
    live = status.get("market") or {}
    locked_market = (status.get("selection") or {}).get("market") or {}
    policy = status.get("signal_policy") or {}
    market = locked_market or live
    summary = board.get("summary") or {}
    health = status.get("health") or {}
    banner = board.get("banner") or market.get("status") or "NO RESULT YET"
    st.subheader(f"Market direction: {banner}")
    st.write(board.get("reason") or status.get("message") or "The scanner has not published a result yet.")
    eligibility = policy.get("eligibility") or "Signal eligibility is published after the scanner locks the 9:30 list."
    if "still scanned" in eligibility:
        signal_state = "Still scanned"
    elif "no new signal" in eligibility or "not eligible" in eligibility or "No direction" in eligibility or "No candidate" in eligibility:
        signal_state = "Not open"
    else:
        signal_state = "See note"
    columns = st.columns(3)
    columns[0].metric("Locked direction", policy.get("locked_direction") or locked_market.get("direction") or "not locked")
    columns[1].metric("Live breadth now", policy.get("live_status") or live.get("status") or "unavailable")
    columns[2].metric("New signals", signal_state)
    st.caption(eligibility)
    nifty = market.get("nifty50") or {}
    fno = market.get("fno") or {}
    columns = st.columns(4)
    columns[0].metric("Nifty 50 advances", f"{nifty.get('adv_pct', '—')}%")
    columns[1].metric("F&O advances", f"{fno.get('adv_pct', '—')}%")
    columns[2].metric("Selected", len(summary.get("buy", [])) + len(summary.get("short", [])) + len(summary.get("wait", [])))
    columns[3].metric("Last update", (status.get("updated_at") or "—")[-8:])
    columns = st.columns(4)
    columns[0].metric("BUY", len(summary.get("buy") or []))
    columns[1].metric("SHORT", len(summary.get("short") or []))
    columns[2].metric("Waiting", len(summary.get("wait") or []))
    columns[3].metric("Excluded or blocked", (summary.get("excluded") or 0) + (summary.get("blocked") or 0))
    st.markdown("**Today's result**")
    if summary.get("buy"):
        st.success("BUY / LONG: " + ", ".join(summary["buy"]))
    if summary.get("short"):
        st.error("SHORT: " + ", ".join(summary["short"]))
    if summary.get("wait"):
        st.info("WAIT: " + ", ".join(summary["wait"]))
    if banner == "NO TRADE":
        st.warning("NO TRADE. " + (board.get("reason") or "The market gate did not confirm one direction."))
    if not summary.get("buy") and not summary.get("short"):
        st.info("No qualifying trade signals at this time.")
    _market_block(market if market else None, "Market breadth used for the decision")
    rows = board.get("rows") or []
    st.subheader("Stock results")
    if not rows:
        st.caption("No stock list is available for this decision. Sample stocks are not shown.")
    else:
        choice = st.selectbox("Show", ["BUY, SHORT and WAIT", "All decisions", "BUY", "SHORT", "WAIT", "EXCLUDED", "BLOCKED"])
        visible = rows
        if choice == "BUY, SHORT and WAIT":
            visible = [row for row in rows if row["Final Decision"] in ("BUY", "SHORT", "WAIT")]
        elif choice != "All decisions":
            visible = [row for row in rows if row["Final Decision"] == choice]
        frame = pd.DataFrame(visible)
        hidden = [column for column in ("checks", "proof") if column in frame.columns]
        shown = frame.drop(columns=hidden) if len(frame) else frame
        if len(shown) and "Final Decision" in shown.columns:
            styled = shown.style.map(_paint, subset=["Final Decision"])
            st.dataframe(styled, use_container_width=True, hide_index=True)
        else:
            st.dataframe(shown, use_container_width=True, hide_index=True)
        st.download_button(
            "Download this table as CSV",
            shown.to_csv(index=False) if len(shown) else "Symbol\n",
            file_name="nse_fno_stock_results.csv",
            mime="text/csv",
            use_container_width=True,
        )
        if visible:
            symbol = st.selectbox("Inspect one stock", [row["Symbol"] for row in visible])
            chosen = next(row for row in visible if row["Symbol"] == symbol)
            st.markdown(f"**{chosen['Symbol']} — {chosen['Final Decision']}**")
            st.write(chosen["Reason"])
            proof = pd.DataFrame([
                {
                    "Strategy check": item.get("label"),
                    "Observed value": item.get("actual"),
                    "Required condition": item.get("required"),
                    "Result": item.get("mark"),
                }
                for item in chosen.get("checks") or []
            ])
            st.dataframe(proof, use_container_width=True, hide_index=True)
            for line in chosen.get("proof") or []:
                st.caption(line)
    st.caption(
        f"Data connection: {health.get('database') or 'not connected'} · "
        f"Feed: {health.get('feed') or 'not started'} · "
        f"Updated: {status.get('updated_at') or '—'}"
    )


def page_checklist(status: dict) -> None:
    from nse_scanner.decisions import coverage_is_complete, strategy_coverage
    board = status.get("board") or {}
    rows = board.get("coverage") or strategy_coverage(False)
    st.subheader("Strategy checklist")
    st.caption("A rule is marked tested only when the application implements it and the automated tests cover it.")
    if coverage_is_complete(rows):
        st.success("Every listed client rule is implemented and tested.")
    else:
        st.warning("The client strategy is not fully covered. Items below that are blocked, missing data, or waiting for a definition are not treated as complete.")
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    from nse_scanner.providers import data_source_rows
    from nse_scanner.settings import load_settings
    settings = load_settings()
    st.subheader("Data-source readiness")
    st.dataframe(
        pd.DataFrame(data_source_rows(
            settings.get("last_groww_permissions"),
            settings.get("last_firebase_test"),
            str(status.get("sector_block") or ""),
        )),
        use_container_width=True,
        hide_index=True,
    )
    methods = board.get("methods") or {}
    if methods:
        st.subheader("What today's scan said about each method")
        for name, detail in methods.items():
            st.write(f"{name}: {detail}")


def page_breadth(status: dict) -> None:
    _market_block(status.get("market"), "Live breadth")
    selection = (status.get("selection") or {}).get("market")
    _market_block(selection, "Breadth used to lock the 9:30 candidate list")
    st.caption("Unchanged stocks are counted separately. They are not treated as advances or declines.")


def page_candidates(status: dict) -> None:
    rows = status.get("candidates") or []
    if not rows:
        st.info("No candidate list is locked. The scanner does not show sample stocks.")
        return
    frame = pd.DataFrame(rows)
    keep = [column for column in ["symbol", "name", "ltp", "change_pct", "sector", "sources", "current_price", "current_price_status"] if column in frame.columns]
    st.dataframe(frame[keep], use_container_width=True, hide_index=True)
    direction = (status.get("selection") or {}).get("direction")
    st.caption(f"Locked direction: {direction or 'none'}. Current price is shown only while the latest Groww price is fresh.")


def page_signals(status: dict) -> None:
    rows = [row for row in status.get("signals") or [] if row.get("origin") == "live"]
    if not rows:
        st.info("No live signals are stored for this session.")
        return
    frame = pd.DataFrame(rows)
    columns = [column for column in [
        "symbol", "direction", "setup", "candle_time", "open", "high", "low", "close", "volume",
        "previous_day_high", "previous_day_low", "current_price", "current_price_status", "sources",
    ] if column in frame.columns]
    st.dataframe(frame[columns], use_container_width=True, hide_index=True)


def page_scanner(_status: dict) -> None:
    st.subheader("Manual rule check")
    st.caption("This check uses the numbers you type. It is not the live scanner and it does not create a live signal.")
    left, right = st.columns(2)
    with left:
        nifty_adv = st.number_input("Nifty 50 advances", min_value=0, step=1, value=0)
        nifty_dec = st.number_input("Nifty 50 declines", min_value=0, step=1, value=0)
    with right:
        fno_adv = st.number_input("F&O advances", min_value=0, step=1, value=0)
        fno_dec = st.number_input("F&O declines", min_value=0, step=1, value=0)
    market = classify_market(nifty_adv, nifty_dec, fno_adv, fno_dec)
    st.write(f"{market['status']} — {market['reason']}")
    st.subheader("Setup rule tester")
    direction = st.selectbox("Direction", ["LONG", "SHORT"])
    pdh = st.number_input("Previous day high", min_value=0.0, value=100.0, format="%.2f")
    pdl = st.number_input("Previous day low", min_value=0.0, value=90.0, format="%.2f")
    example = "09:15,100,102,99,101,1000\n09:20,101,103,100,102,800\n09:25,102,104,101,103,700\n09:30,103,104,101,102,500"
    raw = st.text_area("Candles: time, open, high, low, close, volume", value=example, height=160)
    st.caption("The sample above is only a format example. It is not a live signal.")
    if st.button("Test setup rules", type="primary", use_container_width=True):
        try:
            candles = []
            today = datetime.now().date()
            for line in raw.strip().splitlines():
                parts = [item.strip() for item in line.split(",")]
                if len(parts) != 6:
                    raise ValueError("Each row needs 6 fields.")
                stamp = datetime.combine(today, datetime.strptime(parts[0], "%H:%M").time())
                candles.append(Candle(stamp, *[float(value) for value in parts[1:]]))
            candles.sort(key=lambda candle: candle.ts)
            explained = explain_setups(candles, direction, pdh, pdl)
            alerts = explained["alerts"]
            if alerts:
                st.dataframe(pd.DataFrame(alerts), use_container_width=True, hide_index=True)
            else:
                st.warning("No setup matched these candles.")
            st.dataframe(pd.DataFrame(explained["checks"]), use_container_width=True, hide_index=True)
        except Exception as exc:
            st.error(f"Input error: {exc}")
    st.subheader("Candidate file check")
    uploaded = st.file_uploader("Candidate CSV", type=["csv"])
    if uploaded is not None:
        frame = pd.read_csv(uploaded)
        if "change_pct" in frame.columns:
            frame["eligible"] = frame["change_pct"].apply(
                lambda value: eligible(float(value), market["direction"] or "NO TRADE", market_status=market["status"])
            )
            st.dataframe(frame[frame["eligible"]], use_container_width=True, hide_index=True)
        else:
            st.warning("The CSV needs a change_pct column.")
    st.subheader("Historical test")
    st.caption("Historical output stays on this page and is never stored as a live signal.")
    history_text = st.text_area("Historical candles", value=example, height=140, key="historical_candles")
    move = st.number_input("Daily move percent for the eligibility check", value=1.0, format="%.2f")
    if st.button("Run historical test", use_container_width=True):
        try:
            result = run_historical(nifty_adv, nifty_dec, fno_adv, fno_dec, parse_candle_text(history_text), direction, pdh, pdl, move)
            st.warning(result["note"])
            if result["alerts"]:
                st.dataframe(pd.DataFrame(result["alerts"]), use_container_width=True, hide_index=True)
            else:
                st.info("The historical candles produced no qualifying setup.")
        except Exception as exc:
            st.error(f"Input error: {exc}")
    st.subheader("Rule validation report")
    st.caption("This replays fixed completed candles for all three setups. It does not calculate profit and it does not create a live signal.")
    if st.button("Run historical rule validation", use_container_width=True):
        validation = write_validation_report()
        if validation["passed"]:
            st.success(f"Passed {validation['case_count']} historical rule checks.")
        else:
            st.error(f"{validation['failed_count']} historical rule checks failed.")
        st.caption(validation["setup3_discrepancy"])
        st.caption(validation["reversal_difference"])
        st.caption(validation["profitability"])
        st.dataframe(pd.DataFrame(validation["cases"]), use_container_width=True, hide_index=True)


def _store_or_none():
    try:
        if not secret_status()["firebase_service_account"]:
            return None
        return FirestoreStore.connect()
    except Exception as exc:
        st.warning(f"Firestore is not available: {exc}")
        return None


def page_history(_status: dict) -> None:
    st.caption("Live history comes from Firestore. Historical tests are not listed here.")
    trading_date = st.text_input("Trading date (YYYY-MM-DD)", value="")
    symbol = st.text_input("Stock symbol", value="").upper().strip()
    setup = st.selectbox("Setup", ["All", "SETUP 1", "SETUP 2", "SETUP 3"])
    direction = st.selectbox("Direction", ["All", "LONG", "SHORT"])
    source = st.text_input("Selection source contains", value="")
    store = _store_or_none()
    rows = []
    if store:
        ok, loaded = store.query_signals(trading_date or None, 500, None)
        rows = loaded if ok else []
        if not ok and loaded:
            st.error(loaded[0].get("error", "Could not read signals."))
            return
    else:
        rows = [row for row in (_status.get("signals") or []) if row.get("origin") == "live"]
        st.caption("Showing the current scanner status only, because Firebase is not connected.")
    filtered = []
    for row in rows:
        if not isinstance(row, dict) or row.get("origin") not in (None, "live"):
            continue
        if symbol and row.get("symbol") != symbol:
            continue
        if setup != "All" and row.get("setup") != setup:
            continue
        if direction != "All" and row.get("direction") != direction:
            continue
        if source and source.lower() not in " ".join(row.get("sources") or []).lower():
            continue
        filtered.append(row)
    if not filtered:
        st.info("No stored live signals match these filters.")
        return
    st.dataframe(pd.DataFrame(filtered), use_container_width=True, hide_index=True)


def page_reports(_status: dict) -> None:
    store = _store_or_none()
    if not store:
        st.info("Connect Firebase to read daily reports.")
        return
    ok, rows = store.query_reports(30)
    if not ok:
        st.error(rows[0].get("error") if rows else "Could not read reports.")
        return
    live = [row for row in rows if row.get("origin") == "live"]
    if not live:
        st.info("No live daily report is stored yet.")
        return
    report_frame = pd.DataFrame(live)
    columns = [column for column in ["trading_date", "market_status", "candidate_count", "signal_count", "note"] if column in report_frame.columns]
    st.dataframe(report_frame[columns], use_container_width=True, hide_index=True)
    choice = st.selectbox("Open report", [row["trading_date"] for row in live])
    selected = next(row for row in live if row["trading_date"] == choice)
    st.json({key: value for key, value in selected.items() if key != "signals"})
    if selected.get("signals"):
        st.dataframe(pd.DataFrame(selected["signals"]), use_container_width=True, hide_index=True)


def page_api(status: dict) -> None:
    settings = load_settings()
    health = status.get("health") or {}
    secrets = secret_status()
    rows = pd.DataFrame([
        {"Check": "Groww credentials", "State": "saved" if secrets["groww_api_key"] else "missing"},
        {"Check": "Last Groww authentication", "State": (settings.get("last_groww_test") or {}).get("message") or "not run"},
        {"Check": "Firebase credentials", "State": "saved" if secrets["firebase_service_account"] else "missing"},
        {"Check": "Last Firebase test", "State": (settings.get("last_firebase_test") or {}).get("message") or "not run"},
        {"Check": "Live feed", "State": health.get("feed") or "not started"},
        {"Check": "Last Groww success", "State": health.get("last_groww_success") or "none"},
        {"Check": "Database", "State": health.get("database") or "not connected"},
        {"Check": "Last scanner update", "State": status.get("updated_at") or "none"},
        {"Check": "Latest error", "State": health.get("last_error") or "none"},
    ])
    st.dataframe(rows, use_container_width=True, hide_index=True)
    permissions = settings.get("last_groww_permissions") or {}
    if permissions.get("results"):
        st.subheader("Groww permission checks")
        st.caption("Authentication can succeed while live data, history, or open interest stays forbidden. A failed check is never filled with a sample price.")
        st.dataframe(pd.DataFrame(permissions["results"]), use_container_width=True, hide_index=True)
        st.caption(f"Checked at {permissions.get('at') or '—'}")
    lists = status.get("lists") or {}
    if lists:
        st.subheader("Symbol lists")
        nifty = lists.get("nifty50") or {}
        sectors = lists.get("sectors") or {}
        st.write(nifty.get("detail") or "Nifty 50 list has not been published by the scanner.")
        st.write(sectors.get("detail") or "Sector lists have not been published by the scanner.")
        matches = lists.get("index_matches") or {}
        if matches:
            st.caption("Exact Groww index symbols found in the instrument master. A missing name is not guessed.")
            st.json(matches)
    if health.get("errors"):
        st.subheader("Recent scanner errors")
        st.dataframe(pd.DataFrame({"error": health["errors"]}), use_container_width=True, hide_index=True)


def page_settings(_status: dict) -> None:
    settings = load_settings()
    secrets = secret_status()
    universe = read_json("fno_universe.json", {})
    now = datetime.now()
    cache = load_constituent_cache()
    max_age = float(settings.get("constituent_max_age_hours") or 168)
    nifty_symbols, nifty_detail, nifty_origin = active_nifty(cache, now, max_age)
    sector_symbols, sector_errors = active_sectors(cache, now, max_age)
    nifty_meta = {"usable": bool(nifty_symbols), "detail": nifty_detail, "origin": nifty_origin}
    sector_status = {
        "ready": len(sector_symbols) == len(SECTOR_NAMES),
        "detail": "All 13 official sector lists are usable." if not sector_errors else " ".join(sector_errors),
    }
    report = readiness(
        settings, universe.get("hash", ""), nifty_symbols, bool(sector_symbols),
        nifty_meta=nifty_meta, sector_status=sector_status,
    )
    st.subheader("Setup wizard")
    for item in report["checks"]:
        if item["passed"]:
            st.success(f"{item['name']}: {item['detail']}")
        else:
            st.warning(f"{item['name']}: {item['detail']}")
    if report["ready"]:
        st.success("Mandatory checks have passed. You can start the scanner.")
    else:
        st.error("The scanner is not live-ready. Missing items above must be completed. No sample market is shown in their place.")
    running = worker_running()
    left, right, third = st.columns(3)
    if left.button("Start scanner", use_container_width=True, disabled=running):
        ok, message = start_worker()
        st.session_state["control_message"] = message if ok else message
    if right.button("Stop scanner", use_container_width=True):
        ok, message = stop_worker()
        st.session_state["control_message"] = message
    if third.button("Restart scanner", use_container_width=True):
        stop_worker()
        ok, message = start_worker()
        st.session_state["control_message"] = message
    if st.session_state.get("control_message"):
        st.info(st.session_state["control_message"])
    st.caption(f"Scanner process: {'running' if running else 'stopped'} · Credentials: {secrets['storage']} · {secrets['location']}")

    with st.expander("Groww API", expanded=True):
        mode = st.selectbox("Authentication", ["api_key_secret", "totp"], index=0 if settings["groww_auth_mode"] != "totp" else 1)
        api_key = st.text_input("API key", type="password", placeholder="Saved" if secrets["groww_api_key"] else "Required")
        api_secret = st.text_input("API secret", type="password", placeholder="Saved" if secrets["groww_api_secret"] else "Required for API key flow")
        totp_secret = st.text_input("TOTP secret", type="password", placeholder="Saved" if secrets["groww_totp_secret"] else "Required for TOTP flow")
        st.caption(f"Saved key: {'yes' if secrets['groww_api_key'] else 'no'} · Last success: {(settings.get('last_groww_test') or {}).get('at') or 'never'}")
        if st.button("Save Groww settings", use_container_width=True):
            save_settings({"groww_auth_mode": mode})
            update_secrets(groww_api_key=api_key, groww_api_secret=api_secret, groww_totp_secret=totp_secret)
            st.success("Groww settings saved. Secrets stay outside the project and are not shown again.")
        if st.button("Test Groww permissions", use_container_width=True):
            client = GrowwClient(load_settings())
            results = client.probe_permissions()
            auth = next(row for row in results if row["name"] == "Authentication")
            oi = next(row for row in results if row["name"] == "Open interest")
            checked_at = datetime.now().isoformat(timespec="seconds")
            save_settings({
                "last_groww_test": {"ok": auth["ok"], "message": auth["message"], "at": checked_at},
                "last_groww_permissions": {"at": checked_at, "results": results},
                "last_oi_probe": {"ok": oi["ok"], "message": oi["message"], "at": checked_at},
            })
            st.dataframe(pd.DataFrame(results), use_container_width=True, hide_index=True)
            st.caption("A successful login does not mean live prices, candles, or open interest are included in the subscription.")

    with st.expander("Firebase", expanded=False):
        uploaded = st.file_uploader("Service account JSON", type=["json"])
        st.caption(f"Service account: {'saved' if secrets['firebase_service_account'] else 'missing'}")
        if uploaded is not None and st.button("Save service account", use_container_width=True):
            try:
                payload = json.loads(uploaded.getvalue().decode("utf-8"))
                if payload.get("type") != "service_account" or not payload.get("private_key"):
                    raise ValueError("This file is not a Firebase service account JSON.")
                update_secrets(firebase_service_account=payload)
                st.success("Service account stored outside the project. Its contents are not displayed.")
            except Exception as exc:
                st.error(str(exc))
        if st.button("Test Firestore read and write", use_container_width=True):
            try:
                ok, message = FirestoreStore.connect().ping()
            except Exception as exc:
                ok, message = False, str(exc)
            save_settings({"last_firebase_test": {"ok": ok, "message": message, "at": datetime.now().isoformat(timespec="seconds")}})
            st.success(message) if ok else st.error(message)

    with st.expander("Market settings", expanded=False):
        st.caption("Timezone is fixed to Asia/Kolkata. Changing a core value requires confirmation and does not happen silently.")
        edited = {
            "market_open": st.text_input("Market open", settings["market_open"]),
            "selection_deadline": st.text_input("Candidate deadline", settings["selection_deadline"]),
            "setup3_end": st.text_input("Setup 3 end", settings["setup3_end"]),
            "market_close": st.text_input("Market close", settings["market_close"]),
            "candle_minutes": int(st.number_input("Candle minutes", min_value=1, value=int(settings["candle_minutes"]))),
            "max_abs_move": float(st.number_input("Maximum absolute move percent", value=float(settings["max_abs_move"]))),
            "advance_min": float(st.number_input("Breadth advance minimum", value=float(settings["advance_min"]))),
            "decline_max": float(st.number_input("Breadth decline maximum", value=float(settings["decline_max"]))),
            "setup2_combined_pct": float(st.number_input("Setup 2 combined move cap", value=float(settings["setup2_combined_pct"]))),
            "setup2_second_pct": float(st.number_input("Setup 2 second-candle move cap", value=float(settings["setup2_second_pct"]))),
        }
        denominator = st.selectbox(
            "Breadth denominator",
            [APPROVED_BREADTH_DENOMINATOR, ALTERNATE_BREADTH_DENOMINATOR],
            index=0 if settings["breadth_denominator"] == APPROVED_BREADTH_DENOMINATOR else 1,
            help="The approved rule uses advances plus declines. Including unchanged stocks is a different rule.",
        )
        approve_core = st.checkbox("I confirm these core strategy changes", value=bool(settings.get("core_strategy_change_approved")))
        approve_denominator = st.checkbox(
            "I approve including unchanged stocks in the breadth denominator",
            value=bool(settings.get("breadth_denominator_approved")),
        )
        st.caption(SETUP3_DISCREPANCY)
        include_cutoff = st.checkbox(
            "Include the 11:00-11:05 candle in Setup 3",
            value=bool(settings.get("setup3_include_cutoff_candle", False)),
        )
        approve_setup3 = st.checkbox(
            "I approve excluding the 11:00 candle from Setup 3",
            value=bool(settings.get("setup3_cutoff_change_approved")),
        )
        st.caption(REVERSAL_DIFFERENCE)
        reversal_protection = st.checkbox(
            "Block new signals when live breadth leaves the 9:30 direction",
            value=bool(settings.get("breadth_reversal_protection", False)),
        )
        approve_reversal = st.checkbox(
            "I approve scanning the locked list after the breadth changes",
            value=bool(settings.get("breadth_reversal_protection_change_approved")),
        )
        proposed = {**settings, **edited, "breadth_denominator": denominator,
                    "core_strategy_change_approved": approve_core, "breadth_denominator_approved": approve_denominator,
                    "setup3_include_cutoff_candle": include_cutoff, "setup3_cutoff_change_approved": approve_setup3,
                    "breadth_reversal_protection": reversal_protection,
                    "breadth_reversal_protection_change_approved": approve_reversal}
        if core_changes(proposed) and not approve_core:
            st.warning("These values differ from the approved defaults and will not be saved until you confirm them.")
        if st.button("Save market settings", use_container_width=True):
            if core_changes(proposed) and not approve_core:
                st.error("Core changes were not saved.")
            elif denominator != APPROVED_BREADTH_DENOMINATOR and not approve_denominator:
                st.error("The alternate denominator was not saved.")
            elif not include_cutoff and not approve_setup3:
                st.error("The Setup 3 cutoff change was not saved. Approve it first, or leave the 11:00 candle included.")
            elif not reversal_protection and not approve_reversal:
                st.error("The breadth-reversal change was not saved. Approve it first, or leave the protection on.")
            else:
                save_settings(proposed)
                st.success("Market settings saved.")

    with st.expander("Stock selection", expanded=False):
        movers = st.checkbox("Top movers", value=bool(settings["price_movers_enabled"]))
        nse_rank = st.checkbox("NSE gainers and losers from Groww prices", value=bool(settings["nse_rank_enabled"]))
        oi_enabled = st.checkbox("OI ranking (off until approved)", value=bool(settings["oi_enabled"]))
        oi_approved = st.checkbox(
            "I approve nearest-expiry stock-future OI day change as the OI ranking",
            value=bool(settings["oi_definition_approved"]),
        )
        st.caption(
            "OI increase means the largest oi_day_change. OI decrease means the most negative oi_day_change. "
            "Price gainers are not OI spurts. Ranking stays off until this formula is approved and the OI permission check succeeds."
        )
        top_n = int(st.number_input("Top gainers or losers", min_value=1, value=int(settings["top_n"])))
        sector_enabled = st.checkbox("Sector movers", value=bool(settings["sector_enabled"]))
        method = st.selectbox("Sector move", ["official_index", "constituent_average"], index=0 if settings["sector_method"] == "official_index" else 1)
        sector_count = int(st.number_input("Sectors to select", min_value=1, max_value=13, value=int(settings["sector_count"])))
        per_sector = int(st.number_input("Stocks per sector", min_value=1, value=int(settings["stocks_per_sector"])))
        tie = st.checkbox("Approve symbol A-Z tie-break when the cutoff is tied", value=bool(settings["tie_break_approved"]))
        approve_counts = st.checkbox(
            "I confirm changes to the top-count defaults",
            value=bool(settings.get("core_strategy_change_approved")),
        )
        symbols = dict(settings.get("sector_symbols") or {})
        st.markdown("**Approved sector index symbols**")
        st.caption("Leave a symbol blank until you have matched it to the Groww instrument master. Search keys are not approvals.")
        for name in SECTOR_NAMES:
            symbols[name] = st.text_input(f"{name} index symbol", value=symbols.get(name) or "", help=", ".join(SECTOR_INDEX_SEARCH_KEYS[name])).upper()
        index_approved = st.checkbox("I confirm these index symbols", value=bool(settings["sector_method_approved"]))
        constituents_approved = st.checkbox("I confirm the uploaded sector constituent file", value=bool(settings["sector_constituents_approved"]))
        sector_file = st.file_uploader("Sector constituent fallback CSV", type=["csv"], key="sector_csv")
        if sector_file is not None and st.button("Save sector fallback", use_container_width=True):
            destination = lists_dir() / "sectors.csv"
            destination.write_bytes(sector_file.getvalue())
            try:
                mapping = load_sector_csv(destination)
                ok, message = save_manual_sectors(mapping, datetime.now())
            except ValueError as exc:
                ok, message = False, str(exc)
            st.success(message) if ok else st.error(message)
        if st.button("Save selection settings", use_container_width=True):
            counts_changed = any(
                value != DEFAULTS[name]
                for name, value in {"top_n": top_n, "sector_count": sector_count, "stocks_per_sector": per_sector}.items()
            )
            if counts_changed and not approve_counts:
                st.error("Top-count changes were not saved. Confirm them first, or set them back to the defaults.")
            else:
                payload = {
                    "price_movers_enabled": movers,
                    "nse_rank_enabled": nse_rank,
                    "oi_enabled": oi_enabled,
                    "oi_definition_approved": oi_approved,
                    "top_n": top_n,
                    "sector_enabled": sector_enabled,
                    "sector_method": method,
                    "sector_count": sector_count,
                    "stocks_per_sector": per_sector,
                    "tie_break_approved": tie,
                    "sector_symbols": symbols,
                    "sector_method_approved": index_approved,
                    "sector_constituents_approved": constituents_approved,
                    "sector_choice_saved": True,
                    "sector_source": "approved_index" if method == "official_index" and index_approved else "official_constituents",
                }
                if counts_changed:
                    payload["core_strategy_change_approved"] = True
                save_settings(payload)
                st.success("Selection settings saved.")

    with st.expander("Symbol lists", expanded=False):
        st.caption("Nifty 50 and the 13 sector lists are read from the official NSE constituent files. A failed or outdated file is not used. A CSV upload is only the fallback.")
        st.write(nifty_detail)
        if nifty_symbols:
            st.caption(f"Active Nifty 50 source: {nifty_origin}. {len(nifty_symbols)} symbols.")
            st.dataframe(pd.DataFrame({"symbol": nifty_symbols}), use_container_width=True, hide_index=True)
        if sector_errors:
            st.warning(" ".join(sector_errors))
        elif sector_symbols:
            st.success("All 13 sector constituent lists are usable.")
        if st.button("Refresh official NSE lists", use_container_width=True):
            refreshed = refresh_official_lists(datetime.now(), max_age_hours=max_age, force=True)
            nifty = refreshed.get("nifty50") or {}
            sectors = refreshed.get("sectors") or {}
            if nifty.get("usable"):
                st.success(nifty.get("detail") or "Nifty 50 list refreshed.")
            else:
                st.error(nifty.get("detail") or "Nifty 50 list was not refreshed.")
            if sectors.get("errors"):
                st.warning(" ".join(sectors["errors"]))
            else:
                st.success(sectors.get("detail") or "Sector lists refreshed.")
        template = templates_dir() / "nifty50_constituents.csv"
        if template.exists():
            st.download_button("Download Nifty 50 template", template.read_text(encoding="utf-8"), file_name="nifty50_constituents.csv")
        nifty_file = st.file_uploader("Nifty 50 fallback CSV", type=["csv"], key="nifty_csv")
        if nifty_file is not None and st.button("Save Nifty 50 fallback", use_container_width=True):
            destination = lists_dir() / "nifty50.csv"
            destination.write_bytes(nifty_file.getvalue())
            try:
                ok, message = save_manual_nifty(load_symbol_csv(destination), datetime.now())
            except ValueError as exc:
                ok, message = False, str(exc)
            st.success(message) if ok else st.error(message)
        sector_template = templates_dir() / "sector_constituents.csv"
        if sector_template.exists():
            st.download_button("Download sector template", sector_template.read_text(encoding="utf-8"), file_name="sector_constituents.csv")
        if st.button("Refresh Groww instrument master", use_container_width=True):
            client = GrowwClient(load_settings())
            ok, message = client.authenticate()
            if not ok:
                st.error(message)
            else:
                frame = client.download_instruments()
                path = runtime_dir() / "instruments.csv"
                frame.to_csv(path, index=False)
                derived = derive_fno_equity_universe(frame)
                digest = stable_hash([item["symbol"] for item in derived])
                from nse_scanner.state import write_json
                write_json("fno_universe.json", {"hash": digest, "count": len(derived), "symbols": derived,
                                                 "generated_at": datetime.now().isoformat(sep=" ", timespec="seconds")})
                save_settings({"fno_universe_approved": False, "fno_universe_hash": ""})
                st.success(f"Derived {len(derived)} F&O equities. Approval was cleared because this is a new list.")
                st.dataframe(pd.DataFrame(derived)[["symbol", "name"]], use_container_width=True, hide_index=True)
        symbols = universe.get("symbols") or []
        if symbols and universe.get("hash"):
            st.caption(f"Derived F&O equities: {universe.get('count', len(symbols))}. Hash {universe.get('hash', '')[:12]}.")
            state = "approved" if settings.get("fno_universe_approved") else "not approved"
            st.caption(f"This derived list is {state}.")
            preview = pd.DataFrame(symbols)
            columns = [column for column in ["symbol", "name"] if column in preview.columns]
            st.dataframe(preview[columns], use_container_width=True, hide_index=True)
            if st.button("Approve derived F&O universe", use_container_width=True):
                save_settings({"fno_universe_approved": True, "fno_universe_hash": universe.get("hash", "")})
                st.success("F&O universe approved.")
            if st.button("Clear F&O universe approval", use_container_width=True):
                save_settings({"fno_universe_approved": False, "fno_universe_hash": ""})

    with st.expander("Notifications", expanded=False):
        st.caption("Notifications are optional. The scanner and the dashboard work without them.")
        telegram = st.checkbox("Telegram", value=bool(settings["telegram_enabled"]))
        chat = st.text_input("Telegram chat id", value=settings.get("telegram_chat_id") or "")
        token = st.text_input("Telegram bot token", type="password", placeholder="Saved" if secrets["telegram_token"] else "")
        email = st.checkbox("Email", value=bool(settings["email_enabled"]))
        host = st.text_input("SMTP host", value=settings.get("smtp_host") or "")
        port = int(st.number_input("SMTP port", min_value=1, value=int(settings.get("smtp_port") or 587)))
        username = st.text_input("SMTP username", value=settings.get("smtp_username") or "")
        sender = st.text_input("From address", value=settings.get("smtp_from") or "")
        recipient = st.text_input("To address", value=settings.get("smtp_to") or "")
        password = st.text_input("SMTP password", type="password", placeholder="Saved" if secrets["smtp_password"] else "")
        if st.button("Save notifications", use_container_width=True):
            save_settings({
                "telegram_enabled": telegram, "telegram_chat_id": chat, "email_enabled": email,
                "smtp_host": host, "smtp_port": port, "smtp_username": username,
                "smtp_from": sender, "smtp_to": recipient,
            })
            update_secrets(telegram_token=token, smtp_password=password)
            st.success("Notification settings saved.")
        if st.button("Send test notification", use_container_width=True):
            from nse_scanner.notify import notify_all
            st.write(notify_all(load_settings(), "NSE F&O scanner test message. This is not a trade signal."))

    with st.expander("System", expanded=False):
        health = _status.get("health") or {}
        st.write(f"Trading date: {_status.get('trading_date') or '—'}")
        st.write(f"Last scan update: {_status.get('updated_at') or '—'}")
        st.write(f"Database: {health.get('database') or 'not connected'}")
        st.write(f"Data freshness window: {settings.get('quote_max_age_seconds')} seconds")
        age = int(st.number_input("Quote freshness seconds", min_value=5, value=int(settings["quote_max_age_seconds"])))
        if st.button("Save freshness window", use_container_width=True):
            save_settings({"quote_max_age_seconds": age})
        log_path = runtime_dir() / "scanner.log"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
            st.text("\n".join(lines) or "Log is empty.")
        else:
            st.caption("No scanner log yet.")
        if not settings.get("wizard_seen"):
            save_settings({"wizard_seen": True})
