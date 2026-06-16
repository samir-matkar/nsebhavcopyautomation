#!/usr/bin/env python3
"""
NSE250Auto - daily bhavcopy downloader and breakout finder.

Flow:
 - Download today's NSE bhavcopy (csv inside zip) from NSE archives.
 - Get NIFTY-250 constituents (try NSE API; fallback to local 'nifty250.csv').
 - Filter out excluded symbols (ETF/BEES/GOLD/SILVER/FUND/LQ/REIT/INVIT etc).
 - Use bhavcopy volume to identify universe (we process all NIFTY-250 by default).
 - In configurable batches (default 40) with delay (default 5s) fetch 1y history via yfinance
   for each symbol, compute SMA9, SMA20, EMA9, EMA20, EMA50, 52wk high, pct away,
   price returns (1m,3m,6m,1y), fundamental growth where available.
 - Compute order-book execution proxy (today volume / avg20 volume and close > open uptick proxy).
 - Apply breakout criteria:
     - pct_away_from_52w >= PCT_AWAY_FROM_52W (12)
     - price > SMA9 AND price > SMA20 AND price > EMA9 AND price > EMA20 AND price > EMA50
 - Write 'universe' and 'breakouts' worksheets to target Google Sheet via service account.
"""

import os
import io
import json
import time
import math
import logging
import zipfile
from datetime import datetime, timedelta
from typing import List, Dict, Optional

import pandas as pd
import numpy as np
import requests
import yfinance as yf
from google.oauth2.service_account import Credentials
import gspread

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("nse250auto")

# ----------------- CONFIG -----------------
TOP_N = int(os.getenv("TOP_N", "250"))  # we will process NIFTY-250
PCT_AWAY_FROM_52W = float(os.getenv("PCT_AWAY_FROM_52W", "12.0"))  # percent
MA_FAST = int(os.getenv("MA_FAST", "9"))   # SMA9
MA_SLOW = int(os.getenv("MA_SLOW", "20"))  # SMA20
EMA1 = int(os.getenv("EMA1", "9"))
EMA2 = int(os.getenv("EMA2", "20"))
EMA3 = int(os.getenv("EMA3", "50"))
MIN_HISTORY_DAYS = int(os.getenv("MIN_HISTORY_DAYS", "220"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "40"))
BATCH_DELAY = float(os.getenv("BATCH_DELAY", "5.0"))  # seconds between batches
GOOGLE_SHEET_ID = os.getenv("SHEET_ID")
SERVICE_ACCOUNT_JSON = os.getenv("GSPREAD_SERVICE_ACCOUNT_JSON")
EXCLUDE_KEYWORDS = [k.upper() for k in os.getenv("EXCLUDE_KEYWORDS", "ETF,BEES,GOLD,SILVER,FUND,REIT,INVIT,LQ").split(",")]

# NSE archive base (we'll build URL for today's date)
NSE_ARCHIVE_BASE = "https://archives.nseindia.com/content/historical/EQUITIES"

# Optional env: provide a local nifty250 CSV path in repo to avoid API issues
LOCAL_NIFTY250_CSV = os.getenv("LOCAL_NIFTY250_CSV", "nifty250.csv")

# User-agent and headers for NSE download (polite)
DEFAULT_HEADERS = {
    "User-Agent": os.getenv("NSE_USER_AGENT", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Connection": "keep-alive",
}

# ----------------- HELPERS -----------------
def nse_bhavcopy_url_for_date(dt: datetime) -> str:
    """Construct expected bhavcopy zip URL for an equities date on NSE archive."""
    # format: https://archives.nseindia.com/content/historical/EQUITIES/<YYYY>/<MON>/<cmDDMONYYYYbhav.csv.zip>
    yyyy = dt.strftime("%Y")
    mon = dt.strftime("%b").upper()  # e.g., MAY
    daypart = dt.strftime("%d%b%Y").upper()  # e.g., 12MAY2026
    filename = f"cm{daypart}bhav.csv.zip"
    return f"{NSE_ARCHIVE_BASE}/{yyyy}/{mon}/{filename}"

def download_bhavcopy(dt: datetime, max_retries: int = 3, timeout: int = 30) -> Optional[pd.DataFrame]:
    """Download bhavcopy zip for date dt and return DataFrame of CSV inside. Return None if not found."""
    url = nse_bhavcopy_url_for_date(dt)
    logger.info("Attempting to download bhavcopy: %s", url)
    for attempt in range(1, max_retries+1):
        try:
            with requests.Session() as s:
                s.headers.update(DEFAULT_HEADERS)
                r = s.get(url, timeout=timeout)
                if r.status_code == 200:
                    # file is a zip with a CSV inside
                    z = zipfile.ZipFile(io.BytesIO(r.content))
                    # pick first csv file
                    csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
                    if not csv_names:
                        logger.error("No CSV found inside zip for %s", url)
                        return None
                    csv_bytes = z.read(csv_names[0])
                    df = pd.read_csv(io.BytesIO(csv_bytes), dtype=str)
                    logger.info("Downloaded bhavcopy with %d rows", len(df))
                    return df
                else:
                    logger.warning("Got status %s for %s (attempt %d)", r.status_code, url, attempt)
        except Exception as e:
            logger.warning("Failed download attempt %d: %s", attempt, e)
        time.sleep(1 + attempt)
    logger.error("Failed to download bhavcopy after %d attempts: %s", max_retries, url)
    return None

def find_latest_bhavcopy(max_back_days: int = 5) -> Optional[pd.DataFrame]:
    """Try today and go backwards up to max_back_days to find a bhavcopy (skips weekends/holidays)."""
    today = datetime.now()
    for d in range(0, max_back_days+1):
        dt = today - timedelta(days=d)
        df = download_bhavcopy(dt)
        if df is not None and not df.empty:
            df['TIMESTAMP'] = dt.strftime("%d-%b-%Y")
            return df
    return None

def get_nifty250_list() -> List[str]:
    """
    Try to fetch NIFTY-250 constituents via NSE API. If that fails, fall back to local CSV file 'nifty250.csv'.
    Returns list of symbols (uppercase, no .NS suffix).
    """
    try:
        api = "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20250"
        logger.info("Attempting to fetch NIFTY-250 via NSE API")
        with requests.Session() as s:
            s.headers.update(DEFAULT_HEADERS)
            r = s.get(api, timeout=20)
            if r.status_code == 200:
                data = r.json()
                # data['data'] list has 'symbol'
                syms = [row['symbol'].upper() for row in data.get('data', []) if 'symbol' in row]
                if syms:
                    logger.info("Fetched %d NIFTY-250 symbols from NSE API", len(syms))
                    return syms
    except Exception as e:
        logger.warning("NSE API fetch failed: %s", e)

    # Fallback: local CSV: expect a column 'symbol'
    if os.path.exists(LOCAL_NIFTY250_CSV):
        try:
            df = pd.read_csv(LOCAL_NIFTY250_CSV, dtype=str)
            col = next((c for c in df.columns if c.lower() in ("symbol","ticker","code")), df.columns[0])
            syms = df[col].dropna().astype(str).str.upper().str.strip().tolist()
            logger.info("Loaded %d symbols from local %s", len(syms), LOCAL_NIFTY250_CSV)
            return syms
        except Exception as e:
            logger.error("Failed to load local nifty250.csv: %s", e)

    raise RuntimeError("Unable to obtain NIFTY-250 list. Provide a local nifty250.csv in the repo or ensure NSE API reachable.")

def normalize_bhavcopy(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize bhavcopy DataFrame to lower-case columns and expected names:
    Expected fields used: SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE, TOTTRDQTY, TOTTRDVAL, TIMESTAMP
    """
    cols = {c: c.strip() for c in df.columns.tolist()}
    df = df.rename(columns=cols)
    # Uppercase symbol
    if 'SYMBOL' in df.columns:
        df['SYMBOL'] = df['SYMBOL'].astype(str).str.upper()
    # convert numerics
    for col in ['OPEN','HIGH','LOW','CLOSE','LAST','PREVCLOSE','TOTTRDQTY','TOTTRDVAL']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].str.replace(',',''), errors='coerce')
    return df

def filter_universe_by_nifty250(bhav_df: pd.DataFrame, nifty250: List[str]) -> pd.DataFrame:
    df = bhav_df[bhav_df['SYMBOL'].isin(nifty250)].copy()
    logger.info("Filtered bhavcopy to NIFTY-250: %d rows", len(df))
    return df

def exclude_by_name(df: pd.DataFrame) -> pd.DataFrame:
    """Remove rows where symbol or COMPANYNAME contains any exclusion keyword."""
    def has_excl(s: str) -> bool:
        if not s: return False
        s2 = str(s).upper()
        return any(k in s2 for k in EXCLUDE_KEYWORDS)
    # bhavcopy may not have COMPANYNAME; use SYMBOL only if needed
    mask = ~df['SYMBOL'].astype(str).apply(has_excl)
    return df[mask].copy()

# ---------- Indicators / processing ----------
def calc_sma(series: pd.Series, window: int) -> float:
    if len(series) < window:
        return float('nan')
    return float(series.rolling(window=window).mean().iloc[-1])

def calc_ema(series: pd.Series, span: int) -> float:
    if len(series) < span:
        return float('nan')
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])

def compute_indicators_for_symbol(yf_symbol: str, bhav_today: pd.Series) -> Optional[Dict]:
    """
    Using yfinance for historical prices (1y), compute indicators and fundamentals.
    yf_symbol is like 'RELIANCE.NS'
    bhav_today is the row from bhavcopy with today's TOTTRDQTY and CLOSE
    """
    try:
        tk = yf.Ticker(yf_symbol)
        hist = tk.history(period="1y", interval="1d", actions=False)
        if hist is None or hist.empty or len(hist) < MIN_HISTORY_DAYS//2:
            logger.debug("Insufficient history for %s", yf_symbol)
            return None
        close = hist['Close'].dropna()
        if close.empty:
            return None
        current = float(bhav_today.get('CLOSE') if not pd.isna(bhav_today.get('CLOSE')) else close.iloc[-1])
        high_52w = float(close.max())
        pct_away = (high_52w - current) / high_52w * 100 if high_52w > 0 else float('nan')
        sma9 = calc_sma(close, MA_FAST)
        sma20 = calc_sma(close, MA_SLOW)
        ema9 = calc_ema(close, EMA1)
        ema20 = calc_ema(close, EMA2)
        ema50 = calc_ema(close, EMA3)
        # returns
        def pct_return(days):
            if len(close) > days:
                return (current / close.shift(days).iloc[-1] - 1) * 100
            return float('nan')
        ret_1m = pct_return(22)
        ret_3m = pct_return(63)
        ret_6m = pct_return(126)
        ret_1y = pct_return(252)
        # avg vol 20
        vol = hist['Volume'].dropna()
        avg_vol_20 = float(vol.rolling(window=20).mean().iloc[-1]) if len(vol) >= 20 else float('nan')
        today_vol = float(bhav_today.get('TOTTRDQTY') if not pd.isna(bhav_today.get('TOTTRDQTY')) else (vol.iloc[-1] if not vol.empty else float('nan')))
        vol_ratio = (today_vol / avg_vol_20) if (avg_vol_20 and not math.isnan(avg_vol_20)) else float('nan')
        # uptick proxy: close > open and close > prevclose
        uptick = 1 if (float(bhav_today.get('CLOSE') or 0) > float(bhav_today.get('OPEN') or 0)) else 0
        uptick2 = 1 if (float(bhav_today.get('CLOSE') or 0) > float(bhav_today.get('PREVCLOSE') or 0)) else 0
        uptick_score = uptick + uptick2
        # fundamentals (best-effort)
        info = {}
        try:
            info = tk.info or {}
        except Exception:
            info = {}
        # common growth fields (availability varies)
        revenue_growth = info.get('revenueGrowth') or info.get('revenueGrowth3Y') or None
        earnings_growth = info.get('earningsQuarterlyGrowth') or info.get('earningsGrowth') or None
        # additional fields
        industry = info.get('industry') or info.get('sector') or ""
        longName = info.get('longName') or info.get('shortName') or ""
        row = {
            'yf_symbol': yf_symbol,
            'symbol': yf_symbol.replace('.NS',''),
            'current': current,
            '52w_high': round(high_52w,2),
            'pct_away_from_52w': round(float(pct_away),2),
            'sma9': round(sma9,4) if not math.isnan(sma9) else float('nan'),
            'sma20': round(sma20,4) if not math.isnan(sma20) else float('nan'),
            'ema9': round(ema9,4) if not math.isnan(ema9) else float('nan'),
            'ema20': round(ema20,4) if not math.isnan(ema20) else float('nan'),
            'ema50': round(ema50,4) if not math.isnan(ema50) else float('nan'),
            'ret_1m_pct': round(ret_1m,2) if not math.isnan(ret_1m) else float('nan'),
            'ret_3m_pct': round(ret_3m,2) if not math.isnan(ret_3m) else float('nan'),
            'ret_6m_pct': round(ret_6m,2) if not math.isnan(ret_6m) else float('nan'),
            'ret_1y_pct': round(ret_1y,2) if not math.isnan(ret_1y) else float('nan'),
            'avg_vol_20': round(avg_vol_20,0) if not math.isnan(avg_vol_20) else float('nan'),
            'today_vol': round(today_vol,0) if not math.isnan(today_vol) else float('nan'),
            'vol_ratio': round(vol_ratio,3) if not math.isnan(vol_ratio) else float('nan'),
            'uptick_score': uptick_score,
            'industry': industry,
            'longName': longName,
            'revenue_growth': revenue_growth,
            'earnings_growth': earnings_growth
        }
        return row
    except Exception as e:
        logger.debug("Error computing for %s: %s", yf_symbol, e)
        return None

def pick_breakouts(df: pd.DataFrame) -> pd.DataFrame:
    """Apply breakout criteria described by user."""
    # Exclude if longName contains excluded keywords
    df['excluded'] = df['longName'].fillna("").str.upper().apply(lambda x: any(k in x for k in EXCLUDE_KEYWORDS))
    df = df[~df['excluded']].copy()
    # uptrend rule: price > sma9 & sma20 & ema9 & ema20 & ema50
    cond_uptrend = (
        (df['current'] > df['sma9']) &
        (df['current'] > df['sma20']) &
        (df['current'] > df['ema9']) &
        (df['current'] > df['ema20']) &
        (df['current'] > df['ema50'])
    )
    cond_pct = df['pct_away_from_52w'] >= PCT_AWAY_FROM_52W
    finalists = df[cond_uptrend & cond_pct].copy()
    # sort by vol_ratio and pct_away priority
    finalists = finalists.sort_values(['vol_ratio','pct_away_from_52w'], ascending=[False,False])
    return finalists

def write_to_sheet(universe_df: pd.DataFrame, breakouts_df: pd.DataFrame):
    if not GOOGLE_SHEET_ID:
        raise RuntimeError("SHEET_ID env var not set")
    if not SERVICE_ACCOUNT_JSON:
        raise RuntimeError("GSPREAD_SERVICE_ACCOUNT_JSON env var not set")
    cred = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(cred, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    # Universe worksheet (replace if exists)
    try:
        ws = sh.worksheet("universe")
        sh.del_worksheet(ws)
    except Exception:
        pass
    ws = sh.add_worksheet(title="universe", rows=str(len(universe_df)+10), cols="40")
    ws.update([universe_df.columns.tolist()] + universe_df.fillna("").astype(str).values.tolist())
    # Breakouts worksheet
    try:
        wb = sh.worksheet("breakouts")
        sh.del_worksheet(wb)
    except Exception:
        pass
    wb = sh.add_worksheet(title="breakouts", rows=str(len(breakouts_df)+10), cols="40")
    wb.update([breakouts_df.columns.tolist()] + breakouts_df.fillna("").astype(str).values.tolist())
    logger.info("Wrote %d universe rows and %d breakouts", len(universe_df), len(breakouts_df))

def main():
    logger.info("Starting NSE250Auto job")
    return;
    # 1) bhavcopy
    bhav = find_latest_bhavcopy(max_back_days=7)
    if bhav is None:
        logger.error("No bhavcopy found for last 7 days. Exiting.")
        return
    bhav = normalize_bhavcopy(bhav)
    # 2) get nifty250 list
    nifty250 = get_nifty250_list()
    # 3) filter bhavcopy to NIFTY-250
    bhav_n250 = filter_universe_by_nifty250(bhav, nifty250)
    bhav_n250 = exclude_by_name(bhav_n250)
    # 4) prepare a mapping symbol -> row
    bhav_map = {row['SYMBOL']: row for _, row in bhav_n250.iterrows()}
    symbols = list(bhav_map.keys())[:TOP_N]
    logger.info("Processing %d symbols from NIFTY-250 (TOP_N=%d)", len(symbols), TOP_N)
    # 5) batch processing using yfinance for history
    results = []
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i:i+BATCH_SIZE]
        logger.info("Processing batch %d/%d (%d symbols)", i//BATCH_SIZE+1, math.ceil(len(symbols)/BATCH_SIZE), len(batch))
        for sym in batch:
            try:
                yf_sym = sym + ".NS"
                row = compute_indicators_for_symbol(yf_sym, pd.Series(bhav_map[sym]))
                if row:
                    results.append(row)
                else:
                    logger.debug("No row returned for %s", sym)
            except Exception as e:
                logger.warning("Failed symbol %s: %s", sym, e)
        # delay after batch
        logger.info("Batch complete, sleeping %.1fs", BATCH_DELAY)
        time.sleep(BATCH_DELAY)
    if not results:
        logger.error("No indicator results computed. Exiting.")
        return
    df_res = pd.DataFrame(results)
    # 6) pick breakouts
    breakouts = pick_breakouts(df_res)
    logger.info("Found %d breakout candidates", len(breakouts))
    # 7) write to Google Sheet
    try:
        write_to_sheet(df_res.sort_values('vol_ratio', ascending=False), breakouts)
    except Exception as e:
        logger.error("Failed to write to Google Sheet: %s", e)
    logger.info("Job finished.")

if __name__ == "__main__":
    main()