# NSE F&O Intraday Scanner

English dashboard and background scanner for the NSE F&O rules. It watches the market and records signals. It does not place orders.

Live numbers appear only after Groww returns them. Missing or stale data is left blank. The screen does not fill a gap with an example price.

## Start

```text
pip install -r requirements.txt
python run.py
```

`python run.py` starts one background scanner, then the Streamlit dashboard. Closing that window stops both. A second copy does not start another scanner.

The sidebar has four sections: Dashboard, Strategy Checklist, History, and Settings.

Credentials are stored outside this folder:

- Windows: `%LOCALAPPDATA%\nse_fno_scanner`
- Linux: `~/.config/nse_fno_scanner`

Do not commit that folder, a service-account JSON file, or `.streamlit/secrets.toml`.

## Schedule

The clock is Asia/Kolkata. The worker checks about every 5 seconds.

- Before 09:15 it waits and loads previous-day candles.
- From 09:15 to 09:30 it builds breadth and a candidate snapshot.
- At 09:30 it locks the last fresh snapshot. A snapshot is usable only if it was taken at or before 09:30 and its prices are no older than 30 seconds.
- After the lock it scans only that candidate list on completed 5-minute candles.
- At 15:30 it stops the live scan.
- Saturday, Sunday, and any date in `lists/holidays.csv` stay closed. If that file is missing, only weekends are closed.

Later prices do not rebuild the 9:30 list.

## Market direction

Two groups must agree: the official Nifty 50 list, and the NSE F&O equity universe.

An unchanged price is neither an advance nor a decline. The percentage is advances divided by advances plus declines.

- Bullish: both groups have advances of at least 60% and declines of at most 40%. Direction is LONG.
- Bearish: both groups have declines of at least 60% and advances of at most 40%. Direction is SHORT.
- Neutral, opposite, incomplete, or stale: no stock is selected.

The Nifty 50 list is downloaded from the official NSE constituent file, cached, and checked against Groww's cash list. A bad download keeps the last good file. A file older than seven days is refused. A manual 50-symbol CSV is only a fallback.

The F&O universe is derived from the Groww instrument master: an NSE F&O underlying that also has an NSE cash series EQ listing. Index underlyings such as NIFTY are excluded. A non-empty derived list is accepted. It does not need a separate approval button.

## Selection

A selected stock must be in the F&O universe, must move in the locked direction, and must have an absolute daily move of 5% or less. Exactly 5% stays eligible. A stock found by more than one method appears once, with every method kept.

Method 1 is implemented. Bullish takes the top five F&O price gainers. Bearish takes the top five F&O price losers. Open interest is shown when Groww supplies it. It is not used to rank stocks. No OI-spurt threshold has been defined.

Method 2 is blocked. Official constituent files exist for all 13 sectors, but ranking needs a verified index move for every sector. The current Groww instrument file has no Nifty Energy index and no Nifty India Defence index. Those prices are not estimated, and the other 11 sectors are not ranked in their place.

Method 3 is blocked. It requires an authorized NSE-wide gainer and loser ranking. Groww's documented SDK does not provide one. Method 1's F&O ranking is not reused as Method 3.

## Candle setups

Only a completed 5-minute candle is used. The timestamp is the start of the bar. Candles from another trading date are not mixed in. The live worker, the manual checker, and the historical replay all call the same setup function.

Setup 1 uses the second and third completed candles.

- LONG: the candle is red, and both its open and its close are inside the previous candle's high-low range.
- SHORT: the candle is green, and both its open and its close are inside that range.
- The current candle's own high and low do not have to stay inside the previous range.
- A value exactly equal to the previous high or previous low is inside.

Setup 2 uses the first two completed candles.

- LONG: the first candle's high is strictly above the previous trading day's high.
- SHORT: the first candle's low is strictly below the previous trading day's low.
- The absolute move from the first candle's open to the second candle's close is at most 2.5%.
- The second candle's own absolute open-to-close move is at most 1%.
- Both percentage caps are inclusive.
- If the previous-day high or low is missing, Setup 2 is blocked and is not turned into a signal.

Setup 3 starts at the fourth completed candle and includes the candle that starts at 10:55. A candle that starts at 11:00 or later is excluded.

- LONG: the candle is red. SHORT: the candle is green.
- Its volume must be strictly lower than every earlier completed candle of that session.
- Equal volume fails. Being lower than only the immediately previous candle fails when an earlier candle is lower still.
- A missing volume, or a missing earlier 5-minute candle, blocks Setup 3. It is not treated as a pass.

A selected stock with no passing setup stays WAIT. BUY or SHORT requires a passing setup. The stock table shows each comparison as PASS, FAIL, or BLOCKED. A tick is not shown for a value that was not checked.

## After 9:30

The dashboard shows the locked direction, the live breadth, and signal eligibility separately.

The current program still scans the locked list after the live breadth changes. Whether a reversal should stop new signals is marked NEEDS CLIENT CONFIRMATION. Reversal protection is not on.

## Groww and Firebase

Save the Groww API key and secret, or a TOTP token, in Settings. Save the Firebase service-account file there as well. Firestore is the only database. The worker connects to it on a trading day. The dashboard reads the local status file.

Use Test Groww permissions in Settings. Authentication, instrument access, live prices, historical candles, and open interest are reported separately. HTTP 403 is a permission failure. A successful login does not prove that live prices, candles, or open interest are allowed. This repository does not treat a saved test as proof of today's permission.

## Tests

```text
python -m pytest -q
```

The automated tests use mock candles and mock rankings. They do not prove live market behaviour and they do not calculate profit.

## Current limits

- Method 2 cannot rank sectors until Nifty Energy and Nifty India Defence have verified index prices.
- Method 3 cannot run until an authorized NSE-wide ranking source is configured.
- An OI-spurt rule is not defined.
- The post-9:30 reversal rule is not confirmed.
- No order is sent, and no profit figure is calculated.

`deploy/nse-fno-worker.service` and `deploy/nse-fno-dashboard.service` are optional Linux service templates. They are not required for `python run.py`.
