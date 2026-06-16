# write README.md
@'
# NSE250Auto (nsebhavcopyautomation)

Daily automation to download NSE bhavcopy, analyze NIFTY-250, and write breakouts to Google Sheets.

Components:
 - main.py : Python script that runs in GitHub Actions
 - requirements.txt
 - .github/workflows/daily-nse.yml
 - Code.gs : Google Apps Script variant (optional)

Deployment (Python + GitHub Actions)
1. Merge the branch when ready.
2. Create a Google Cloud project and service account with Sheets API enabled:
   - Create service account, generate JSON key.
   - Share your Google Sheet with the service account email (Editor).
   - Copy the entire JSON content and save as a GitHub secret named `GSPREAD_SERVICE_ACCOUNT_JSON`.
   - Save the Sheet ID as a GitHub secret `SHEET_ID`.
3. Optional secrets / env:
   - LOCAL_NIFTY250_CSV : path to a CSV in repo with NIFTY-250 symbols if NSE API is blocked.
   - EXCLUDE_KEYWORDS : comma-separated string of keywords to exclude (default: ETF,BEES,GOLD,SILVER,FUND,REIT,INVIT,LQ)
   - BATCH_SIZE and BATCH_DELAY to tune fetch concurrency
4. Workflow schedule: configured for 20:35 IST (cron: 15:05 UTC). Modify `.github/workflows/daily-nse.yml` if you want a different time.
5. Run manually via Actions -> workflow_dispatch to test immediately.

Notes / Limitations
 - NSE archive endpoints may block or rate-limit scrapers. The script uses polite headers/retries but if you see frequent failures consider adding LOCAL_NIFTY250_CSV fallback or using a mirror.
 - yfinance is used for historical prices and fundamentals; some tickers may miss metadata.
 - Apps Script variant is provided but may hit execution time or URL quotas for 250 tickers.
'@ | Out-File -FilePath README.md -Encoding utf8