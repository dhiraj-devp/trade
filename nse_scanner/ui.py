"""English Streamlit dashboard. Live pages read the worker; they do not invent prices."""

from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from nse_scanner.cloud_budget import cached_history_rows, cached_reports, load_budget
from nse_scanner.cloud_scanner import (
    evaluate_monitoring_cycle,
    ist_now,
    lock_and_save_candidates,
    prepare_candidates_scan,
    restore_today_state,
    validate_market_session,
)
from nse_scanner.constituents import (
    active_nifty,
    active_sectors,
    load_constituent_cache,
    refresh_official_lists,
    save_manual_nifty,
    save_manual_sectors,
)
from nse_scanner.control import start_worker, stop_worker, worker_running
from nse_scanner.decisions import build_board
from nse_scanner.engine import parse_clock
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
    if load_settings().get("developer_mode"):
        st.warning(
            "Developer mode is on. Clock limits are bypassed for this test. "
            "These results are stored separately and do not change the production scan."
        )
        st.sidebar.caption("Developer mode")
    status = read_status()
    secrets = secret_status()
    if not secrets.get("groww_api_key"):
        st.warning("Groww API credentials are not configured. Add them in Settings or Streamlit Secrets.")
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


@st.fragment(run_every=30)
def _monitoring_fragment(client, store, locked_candidates, settings) -> None:
    now = ist_now()
    trading_date = str(now.date())
    cutoff_time = parse_clock(settings.get("setup3_end", "11:00"))
    is_active = st.session_state.get("monitoring_active", False)
    cutoff_reached = now.time().replace(microsecond=0) >= cutoff_time.replace(microsecond=0)

    if is_active:
        if cutoff_reached:
            st.session_state["monitoring_active"] = False
            mon_state = st.session_state.get("monitoring_state") or {}
            mon_state["state"] = "COMPLETED"
            mon_state["cutoff_reached"] = True
            st.session_state["monitoring_state"] = mon_state
            if store is not None:
                try:
                    store.save_monitoring_state(trading_date, mon_state)
                except Exception:
                    pass
            st.info("11:00 AM IST cutoff reached. Monitoring window completed.")
        else:
            eval_res = evaluate_monitoring_cycle(
                client, store, locked_candidates, now, settings,
                known_ids=st.session_state.get("known_ids"),
            )
            st.session_state["board"] = eval_res.get("board")
            st.session_state["signals"] = eval_res.get("signals")
            st.session_state["monitoring_state"] = eval_res.get("monitoring_state")
            if eval_res.get("gap_detected"):
                st.session_state["gap_message"] = eval_res.get("gap_message")

    mon = st.session_state.get("monitoring_state") or {}
    c1, c2, c3 = st.columns(3)
    state_str = "ACTIVE (30s refresh)" if is_active else ("COMPLETED (11:00 Cutoff)" if cutoff_reached else "PAUSED")
    c1.metric("Monitoring State", state_str)
    c2.metric("Last Data Update", (mon.get("last_data_update") or "—")[-8:])
    c3.metric("Last Candle Evaluated", (mon.get("last_monitored_candle") or "—")[-8:])


def page_dashboard(status: dict) -> None:
    now = ist_now()
    trading_date = str(now.date())
    settings = load_settings()
    store = _store_or_none()
    client = GrowwClient(settings)

    # Initialize session state keys
    st.session_state.setdefault("prepared_data", None)
    st.session_state.setdefault("locked_candidates", None)
    st.session_state.setdefault("monitoring_active", False)
    st.session_state.setdefault("monitoring_state", None)
    st.session_state.setdefault("board", None)
    st.session_state.setdefault("signals", [])
    st.session_state.setdefault("known_ids", set())
    st.session_state.setdefault("gap_message", "")
    st.session_state.setdefault("last_scan_error", "")

    # Multi-client Firestore state restoration (Odisha client & Maharashtra admin)
    if st.session_state.get("last_synced_date") != trading_date:
        restored = restore_today_state(store, trading_date)
        if restored.get("locked"):
            st.session_state["locked_candidates"] = restored["locked"]
        if restored.get("monitoring_state"):
            mon = restored["monitoring_state"]
            st.session_state["monitoring_state"] = mon
            if mon.get("state") == "MONITORING":
                st.session_state["monitoring_active"] = True
            elif mon.get("state") == "COMPLETED":
                st.session_state["monitoring_active"] = False
        if restored.get("signals"):
            st.session_state["signals"] = restored["signals"]
            for s in restored["signals"]:
                if s.get("id"):
                    st.session_state["known_ids"].add(s["id"])
        st.session_state["last_synced_date"] = trading_date

    locked_candidates = st.session_state.get("locked_candidates")
    prepared_data = st.session_state.get("prepared_data")
    monitoring_active = st.session_state.get("monitoring_active", False)
    monitoring_state = st.session_state.get("monitoring_state") or {}

    cutoff_reached = monitoring_state.get("cutoff_reached") or (now.time().replace(microsecond=0) >= parse_clock(settings.get("setup3_end", "11:00")).replace(microsecond=0))

    # Determine status badge
    if cutoff_reached and locked_candidates:
        current_badge = "COMPLETED"
        badge_color = "#059669"
        badge_desc = "11:00 AM IST cutoff reached. Monitoring complete for today."
    elif monitoring_active:
        current_badge = "MONITORING"
        badge_color = "#16a34a"
        badge_desc = "Setup evaluation running every 30s on completed 5-min candles."
    elif locked_candidates:
        current_badge = "LOCKED"
        badge_color = "#0284c7"
        badge_desc = f"Candidate list locked for {trading_date}. Ready for setup monitoring."
    elif prepared_data:
        current_badge = "PREPARED"
        badge_color = "#9333ea"
        badge_desc = "Candidates prepared from live scan. Click SAVE CANDIDATES to lock."
    elif st.session_state.get("last_scan_error"):
        current_badge = "ERROR"
        badge_color = "#dc2626"
        badge_desc = st.session_state["last_scan_error"]
    else:
        current_badge = "READY"
        badge_color = "#4b5563"
        badge_desc = "Ready for candidate scan. At ~9:30 AM IST, click SCAN STOCKS."

    # Render top Status Bar
    st.markdown(
        f"""
        <div style="background-color: {badge_color}12; border-left: 5px solid {badge_color}; padding: 12px 18px; border-radius: 6px; margin-bottom: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;">
                <div>
                    <span style="background-color: {badge_color}; color: white; padding: 4px 10px; border-radius: 4px; font-weight: bold; font-size: 0.85rem; letter-spacing: 0.05em;">{current_badge}</span>
                    <span style="margin-left: 12px; font-weight: 500; color: #1e293b;">{badge_desc}</span>
                </div>
                <div style="color: #64748b; font-size: 0.85rem; font-family: monospace;">
                    {trading_date} · {now.strftime('%H:%M:%S IST')} · Cloud Sync Active
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if st.session_state.get("gap_message"):
        st.warning(st.session_state["gap_message"])

    # -------------------------------------------------------------
    # PHASE A: SCAN STOCKS
    # -------------------------------------------------------------
    st.markdown("### Phase A: Prepare Candidates")
    if locked_candidates:
        st.info(f"Candidate list is LOCKED for trading day {trading_date}. Overwrite is blocked.")
    else:
        col_scan, col_info = st.columns([1, 2])
        with col_scan:
            scan_clicked = st.button("SCAN STOCKS", type="primary", use_container_width=True)
        with col_info:
            st.caption("Validates market time (~9:30 AM IST), authenticates with Groww, calculates 60/40 market breadth, and filters top movers <= 5%.")

        if scan_clicked:
            st.session_state["last_scan_error"] = ""
            prog = st.progress(0, text="Initializing market scan...")
            def _progress_cb(pct, text):
                prog.progress(pct, text=text)

            res = prepare_candidates_scan(client, store, settings, now, progress_callback=_progress_cb)
            if res.get("valid"):
                st.session_state["prepared_data"] = res
                st.session_state["last_scan_error"] = ""
                st.success("Candidate scan completed successfully! Review candidates below.")
                st.rerun()
            else:
                err = res.get("error") or "Unknown error occurred during scan."
                st.session_state["last_scan_error"] = err
                st.error(err)

    # -------------------------------------------------------------
    # PHASE B: SAVE CANDIDATES
    # -------------------------------------------------------------
    if prepared_data and not locked_candidates:
        st.markdown("---")
        st.markdown("### Phase B: Review & Save Candidates")

        market = prepared_data.get("market") or {}
        st.subheader(f"Market Breadth: {market.get('status') or 'UNKNOWN'}")
        st.write(market.get("reason") or "")

        _market_block(market, "Market Breadth Summary")

        st.markdown("#### Candidate List (Unsaved Snapshot)")
        cand_list = prepared_data.get("candidates") or []
        if cand_list:
            cand_df = pd.DataFrame(cand_list)
            keep_cols = [c for c in ["symbol", "name", "ltp", "change_pct", "sources", "current_price_status", "data_updated"] if c in cand_df.columns]
            st.dataframe(cand_df[keep_cols], use_container_width=True, hide_index=True)
        else:
            st.warning("No eligible candidates found. Market direction might be Neutral/Opposite or all candidates exceeded 5% move.")

        # Method status explanations
        st.markdown("**Stock Selection Methods Status:**")
        st.write("- **Method 1 (Top F&O Movers):** Active. Evaluates top 5 gainers (BULLISH) or top 5 losers (BEARISH) within 5% move cap.")
        st.write("- **Method 2 (Sector Movers):** **BLOCKED**. Missing verified Groww index prices for required sectors (Energy, India Defence).")
        st.write("- **Method 3 (NSE-wide Movers):** **BLOCKED**. Requires authorized NSE-wide ranking feed.")
        st.write("- **Filter Rule:** Maximum absolute daily movement <= 5.0%.")

        direction = market.get("direction")
        can_save = bool(direction and cand_list and market.get("status") in ("BULLISH", "BEARISH"))

        col_save, col_save_info = st.columns([1, 2])
        with col_save:
            save_clicked = st.button("SAVE CANDIDATES", type="primary", disabled=not can_save, use_container_width=True)
        with col_save_info:
            if not can_save:
                st.caption("SAVE CANDIDATES is disabled because market direction is not BULLISH/BEARISH or candidate list is empty.")
            else:
                st.caption("Locks candidate list in Firestore for the trading day. Cannot be overwritten.")

        if save_clicked and can_save:
            ok, msg, locked_doc = lock_and_save_candidates(store, prepared_data, now, settings)
            if ok:
                st.session_state["locked_candidates"] = locked_doc
                st.session_state["prepared_data"] = None
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

    # -------------------------------------------------------------
    # PHASE C: SETUP MONITORING
    # -------------------------------------------------------------
    if locked_candidates:
        st.markdown("---")
        st.markdown("### Phase C: Setup Monitoring")
        locked_market = locked_candidates.get("market") or {}
        locked_dir = locked_candidates.get("direction") or "NONE"
        locked_time = locked_candidates.get("locked_at") or "—"
        locked_count = len(locked_candidates.get("candidates") or [])

        st.caption(f"Locked Direction: **{locked_dir}** · Locked At: **{locked_time}** · Candidates: **{locked_count}** · Cutoff: **11:00 AM IST**")

        col_mon1, col_mon2 = st.columns(2)
        with col_mon1:
            if cutoff_reached:
                st.button("MONITORING COMPLETED", disabled=True, use_container_width=True)
            elif not monitoring_active:
                if st.button("START MONITORING", type="primary", use_container_width=True):
                    st.session_state["monitoring_active"] = True
                    st.rerun()
            else:
                if st.button("PAUSE MONITORING", use_container_width=True):
                    st.session_state["monitoring_active"] = False
                    st.rerun()

        with col_mon2:
            if st.button("EVALUATE NOW (Manual Refresh)", use_container_width=True):
                eval_res = evaluate_monitoring_cycle(
                    client, store, locked_candidates, now, settings,
                    known_ids=st.session_state["known_ids"],
                )
                st.session_state["board"] = eval_res.get("board")
                st.session_state["signals"] = eval_res.get("signals")
                st.session_state["monitoring_state"] = eval_res.get("monitoring_state")
                if eval_res.get("gap_detected"):
                    st.session_state["gap_message"] = eval_res.get("gap_message")
                st.success("Evaluation cycle completed.")
                st.rerun()

        # Dynamic Fragment for 30s Periodic Auto-refresh
        _monitoring_fragment(client, store, locked_candidates, settings)

    # -------------------------------------------------------------
    # PHASE D: FINAL RESULTS & INSPECTOR
    # -------------------------------------------------------------
    if locked_candidates:
        st.markdown("---")
        st.markdown("### Phase D: Final Results & Signals")

        # If board not built yet, build initial board
        board = st.session_state.get("board")
        if not board:
            board = build_board(
                locked_candidates.get("market") or {},
                locked_candidates.get("candidates") or [],
                locked_candidates.get("considered") or [],
                st.session_state.get("signals") or [],
                locked_candidates.get("notes") or {},
                trading_date,
                data_updated=now.strftime("%Y-%m-%d %H:%M:%S"),
                locked=True,
            )
            st.session_state["board"] = board

        summary = board.get("summary") or {}
        cols = st.columns(5)
        cols[0].metric("BUY / LONG", len(summary.get("buy") or []))
        cols[1].metric("SHORT", len(summary.get("short") or []))
        cols[2].metric("WAITING", len(summary.get("wait") or []))
        cols[3].metric("EXCLUDED", summary.get("excluded", 0))
        cols[4].metric("BLOCKED", summary.get("blocked", 0))

        if summary.get("buy"):
            st.success("BUY / LONG: " + ", ".join(summary["buy"]))
        if summary.get("short"):
            st.error("SHORT: " + ", ".join(summary["short"]))
        if summary.get("wait"):
            st.info("WAIT: " + ", ".join(summary["wait"]))
        if not summary.get("buy") and not summary.get("short"):
            st.info("No BUY or SHORT triggers yet. Candidates are waiting for qualifying candle setups.")

        _market_block(locked_candidates.get("market"), "Locked Market Breadth")

        rows = board.get("rows") or []
        st.subheader("Decision Board")
        if rows:
            choice = st.selectbox("Filter Decisions", ["BUY, SHORT and WAIT", "All decisions", "BUY", "SHORT", "WAIT", "EXCLUDED", "BLOCKED"], key="filter_decision_choice")
            visible = rows
            if choice == "BUY, SHORT and WAIT":
                visible = [r for r in rows if r["Final Decision"] in ("BUY", "SHORT", "WAIT")]
            elif choice != "All decisions":
                visible = [r for r in rows if r["Final Decision"] == choice]

            frame = pd.DataFrame(visible)
            hidden = [col for col in ("checks", "proof") if col in frame.columns]
            shown = frame.drop(columns=hidden) if len(frame) else frame
            if len(shown) and "Final Decision" in shown.columns:
                styled = shown.style.map(_paint, subset=["Final Decision"])
                st.dataframe(styled, use_container_width=True, hide_index=True)
            else:
                st.dataframe(shown, use_container_width=True, hide_index=True)

            st.download_button(
                "Download this table as CSV",
                shown.to_csv(index=False) if len(shown) else "Symbol\n",
                file_name=f"nse_fno_decisions_{trading_date}.csv",
                mime="text/csv",
                use_container_width=True,
            )

            if visible:
                symbol = st.selectbox("Inspect one stock", [r["Symbol"] for r in visible], key="inspect_stock_choice")
                chosen = next(r for r in visible if r["Symbol"] == symbol)
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

        # Signals History Table
        all_signals = st.session_state.get("signals") or []
        st.subheader(f"Stored Live Signals ({len(all_signals)})")
        if all_signals:
            sig_frame = pd.DataFrame(all_signals)
            sig_cols = [c for c in ["symbol", "direction", "setup", "candle_time", "open", "high", "low", "close", "volume", "sources"] if c in sig_frame.columns]
            st.dataframe(sig_frame[sig_cols], use_container_width=True, hide_index=True)
        else:
            st.caption("No signals triggered yet today.")

    _cloud_caption()
    st.caption(f"App Active · Session Date: {trading_date} · Signals only. No automatic trading.")


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
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
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
        width="stretch",
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
    st.dataframe(frame[keep], width="stretch", hide_index=True)
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
    st.dataframe(frame[columns], width="stretch", hide_index=True)


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
    if st.button("Test setup rules", type="primary", width="stretch"):
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
                st.dataframe(pd.DataFrame(alerts), width="stretch", hide_index=True)
            else:
                st.warning("No setup matched these candles.")
            st.dataframe(pd.DataFrame(explained["checks"]), width="stretch", hide_index=True)
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
            st.dataframe(frame[frame["eligible"]], width="stretch", hide_index=True)
        else:
            st.warning("The CSV needs a change_pct column.")
    st.subheader("Historical test")
    st.caption("Historical output stays on this page and is never stored as a live signal.")
    history_text = st.text_area("Historical candles", value=example, height=140, key="historical_candles")
    move = st.number_input("Daily move percent for the eligibility check", value=1.0, format="%.2f")
    if st.button("Run historical test", width="stretch"):
        try:
            result = run_historical(nifty_adv, nifty_dec, fno_adv, fno_dec, parse_candle_text(history_text), direction, pdh, pdl, move)
            st.warning(result["note"])
            if result["alerts"]:
                st.dataframe(pd.DataFrame(result["alerts"]), width="stretch", hide_index=True)
            else:
                st.info("The historical candles produced no qualifying setup.")
        except Exception as exc:
            st.error(f"Input error: {exc}")
    st.subheader("Rule validation report")
    st.caption("This replays fixed completed candles for all three setups. It does not calculate profit and it does not create a live signal.")
    if st.button("Run historical rule validation", width="stretch"):
        validation = write_validation_report()
        if validation["passed"]:
            st.success(f"Passed {validation['case_count']} historical rule checks.")
        else:
            st.error(f"{validation['failed_count']} historical rule checks failed.")
        st.caption(validation["setup3_discrepancy"])
        st.caption(validation["reversal_difference"])
        st.caption(validation["profitability"])
        st.dataframe(pd.DataFrame(validation["cases"]), width="stretch", hide_index=True)


def _store_or_none():
    try:
        if not secret_status()["firebase_service_account"]:
            return None
        return FirestoreStore.connect()
    except Exception as exc:
        st.warning(f"Firestore is not available: {exc}")
        return None


def _cloud_caption() -> None:
    try:
        view = load_budget().public_view()
    except Exception:
        return
    if view.get("state") == "open" and not view.get("pending"):
        st.caption(view.get("status") or "")
        return
    st.warning(view.get("status") or "Firestore sync is paused. The local scan continues.")


def _usage_table() -> None:
    try:
        view = load_budget().public_view()
    except Exception as exc:
        st.caption(f"Firestore usage is not available: {exc}")
        return
    st.subheader("Firestore usage")
    st.caption("Estimated by this application for the current Pacific quota day. This is not an official Firebase quota.")
    st.dataframe(pd.DataFrame([
        {"Check": "Circuit breaker", "State": view.get("state")},
        {"Check": "Estimated writes today", "State": view.get("writes")},
        {"Check": "Estimated reads today", "State": view.get("reads")},
        {"Check": "Estimated deletes today", "State": view.get("deletes")},
        {"Check": "Failed operations", "State": view.get("failed")},
        {"Check": "Skipped duplicate writes", "State": view.get("skipped")},
        {"Check": "Pending local sync", "State": view.get("pending")},
        {"Check": "Write safety target", "State": view.get("write_target")},
        {"Check": "Next reset", "State": view.get("reset_at") or "not waiting"},
        {"Check": "Next retry", "State": view.get("retry_at") or "not waiting"},
    ]), width="stretch", hide_index=True)
    st.write(view.get("status") or "")


def page_history(_status: dict) -> None:
    st.caption("History is read from the local cache. Opening this page does not query Firestore.")
    trading_date = st.text_input("Trading date (YYYY-MM-DD)", value="")
    symbol = st.text_input("Stock symbol", value="").upper().strip()
    setup = st.selectbox("Setup", ["All", "SETUP 1", "SETUP 2", "SETUP 3"])
    direction = st.selectbox("Direction", ["All", "LONG", "SHORT"])
    source = st.text_input("Selection source contains", value="")
    rows = list(cached_history_rows())
    for row in _status.get("signals") or []:
        if isinstance(row, dict) and row.get("id") and row.get("id") not in {item.get("id") for item in rows}:
            rows.append(row)
    filtered = []
    for row in rows:
        if not isinstance(row, dict) or row.get("origin") not in (None, "live", "developer"):
            continue
        if trading_date and row.get("trading_date") != trading_date:
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
    st.dataframe(pd.DataFrame(filtered), width="stretch", hide_index=True)


def page_reports(_status: dict) -> None:
    st.caption("Daily reports are read from the local cache. Opening this page does not query Firestore.")
    rows = cached_reports()
    live = [row for row in rows if row.get("origin") in ("live", "developer")]
    if not live:
        st.info("No live daily report is stored yet.")
        return
    report_frame = pd.DataFrame(live)
    columns = [column for column in ["trading_date", "market_status", "candidate_count", "signal_count", "note"] if column in report_frame.columns]
    st.dataframe(report_frame[columns], width="stretch", hide_index=True)
    choice = st.selectbox("Open report", [row["trading_date"] for row in live])
    selected = next(row for row in live if row["trading_date"] == choice)
    st.json({key: value for key, value in selected.items() if key != "signals"})
    if selected.get("signals"):
        st.dataframe(pd.DataFrame(selected["signals"]), width="stretch", hide_index=True)


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
        {"Check": "Firestore circuit breaker", "State": (health.get("firestore") or {}).get("state") or "not reported"},
        {"Check": "Pending local sync", "State": (health.get("firestore") or {}).get("pending", 0)},
        {"Check": "Last scanner update", "State": status.get("updated_at") or "none"},
        {"Check": "Latest error", "State": health.get("last_error") or "none"},
    ])
    st.dataframe(rows, width="stretch", hide_index=True)
    permissions = settings.get("last_groww_permissions") or {}
    if permissions.get("results"):
        st.subheader("Groww permission checks")
        st.caption("Authentication can succeed while live data, history, or open interest stays forbidden. A failed check is never filled with a sample price.")
        st.dataframe(pd.DataFrame(permissions["results"]), width="stretch", hide_index=True)
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
        st.dataframe(pd.DataFrame({"error": health["errors"]}), width="stretch", hide_index=True)
    _usage_table()


def page_settings(_status: dict) -> None:
    settings = load_settings()
    st.subheader("Developer mode")
    developer = st.checkbox("Developer mode", value=bool(settings.get("developer_mode")))
    st.caption(
        "Testing only. Market hours, weekends, holidays, quote age, and the 9:30 deadline do not block a test. "
        "Test files stay in the developer folder. Test database records use separate collections. "
        "Production mode keeps the current rules and the current files while this is off."
    )
    if developer != bool(settings.get("developer_mode")):
        save_settings({"developer_mode": developer})
        st.rerun()
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
    if left.button("Start scanner", width="stretch", disabled=running):
        ok, message = start_worker()
        st.session_state["control_message"] = message if ok else message
    if right.button("Stop scanner", width="stretch"):
        ok, message = stop_worker()
        st.session_state["control_message"] = message
    if third.button("Restart scanner", width="stretch"):
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
        if st.button("Save Groww settings", width="stretch"):
            save_settings({"groww_auth_mode": mode})
            update_secrets(groww_api_key=api_key, groww_api_secret=api_secret, groww_totp_secret=totp_secret)
            st.success("Groww settings saved. Secrets stay outside the project and are not shown again.")
        if st.button("Test Groww permissions", width="stretch"):
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
            st.dataframe(pd.DataFrame(results), width="stretch", hide_index=True)
            st.caption("A successful login does not mean live prices, candles, or open interest are included in the subscription.")

    with st.expander("Firebase", expanded=False):
        uploaded = st.file_uploader("Service account JSON", type=["json"])
        st.caption(f"Service account: {'saved' if secrets['firebase_service_account'] else 'missing'}")
        if uploaded is not None and st.button("Save service account", width="stretch"):
            try:
                payload = json.loads(uploaded.getvalue().decode("utf-8"))
                if payload.get("type") != "service_account" or not payload.get("private_key"):
                    raise ValueError("This file is not a Firebase service account JSON.")
                update_secrets(firebase_service_account=payload)
                st.success("Service account stored outside the project. Its contents are not displayed.")
            except Exception as exc:
                st.error(str(exc))
        target = st.number_input(
            "Firestore write safety target",
            min_value=100,
            value=int(settings.get("firestore_write_target") or 2000),
            help="Application estimate for one trading day. This does not change the Firebase quota.",
        )
        st.caption("Nonessential cloud writes pause at this estimate. Signals, the locked list, and the daily report can still sync. This is not an official Firebase quota.")
        if int(target) != int(settings.get("firestore_write_target") or 2000):
            save_settings({"firestore_write_target": int(target)})
            st.rerun()
        _usage_table()
        if st.button("Test Firestore connection", width="stretch"):
            try:
                ok, message = FirestoreStore.connect().check_connection()
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
        if st.button("Save market settings", width="stretch"):
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
        if sector_file is not None and st.button("Save sector fallback", width="stretch"):
            destination = lists_dir() / "sectors.csv"
            destination.write_bytes(sector_file.getvalue())
            try:
                mapping = load_sector_csv(destination)
                ok, message = save_manual_sectors(mapping, datetime.now())
            except ValueError as exc:
                ok, message = False, str(exc)
            st.success(message) if ok else st.error(message)
        if st.button("Save selection settings", width="stretch"):
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
            st.dataframe(pd.DataFrame({"symbol": nifty_symbols}), width="stretch", hide_index=True)
        if sector_errors:
            st.warning(" ".join(sector_errors))
        elif sector_symbols:
            st.success("All 13 sector constituent lists are usable.")
        if st.button("Refresh official NSE lists", width="stretch"):
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
        if nifty_file is not None and st.button("Save Nifty 50 fallback", width="stretch"):
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
        if st.button("Refresh Groww instrument master", width="stretch"):
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
                st.dataframe(pd.DataFrame(derived)[["symbol", "name"]], width="stretch", hide_index=True)
        symbols = universe.get("symbols") or []
        if symbols and universe.get("hash"):
            st.caption(f"Derived F&O equities: {universe.get('count', len(symbols))}. Hash {universe.get('hash', '')[:12]}.")
            state = "approved" if settings.get("fno_universe_approved") else "not approved"
            st.caption(f"This derived list is {state}.")
            preview = pd.DataFrame(symbols)
            columns = [column for column in ["symbol", "name"] if column in preview.columns]
            st.dataframe(preview[columns], width="stretch", hide_index=True)
            if st.button("Approve derived F&O universe", width="stretch"):
                save_settings({"fno_universe_approved": True, "fno_universe_hash": universe.get("hash", "")})
                st.success("F&O universe approved.")
            if st.button("Clear F&O universe approval", width="stretch"):
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
        if st.button("Save notifications", width="stretch"):
            save_settings({
                "telegram_enabled": telegram, "telegram_chat_id": chat, "email_enabled": email,
                "smtp_host": host, "smtp_port": port, "smtp_username": username,
                "smtp_from": sender, "smtp_to": recipient,
            })
            update_secrets(telegram_token=token, smtp_password=password)
            st.success("Notification settings saved.")
        if st.button("Send test notification", width="stretch"):
            from nse_scanner.notify import notify_all
            st.write(notify_all(load_settings(), "NSE F&O scanner test message. This is not a trade signal."))

    with st.expander("System", expanded=False):
        health = _status.get("health") or {}
        st.write(f"Trading date: {_status.get('trading_date') or '—'}")
        st.write(f"Last scan update: {_status.get('updated_at') or '—'}")
        st.write(f"Database: {health.get('database') or 'not connected'}")
        st.write(f"Data freshness window: {settings.get('quote_max_age_seconds')} seconds")
        age = int(st.number_input("Quote freshness seconds", min_value=5, value=int(settings["quote_max_age_seconds"])))
        if st.button("Save freshness window", width="stretch"):
            save_settings({"quote_max_age_seconds": age})
        log_path = runtime_dir() / "scanner.log"
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
            st.text("\n".join(lines) or "Log is empty.")
        else:
            st.caption("No scanner log yet.")
        if not settings.get("wizard_seen"):
            save_settings({"wizard_seen": True})
