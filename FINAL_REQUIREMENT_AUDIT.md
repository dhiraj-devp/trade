# Final requirement audit

Audit date: 21 November 2026. Automated result: `python -m pytest -q` — **36 passed, 0 failed**.

This pass did not call the live Groww account. A permission check that raises “Access forbidden” is covered by a unit test only. Authentication success is not treated as a live-data subscription.

No order placement was added. `tests/test_scanner_flow.py::test_source_does_not_place_orders` passed.

The official NSE constituent files were fetched by the application code on 21 November 2026. The cached result was 50 Nifty 50 symbols and all 13 sector lists, with no file errors. Cache path: the app home `lists` folder, not the project folder.

## Strategy rules that were not changed

| Requirement | File | Status | Test | Remaining dependency | Approval |
|---|---|---|---|---|---|
| Bullish only when both groups are at least 60% advances and at most 40% declines. Bearish is the reverse. Opposite, tie, or a failed gate selects nothing. | `scanner_rules.py` | Implemented. Unchanged. | `tests/test_rules.py` | None | No |
| Unchanged prices are neither advances nor declines. Default denominator is advances plus declines. | `scanner_rules.py` | Implemented. Alternate denominator stays off until approved. | `tests/test_rules.py` | None for the default | Yes, to include unchanged stocks |
| Absolute move of 5% or less stays eligible. Exactly 5.0 stays eligible. | `scanner_rules.py` | Implemented. Unchanged. | `tests/test_rules.py` | None | Yes, to change the percentage |
| Setup 1 and Setup 2, including the inclusive 2.5% and 1% caps. | `scanner_rules.py` | Implemented. Unchanged. | `tests/test_rules.py`, `tests/test_completion.py` | None | Yes, to change the caps |
| Candidate lock uses the last fresh complete snapshot at or before 9:30 IST. A missing or stale snapshot locks nobody. | `nse_scanner/engine.py`, `nse_scanner/worker.py` | Implemented. Unchanged. | Existing rule tests | A weekday session with fresh Groww prices | No |
| Signals are stored once per date, symbol, setup, candle time, and direction. | `scanner_rules.py`, `nse_scanner/engine.py` | Implemented. | `test_early_candles_create_one_live_signal_and_do_not_repeat`, historical report | Firestore must be connected for the live copy | No |
| The scanner does not place orders and does not invent a missing price. | `nse_scanner/worker.py`, `nse_scanner/groww_client.py` | Implemented. | `test_source_does_not_place_orders` | None | No |

## Task 1 — Nifty 50 constituents

| Requirement | File | Status | Test | Remaining dependency | Approval |
|---|---|---|---|---|---|
| Retrieve the official Nifty 50 list from a reliable source. | `nse_scanner/constituents.py` | Implemented. Source: `https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv`. A response without a Symbol column, or a count other than 50, is rejected. | Live fetch on 21 Nov 2026 returned 50 symbols. `test_official_nifty_list_is_cached_and_a_change_is_recorded` | NSE must be reachable. Groww cash master must contain every symbol before the list is used for scanning. | No, for this official file |
| Cache the latest verified list and remember the last success. | `nse_scanner/constituents.py` | Implemented. Stored outside the project. | Same test | None | No |
| Detect constituent changes. | `nse_scanner/constituents.py` | Implemented. Added and removed symbols are stored when the hash changes. | Same test, with `NEWCO` added and `S49` removed | None | No |
| Validate symbols against Groww instruments. | `nse_scanner/constituents.py` `apply_cash_validation`, `nse_scanner/worker.py` | Implemented. Any symbol missing from NSE cash EQ blocks the whole list. | `test_nifty_symbols_missing_from_groww_block_the_list` | The worker performs this check after the Groww instrument file loads. It has not been run against today’s live master in this audit. | No |
| Manual CSV fallback. | `nse_scanner/constituents.py`, Settings in `nse_scanner/ui.py` | Implemented. Used only when the official list is unusable. The CSV must contain exactly 50 symbols. | `test_invalid_or_outdated_nifty_list_is_not_used` | The user must upload a file only if the official fetch fails | No |
| Never silently use an invalid or outdated list. | `nse_scanner/constituents.py` | Implemented. A bad download keeps the last good list and says why. A list older than 168 hours is refused. | `test_invalid_or_outdated_nifty_list_is_not_used` | None | No |

## Task 2 — 13-sector selection

| Requirement | File | Status | Test | Remaining dependency | Approval |
|---|---|---|---|---|---|
| Use verified constituent lists for Realty, FMCG, PSU Bank, Financial Services, Pharma, Bank, Auto, IT, Metal, Energy, Media, Mid Select, and India Defence. | `nse_scanner/constituents.py` `SECTOR_URLS` | Implemented. Each URL was fetched live and returned a Symbol column. Counts on 21 Nov 2026: Realty 10, FMCG 15, PSU Bank 12, Financial Services 20, Pharma 20, Bank 14, Auto 15, IT 10, Metal 15, Energy 40, Media 10, Mid Select 25, India Defence 22. No membership was typed in. | Live fetch: 13 lists, no errors. Unit test uses fixture files. | A sector file that later fails, and has no fresh cache or manual fallback, removes that sector. The sector method then selects nobody and names the missing sector. | No, for these official files |
| Do not invent index mappings. | `nse_scanner/worker.py` | Implemented. Sector performance is the average change of official constituents that are also in the F&O equity universe. Exact Groww index-symbol matches are displayed only. A name that is not in the instrument master is not guessed. | `test_sector_method_uses_verified_members_and_keeps_sources` | If a sector’s F&O constituents do not all have a fresh price, that sector has no move and the sector method selects nobody. | Yes, only if index-point moves should replace the constituent average |
| Top two sectors in the market direction, then top five eligible F&O stocks, with the 5% filter. | `scanner_rules.py`, `nse_scanner/engine.py` | Implemented when all 13 sector moves exist. A stock above 5% is left out. | `test_sector_method_uses_verified_members_and_keeps_sources` | All 13 sectors need a complete F&O price set at the snapshot | No |
| Keep every source and deduplicate. | `scanner_rules.py` `merge_candidates` | Implemented. | Same test, and `test_candidate_selection_merges_sources_and_blocks_unapproved_oi` | None | No |
| Show mapping errors and missing constituents. | `nse_scanner/worker.py`, Settings and API Status | Implemented. Missing files, missing F&O overlap, and missing prices are written into the sector note. | Unit test expects `Media` when that sector move is absent | The dashboard shows the worker’s published list status after a scan | No |

Sector selection is on unless a saved Settings choice turns it off. An old settings file that had the method off only because no list existed is treated as not yet chosen, so the official lists are used.

## Task 3 — OI spurts

| Requirement | File | Status | Test | Remaining dependency | Approval |
|---|---|---|---|---|---|
| Keep top five price gainers and losers. | `nse_scanner/engine.py` | Implemented. Unchanged. | `test_candidate_selection_merges_sources_and_blocks_unapproved_oi` | Groww live prices | No |
| Do not treat price gainers as OI spurts. | `nse_scanner/engine.py` | Implemented. The OI note says price gainers are not used. | Same test | None | No |
| Keep OI ranking off until the formula is approved, and require validated OI first. | `nse_scanner/engine.py`, `nse_scanner/settings.py` | Implemented. Both `oi_definition_approved` and a successful OI permission check are required. The definition remains nearest-expiry stock future `oi_day_change`. It is not enabled. | Same test | Client approval of that formula, plus a Groww quote that actually returns `oi_day_change` | Yes |

## Task 4 — Setup 3 timing

| Requirement | File | Status | Test | Remaining dependency | Approval |
|---|---|---|---|---|---|
| Review “scan until 11:00”. | `scanner_rules.py`, `nse_scanner/historical.py` | Documented. The candle timestamp is the bar start. The current comparison keeps a timestamp of 11:00, so the 11:00–11:05 candle is included. A reading that stops at 11:00 would end with the 10:55–11:00 candle. | `test_setup_three_requires_strictly_lower_volume_until_eleven` | None | No, for the finding |
| Do not change the rule silently. Provide an approved option. | `scanner_rules.py`, `nse_scanner/engine.py`, Settings | Implemented. The default still includes the 11:00 candle. Unchecking it is saved only with the approval checkbox. Without that approval, the 11:00 candle stays included. | `test_reversal_protection_stays_on_until_approved`, historical report | None | Yes, to exclude the 11:00–11:05 candle |

## Task 5 — Breadth after the 9:30 lock

| Requirement | File | Status | Test | Remaining dependency | Approval |
|---|---|---|---|---|---|
| Keep the post-lock breadth check as a configurable protection, and document the difference. | `nse_scanner/engine.py`, `nse_scanner/worker.py`, `nse_scanner/historical.py` | Implemented. Protection is on by default: a new signal is stored only while the live breadth is complete and still matches the locked direction. With an explicit approval, the locked names continue to be scanned after the breadth changes. That second behaviour is not applied unless the approval checkbox is saved. | `test_reversal_protection_stays_on_until_approved` | None | Yes, to turn the protection off |

The original lock still chooses the names at 9:30. The protection is an extra gate on later signals. It is the behaviour the scanner already used, and it remains the default.

## Task 6 — Historical validation

| Requirement | File | Status | Test | Remaining dependency | Approval |
|---|---|---|---|---|---|
| Replay completed candles for all three setups, compare actual with expected, and check duplicate suppression. | `nse_scanner/historical.py` | Implemented. The Stock Scanner page runs it and writes `runtime/historical_validation_report.json`. | `test_historical_validation_report_passes_without_claiming_profit` passed all cases in that report | This replays fixed candles. It does not download a past market day from Groww. | No |
| Do not claim profitability. | `nse_scanner/historical.py` | The report says profit is not calculated because no entry or exit rule is defined. | Same test | An entry and exit rule, if profit is ever required | Yes, before any profit figure is added |

## Task 7 — Groww permission checks

| Requirement | File | Status | Test | Remaining dependency | Approval |
|---|---|---|---|---|---|
| Separate authentication, instrument master, live LTP, live quote, streaming feed, 5-minute history, and OI. | `nse_scanner/groww_client.py`, Settings and API Status | Implemented. The button is “Test Groww permissions”. Each row has its own result. A forbidden data call is labelled entitlement, not a failed login. Empty or missing prices are not replaced. | `test_permission_probe_does_not_treat_forbidden_data_as_a_failed_login` | This audit did not press the live button. The user’s subscription still has to be checked from Settings. A trial key can authenticate and still be forbidden for live data, history, and OI. | No |
| Readiness uses authentication, not a successful LTP, as the Groww connection check. | `nse_scanner/readiness.py`, `nse_scanner/ui.py` | Implemented. Live scanning still fails closed when a later price or candle call fails. | Readiness tests | A successful login does not by itself prove live data | No |

## Task 8 — Other original behaviour still open

| Requirement | File | Status | Test | Remaining dependency | Approval |
|---|---|---|---|---|---|
| F&O equity universe from the Groww instrument master: NSE F&O underlying that also has NSE cash series EQ. | `nse_scanner/universe.py` | Implemented. Scanning still requires the derived list to be approved, and approval expires when the hash changes. | `test_fno_universe_keeps_equities_and_drops_index_underlyings` | Press “Approve derived F&O universe” after a refresh | Yes |
| Separate NSE gainer/loser ranking. | `nse_scanner/engine.py` | Not a separate feed. The option uses the same Groww prices as top movers, and the note says so. | Candidate selection test | An NSE ranking source, if one is required to differ from Groww prices | Yes, before treating it as a different ranking |
| Firestore storage and a worker that is separate from the dashboard. | `nse_scanner/store.py`, `nse_scanner/worker.py` | Implemented. On Saturday and Sunday the worker returns before it connects, so the dashboard can say the database is not connected on a weekend even after a successful Firebase test. | Not a network test | Firebase credentials, which were already tested successfully on 21 Nov 2026 | No |
| Optional Telegram and email. | `nse_scanner/notify.py` | Implemented. Off unless enabled. | Not executed against a live bot in this audit | Bot token or SMTP details | No |
| Android-friendly dashboard. | `nse_scanner/ui.py` | Implemented as the existing Streamlit layout. | Not a device test in this audit | Open the Streamlit address in the phone browser | No |

## What this audit does not call complete

- OI ranking. The formula is still unapproved, and live `oi_day_change` was not read from Groww in this run.
- A distinct NSE gainer/loser feed.
- Profit, loss, or an exit rule.
- Automatic order placement.
- Today’s Groww subscription. Use Settings → Test Groww permissions and read each row.
- The F&O universe approval button. The derived list is still gated.
- A full weekday scan. Today is Saturday, so the worker does not open the market loop.
