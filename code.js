/**
 * Google Apps Script variant (simpler, subject to quotas).
 *
 * This script:
 *  - Downloads today's NSE bhavcopy CSV (if accessible)
 *  - Appends today's (symbol, date, close, volume) rows into a Sheet 'history'
 *  - Computes SMA9, SMA20, EMA9, EMA20, EMA50 per symbol using sheet formulas or script
 *  - Writes results and breakouts to 'universe' and 'breakouts' sheets
 *
 * Limitations:
 *  - UrlFetch quotas and runtime (6 minutes) may make full 250-symbol 1-year operations fail.
 *  - Recommended: run daily and keep history sheet small or run for a subset (e.g., top 100).
 *
 * Deployment:
 *  - Paste this into Apps Script editor bound to your Google Sheet.
 *  - Create time-driven trigger daily (20:40 IST).
 */

const NSE_ARCHIVE_BASE = "https://archives.nseindia.com/content/historical/EQUITIES";

function nseBhavcopyUrlForDate(date) {
  var yyyy = Utilities.formatDate(date, "GMT", "yyyy");
  var mon = Utilities.formatDate(date, "GMT", "MMM").toUpperCase();
  var daypart = Utilities.formatDate(date, "GMT", "ddMMMYYYY").toUpperCase();
  var filename = "cm" + daypart + "bhav.csv.zip";
  return NSE_ARCHIVE_BASE + "/" + yyyy + "/" + mon + "/" + filename;
}

function fetchBhavcopyCsv(date) {
  var url = nseBhavcopyUrlForDate(date);
  var options = {
    headers: {
      "User-Agent": "Mozilla/5.0 (compatible; GoogleAppsScript)",
      "Accept": "*/*"
    },
    muteHttpExceptions: true
  };
  var resp = UrlFetchApp.fetch(url, options);
  if (resp.getResponseCode() !== 200) {
    Logger.log("Failed to fetch bhavcopy: " + resp.getResponseCode());
    return null;
  }
  // Bytes are a zip; Apps Script cannot unzip natively. However the modern bhavcopy from archive is a zip;
  // We can use Utilities.unzip on the blob.
  var blob = resp.getBlob();
  try {
    var files = Utilities.unzip(blob);
    if (files.length === 0) return null;
    // pick first CSV and parse
    var csvContent = files[0].getDataAsString();
    return Utilities.parseCsv(csvContent);
  } catch (e) {
    Logger.log("Unzip failed: " + e);
    return null;
  }
}

function appendDailyToHistory() {
  var ss = SpreadsheetApp.getActive();
  var historySheet = ss.getSheetByName("history");
  if (!historySheet) {
    historySheet = ss.insertSheet("history");
    historySheet.appendRow(["symbol","date","open","high","low","close","prevclose","volume"]);
  }
  // find latest date in history
  var lastRow = historySheet.getLastRow();
  var lastDate = null;
  if (lastRow > 1) {
    lastDate = historySheet.getRange(lastRow,2).getValue();
  }
  var today = new Date();
  var csv = fetchBhavcopyCsv(today);
  if (!csv) {
    Logger.log("No bhavcopy for today.");
    return;
  }
  // assume header row present
  var header = csv[0];
  var rows = csv.slice(1);
  var colIndex = {};
  header.forEach(function(h,i){ colIndex[h.trim().toUpperCase()] = i; });
  var out = [];
  for (var i=0;i<rows.length;i++){
    var r = rows[i];
    var sym = r[colIndex['SYMBOL']];
    var close = r[colIndex['CLOSE']];
    var open = r[colIndex['OPEN']];
    var prev = r[colIndex['PREVCLOSE']];
    var vol = r[colIndex['TOTTRDQTY']];
    var dateStr = Utilities.formatDate(today, Session.getScriptTimeZone(), "yyyy-MM-dd");
    out.push([sym, dateStr, open, "", "", close, prev, vol]);
  }
  if (out.length>0) {
    historySheet.getRange(historySheet.getLastRow()+1,1,out.length,out[0].length).setValues(out);
    Logger.log("Appended %d rows to history", out.length);
  }
}

function computeIndicatorsFromHistory() {
  // This is intentionally minimal. For robust indicators use spreadsheet formulas per-symbol or export and compute externally.
  var ss = SpreadsheetApp.getActive();
  var history = ss.getSheetByName("history");
  if (!history) {
    Logger.log("No history sheet");
    return;
  }
  var data = history.getDataRange().getValues();
  var header = data[0];
  var rows = data.slice(1);
  // Build per-symbol arrays
  var map = {};
  rows.forEach(function(r){
    var sym = r[0];
    var date = r[1];
    var close = parseFloat(r[5]);
    var vol = parseFloat(r[7]);
    if (!(sym in map)) map[sym] = [];
    map[sym].push({date: date, close: close, vol: vol});
  });
  var universe = [];
  for (var sym in map) {
    var arr = map[sym].map(function(x){return x.close;}).filter(function(v){return !isNaN(v);});
    if (arr.length < 10) continue;
    // sma9 sma20 emas using simple formulas
    var sma9 = average(arr.slice(-9));
    var sma20 = arr.length>=20 ? average(arr.slice(-20)) : null;
    var ema9 = ema(arr,9);
    var ema20 = ema(arr,20);
    var ema50 = ema(arr,50);
    var current = arr[arr.length-1];
    var high52 = Math.max.apply(null, arr.slice(-252));
    var pct_away = (high52 - current)/high52*100;
    universe.push([sym, current, high52, pct_away, sma9, sma20, ema9, ema20, ema50]);
  }
  // write to 'universe' sheet
  var s = ss.getSheetByName("universe");
  if (s) ss.deleteSheet(s);
  s = ss.insertSheet("universe");
  s.appendRow(["symbol","current","52w_high","pct_away","sma9","sma20","ema9","ema20","ema50"]);
  if (universe.length>0) s.getRange(2,1,universe.length,universe[0].length).setValues(universe);
  // simple breakout filter
  var breakouts = universe.filter(function(r){ return r[3] >= 12 && r[1] > r[4] && r[1] > r[5] && r[1] > r[6] && r[1] > r[7] && r[1] > r[8]; });
  var b = ss.getSheetByName("breakouts");
  if (b) ss.deleteSheet(b);
  b = ss.insertSheet("breakouts");
  b.appendRow(["symbol","current","52w_high","pct_away","sma9","sma20","ema9","ema20","ema50"]);
  if (breakouts.length>0) b.getRange(2,1,breakouts.length,breakouts[0].length).setValues(breakouts);
}

function average(arr) {
  if (!arr || arr.length===0) return null;
  var s = 0;
  for (var i=0;i<arr.length;i++) s+=arr[i];
  return s/arr.length;
}

function ema(values, span) {
  if (!values || values.length < 1) return null;
  var k = 2/(span+1);
  var emaPrev = values[0];
  for (var i=1;i<values.length;i++){
    emaPrev = values[i]*k + emaPrev*(1-k);
  }
  return emaPrev;
}

function mainAppsScript() {
  appendDailyToHistory();
  computeIndicatorsFromHistory();
}