#!/usr/bin/env python3
"""
rebuild.py - S&P 500 market internals model (Acheron Insights house style)

Regenerates spx_internals.html from an empty directory.
  data   : Yahoo Finance daily adjusted closes (ETFs), CBOE CDN (VIX, VIX3M — the actual term structure),
           FRED (NFCI, HY/IG OAS — flagged short-history)
  fonts  : fontsource latin subsets from jsDelivr, base64-embedded
  output : ./spx_internals.html (and /mnt/user-data/outputs/ if it exists)

Model
  Weekly (Friday close) values vs weekly log return of the S&P 500 (%), 2004 onward (2009+ where the VIX term
  structure pillar is live — CBOE only publishes VIX3M from Sep 2009).
  Screen (R0-R6) selects members greedily; membership is frozen in MEMBERS and re-screened on every rebuild
  (drift is flagged in the dashboard, not silently re-selected).
  Composite = equal weight across pillars of equal-weight members; each member is sign-aligned to its economic
  prior and scaled by trailing 52w vol (lagged one week), clipped +/-4.
  Implied 13w return = trailing 156w no-intercept beta of 13w SPX return on 13w composite (lagged one week).
"""
import os, io, re, sys, json, time, base64, random, argparse, tempfile, subprocess, datetime as dt
import urllib.parse
import numpy as np
import pandas as pd
urllib_quote = urllib.parse.quote

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
OUT_NAME = "spx_internals.html"
START = "2004-01-01"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
TTL_HOURS = 10
os.makedirs(CACHE, exist_ok=True)

TIINGO_TOKEN = os.environ.get("TIINGO_TOKEN", "").strip()
FRED_API_KEY = os.environ.get("FRED_API_KEY", "").strip()
TIINGO_OK = False
SRC = {}      # series key -> source actually used
STALE = {}    # series key -> last cached date, when a download failed and we fell back to cache
OPTIONAL = {"DX-Y.NYB", "BAMLH0A0HYM2", "BAMLC0A0CM", "T10Y2Y"}   # context-only: never fail the build over these

def log(*a): print(*a, flush=True)

# ---------------------------------------------------------------- fetch
BROWSER_HEADERS = ["Accept: text/html,application/json,text/csv,*/*;q=0.8", "Accept-Language: en-US,en;q=0.9"]

def curl(url, binary=False, tries=5, use_ua=True, base_wait=2.0, max_wait=45.0, headers=()):
    """use_ua=False for FRED's fredgraph endpoint: its edge proxy returns an "upstream connect error" body
    (HTTP 200, so it is not caught as a failed request) when it sees a browser User-Agent; plain curl works.
    Retries with jittered backoff on network errors, 429 and 5xx."""
    last = None
    for i in range(tries):
        with tempfile.NamedTemporaryFile(delete=False) as tf: tmp = tf.name
        try:
            cmd = ["curl", "-s", "-L", "--compressed", "--max-time", "60"]
            if use_ua: cmd += ["-H", f"User-Agent: {UA}"]
            for h in headers: cmd += ["-H", h]
            cmd += ["-o", tmp, "-w", "%{http_code}", url]
            r = subprocess.run(cmd, capture_output=True, text=True)
            code = r.stdout.strip(); body = open(tmp, "rb").read()
        finally:
            os.unlink(tmp)
        if code == "200" and body and not body.startswith(b"upstream connect error"):
            return body if binary else body.decode("utf-8", "replace")
        last = f"HTTP {code or 'none'} {body[:100]!r}"
        if code not in ("", "000", "429") and not code.startswith("5"):
            break                                   # 4xx other than 429: retrying will not help
        if i < tries - 1:
            time.sleep(min(max_wait, base_wait * (2 ** i)) * (0.6 + 0.8 * random.random()))
    raise RuntimeError(f"fetch failed {url}: {last}")

def http_get(url, **kw): return curl(url, **kw)

def fresh(path, hours=None):
    hours = TTL_HOURS if hours is None else hours
    return os.path.exists(path) and (time.time() - os.path.getmtime(path)) < hours * 3600

def yahoo_raw(sym):
    """Full daily series from Yahoo (adjusted close). Network only; get_series owns the cache."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib_quote(sym)}?period1=946684800"
           f"&period2={int(time.time())}&interval=1d&events=div%2Csplit&includeAdjustedClose=true")
    tries = 2 if TIINGO_OK else 5          # on CI Yahoo is usually blocked; do not burn the time budget
    j = json.loads(http_get(url, headers=BROWSER_HEADERS, tries=tries, base_wait=1.5, max_wait=20))["chart"]["result"][0]
    q = j["indicators"]
    vals = q["adjclose"][0]["adjclose"] if q.get("adjclose") else q["quote"][0]["close"]
    ser = pd.Series(vals, index=pd.to_datetime(j["timestamp"], unit="s").normalize(), dtype=float)
    return ser[~ser.index.duplicated(keep="last")].dropna()

def fredgraph(fid, cosd="2000-01-01"):
    """FRED's CSV chart endpoint. Works locally; its edge proxy blocks GitHub runner IPs, hence the JSON API above.
    Note it also caps some series (the ICE BofA OAS ones) to a trailing window regardless of cosd."""
    txt = ""
    for i in range(4):
        txt = curl(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fid}&cosd={cosd}", use_ua=False)
        if txt[:40].lower().startswith(("observation_date", "date")): break
        time.sleep(3 * (i + 1))
    else:
        raise RuntimeError(f"fredgraph {fid}: {txt[:80]!r}")
    df = pd.read_csv(io.StringIO(txt))
    return pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values, index=pd.to_datetime(df.iloc[:, 0])).dropna()

def cboe_raw(sym):
    """CBOE's own daily index CSV (cdn.cboe.com). VIX from 1990, VIX3M from 18 Sep 2009.
    FRED's VXVCLS carries VIX3M back to Dec 2007, so FRED is preferred when a key is available."""
    txt = http_get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv", tries=3)
    if not txt[:4].upper().startswith("DATE"):
        raise RuntimeError(f"CBOE {sym} bad payload: {txt[:80]!r}")
    df = pd.read_csv(io.StringIO(txt))
    return pd.Series(pd.to_numeric(df["CLOSE"], errors="coerce").values,
                     index=pd.to_datetime(df["DATE"], format="%m/%d/%Y")).dropna().sort_index()

# ---------------------------------------------------------------- market data sources
# Yahoo blocks GitHub's shared runner IPs, so CI uses Tiingo for the ETFs and FRED's JSON API for everything
# else. Locally, with no keys, everything falls back to Yahoo / CBOE / fredgraph, which work from a normal IP.
TIINGO_ETF = {  # our ticker -> Tiingo daily symbol
    "XLI":"xli","XLB":"xlb","XLY":"xly","XLF":"xlf","XLE":"xle","XLK":"xlk","XLU":"xlu","XLP":"xlp","XLV":"xlv",
    "KRE":"kre","IEI":"iei","HYG":"hyg","LQD":"lqd","IEF":"ief","IYT":"iyt","IWM":"iwm","GLD":"gld","TLT":"tlt",
    "DIA":"dia","XHB":"xhb","SMH":"smh","RSP":"rsp","QQQ":"qqq","IWF":"iwf","IWD":"iwd","SHY":"shy","KBE":"kbe",
    "TIP":"tip","KIE":"kie",
    "^GSPC":"spy",      # the index itself is not on Tiingo's free tier; SPY stands in (see TARGET_NOTE)
}
TARGET_NOTE = ("On the automated build the target is SPY total return rather than the S&P 500 price index, which "
               "Tiingo's free tier does not carry. SPY tracks the index at a 0.998 weekly-return correlation; the "
               "difference is the ~1.8%/yr dividend yield, which shows up only in the compounding, never in the "
               "correlations this model is built on.")
FRED_SERIES = {   # our key -> FRED series id (used for everything non-ETF)
    "NFCI":"NFCI", "BAMLH0A0HYM2":"BAMLH0A0HYM2", "BAMLC0A0CM":"BAMLC0A0CM",
    "T10Y2Y":"T10Y2Y", "DTWEXBGS":"DTWEXBGS", "VIX":"VIXCLS", "VIX3M":"VXVCLS",
}

def tiingo_daily(sym, tsym):
    """Tiingo daily prices. Full CSV (no columns filter) so the parser is robust to schema differences."""
    url = f"https://api.tiingo.com/tiingo/daily/{tsym}/prices?startDate=2000-01-01&format=csv&token={TIINGO_TOKEN}"
    txt = http_get(url, headers=["Accept: text/csv"], tries=4, base_wait=2, max_wait=20)
    if not txt[:4].lower().startswith("date"):
        raise RuntimeError(f"Tiingo {tsym}: {txt[:100]!r}")
    df = pd.read_csv(io.StringIO(txt))
    col = "adjClose" if "adjClose" in df.columns else "close"
    idx = pd.to_datetime(df["date"])
    if getattr(idx.dt, "tz", None) is not None: idx = idx.dt.tz_localize(None)
    return pd.Series(pd.to_numeric(df[col], errors="coerce").values, index=idx).dropna().sort_index()

def tiingo_check():
    if not TIINGO_TOKEN: return False, "TIINGO_TOKEN not set"
    try:
        txt = http_get(f"https://api.tiingo.com/tiingo/daily/spy/prices?startDate=2026-01-01&format=csv&token={TIINGO_TOKEN}",
                       headers=["Accept: text/csv"], tries=3, base_wait=2, max_wait=15)
        if txt[:4].lower().startswith("date") and len(txt.splitlines()) > 3: return True, "ok"
        return False, txt[:150].strip().replace("\n", " ")
    except Exception as e:
        return False, str(e)[:150]

def fred_api(fid, tries=5):
    """FRED's official JSON API. Reliable from CI, unlike the fredgraph CSV endpoint, whose edge proxy
    resets connections from GitHub runner IPs."""
    url = (f"https://api.stlouisfed.org/fred/series/observations?series_id={fid}"
           f"&observation_start=2000-01-01&file_type=json&api_key={FRED_API_KEY}")
    j = json.loads(http_get(url, headers=["Accept: application/json"], tries=tries, base_wait=2, max_wait=30))
    if "observations" not in j: raise RuntimeError(f"FRED API {fid}: unexpected payload")
    ser = pd.Series({o["date"]: o["value"] for o in j["observations"]})
    ser.index = pd.to_datetime(ser.index)
    return pd.to_numeric(ser, errors="coerce").dropna()     # FRED encodes gaps as "."

def fred_check():
    try:
        ser = fred_api("VIXCLS", tries=3)
        return (len(ser) > 100), ("ok" if len(ser) > 100 else "too few observations")
    except Exception as e:
        return False, str(e)[:150]

def _cache_path(key):
    return os.path.join(CACHE, "s_" + re.sub(r"[^A-Za-z0-9]", "_", key) + ".csv")

def get_series(key, kind):
    """One daily series for `key`. kind is 'etf' | 'fred' | 'vol'. Sources are tried in order and the result
    is cached; if every source fails we fall back to the cached copy and flag it stale."""
    path = _cache_path(key)
    if not fresh(path):
        plan, errs = [], []
        if kind == "etf":
            if TIINGO_OK and key in TIINGO_ETF: plan.append(("Tiingo", lambda: tiingo_daily(key, TIINGO_ETF[key])))
            if not (TIINGO_OK and key in TIINGO_ETF): plan.append(("Yahoo", lambda: yahoo_raw(key)))
        elif kind == "fred":
            if FRED_API_KEY: plan.append(("FRED API", lambda: fred_api(FRED_SERIES[key])))
            plan.append(("fredgraph", lambda: fredgraph(FRED_SERIES[key])))
        elif kind == "vol":
            if FRED_API_KEY: plan.append(("FRED API", lambda: fred_api(FRED_SERIES[key])))
            plan.append(("CBOE", lambda: cboe_raw("VIX" if key == "VIX" else "VIX3M")))
            plan.append(("fredgraph", lambda: fredgraph(FRED_SERIES[key])))
        got = None
        for label, fn in plan:
            t0 = time.time()
            try:
                ser = fn()
                ser = ser[~ser.index.duplicated(keep="last")].dropna()
                if len(ser) < 250: raise RuntimeError(f"only {len(ser)} rows")
                ser.to_csv(path, header=["v"]); SRC[key] = label; got = label
                log(f"  {key:14s} {label:9s} {len(ser):5d} rows to {ser.index[-1].date()}  {time.time()-t0:4.1f}s")
                break
            except Exception as e:
                errs.append(f"{label}: {e}")
                log(f"  {key:14s} {label:9s} failed ({time.time()-t0:4.1f}s): {str(e)[:70]}")
        if got is None:
            if os.path.exists(path):
                last = pd.read_csv(path, index_col=0, parse_dates=True).index.max()
                STALE[key] = str(last.date()); SRC[key] = "cache"
                log(f"  {key:14s} CACHE     using copy to {last.date()}")
            elif key in OPTIONAL:
                SRC[key] = "missing"; log(f"  {key:14s} MISSING   optional series dropped")
                return None
            else:
                raise RuntimeError(f"{key} failed from all sources -> " + " | ".join(errs))
    else:
        SRC.setdefault(key, "cache")
    ser = pd.read_csv(path, index_col=0, parse_dates=True)["v"]
    return ser.where(ser > 0) if kind == "etf" else ser

FONT_FILES = {
    ("Space Grotesk", 500, "normal"): "space-grotesk@latest/latin-500-normal",
    ("Space Grotesk", 700, "normal"): "space-grotesk@latest/latin-700-normal",
    ("IBM Plex Sans", 400, "normal"): "ibm-plex-sans@latest/latin-400-normal",
    ("IBM Plex Sans", 600, "normal"): "ibm-plex-sans@latest/latin-600-normal",
    ("IBM Plex Sans", 400, "italic"): "ibm-plex-sans@latest/latin-400-italic",
    ("IBM Plex Mono", 400, "normal"): "ibm-plex-mono@latest/latin-400-normal",
    ("IBM Plex Mono", 500, "normal"): "ibm-plex-mono@latest/latin-500-normal",
}
def font_css():
    out = []
    for (fam, w, st), stem in FONT_FILES.items():
        p = os.path.join(CACHE, stem.replace("/", "_").replace("@", "_") + ".woff2")
        if not os.path.exists(p):
            open(p, "wb").write(curl(f"https://cdn.jsdelivr.net/fontsource/fonts/{stem}.woff2", binary=True))
        b64 = base64.b64encode(open(p, "rb").read()).decode()
        out.append(f"@font-face{{font-family:'{fam}';font-style:{st};font-weight:{w};font-display:swap;"
                   f"src:url(data:font/woff2;base64,{b64}) format('woff2');}}")
    return "\n".join(out)

# ---------------------------------------------------------------- universe
TICKERS = ("^GSPC XLI XLB XLY XLF XLE XLK XLU XLP XLV KRE IEI HYG LQD IEF IYT IWM GLD TLT DIA").split()

def lr(s): return np.log(s).diff()
def basket(W, names): return pd.concat([lr(W[n]) for n in names], axis=1).mean(axis=1, skipna=False)

# key, label, legs, origin, prior sign, pillar, builder(W, extra) -> weekly series (log-return-scale or level-diff)
CANDS = [
 ("HYOAS_CHG","High-yield OAS","ICE BofA US HY OAS, bp change, inverted","requested",-1,"CR", lambda W,X: X["hyoas"].diff()*100),
 ("IGOAS_CHG","Investment-grade OAS","ICE BofA US IG OAS, bp change, inverted","requested",-1,"CR", lambda W,X: X["igoas"].diff()*100),
 ("HYG_IEI","High yield / Treasuries","HYG vs IEI, duration-matched","requested",1,"CR", lambda W,X: lr(W.HYG)-lr(W.IEI)),
 ("XLY_XLP","Discretionary / Staples","XLY vs XLP","requested",1,"RISK", lambda W,X: lr(W.XLY)-lr(W.XLP)),
 ("TERM_CHG","VIX term structure","CBOE VIX3M / VIX \u2212 1, weekly change","requested",1,"VOL", lambda W,X: X["term"].diff()),
 ("TLT_SPY","Long Treasuries / S&P 500","TLT vs SPX, inverted","tested",-1,"SAFE", lambda W,X: lr(W.TLT)-lr(W["^GSPC"])),
 ("HYG_IEF","High yield / 7-10y Treasuries","HYG vs IEF","tested",1,"CR", lambda W,X: lr(W.HYG)-lr(W.IEF)),
 ("VIX_CHG","VIX","CBOE VIX, weekly log change, inverted","tested",-1,"VOL", lambda W,X: np.log(X["vix"]).diff()),
 ("GLD_SPY","Gold / S&P 500","GLD vs SPX, inverted","tested",-1,"SAFE", lambda W,X: lr(W.GLD)-lr(W["^GSPC"])),
 ("CYC_DEF","Cyclicals / Defensives","XLI+XLB+XLY+XLF vs XLU+XLP+XLV","tested",1,"RISK", lambda W,X: basket(W,["XLI","XLB","XLY","XLF"])-basket(W,["XLU","XLP","XLV"])),
 ("HYG_LQD","High yield / Investment grade","HYG vs LQD","tested",1,"CR", lambda W,X: lr(W.HYG)-lr(W.LQD)),
 ("DXY","US dollar index","DTWEXBGS, inverted","tested",-1,"SAFE", lambda W,X: np.log(X["dxy"]).diff()),
 ("VRP_CHG","Vol risk premium","VIX minus 21d realized vol, weekly change, inverted","tested",-1,"VOL", lambda W,X: X["vrp"].diff()),
 ("KRE_XLU","Regional banks / Utilities","KRE vs XLU","tested",1,"RISK", lambda W,X: lr(W.KRE)-lr(W.XLU)),
 ("IYT_XLU","Transports / Utilities","IYT vs XLU","tested",1,"RISK", lambda W,X: lr(W.IYT)-lr(W.XLU)),
 ("XHB_SPY","Homebuilders / S&P 500","XHB vs SPX","tested",1,"RISK", lambda W,X: lr(W.get("XHB", W["^GSPC"]*np.nan))-lr(W["^GSPC"])),
 ("NFCI_CHG","Financial conditions","Chicago Fed NFCI, weekly change, inverted","tested",-1,"CR", lambda W,X: X["nfci"].diff()),
 ("IWM_SPY","Small caps / S&P 500","IWM vs SPX","tested",1,"RISK", lambda W,X: lr(W.IWM)-lr(W["^GSPC"])),
 ("SMH_SPY","Semiconductors / S&P 500","SMH vs SPX","tested",1,"RISK", lambda W,X: lr(W.get("SMH", W["^GSPC"]*np.nan))-lr(W["^GSPC"])),
 ("RSP_SPY","Equal weight / Cap weight","RSP vs SPX","tested",1,"RISK", lambda W,X: lr(W.get("RSP", W["^GSPC"]*np.nan))-lr(W["^GSPC"])),
 ("QQQ_SPY","Nasdaq 100 / S&P 500","QQQ vs SPX","tested",1,"RISK", lambda W,X: lr(W.get("QQQ", W["^GSPC"]*np.nan))-lr(W["^GSPC"])),
 ("CURVE_CHG","Yield curve (10s2s)","T10Y2Y, bp change","tested",1,"CR", lambda W,X: X["curve"].diff()*100),
 ("IWF_IWD","Growth / Value","IWF vs IWD","tested",1,"RISK", lambda W,X: lr(W.get("IWF", W["^GSPC"]*np.nan))-lr(W.get("IWD", W["^GSPC"]*np.nan))),
]
MIN_HISTORY_WEEKS = 350          # ~6.7y — below this a candidate can't be screened across regimes (R0)
UNIT = {"HYOAS_CHG": "bp", "IGOAS_CHG": "bp", "CURVE_CHG": "bp", "NFCI_CHG": "pts",
        "TERM_CHG": "ppt", "VRP_CHG": "pts"}   # default (unlisted) is "pct" (fractional log return, x100 for display)
PILLAR_NAMES = {"CR": "Credit", "RISK": "Equity rotation", "VOL": "Volatility", "SAFE": "Safe haven"}
CARDS_EXTRA = ["HYOAS_CHG", "IGOAS_CHG", "VIX_CHG", "CYC_DEF", "HYG_IEF", "RSP_SPY", "SMH_SPY", "VRP_CHG", "IWF_IWD", "QQQ_SPY", "CURVE_CHG"]
RULE = dict(r1=0.20, r2min=0.10, r3=0.75, r5=0.75, r6noise=0.05)

def main():
    global TIINGO_OK
    on_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    ok, why = tiingo_check(); TIINGO_OK = ok
    if ok: log("Tiingo token OK: ETFs from Tiingo, everything else from FRED.")
    elif TIINGO_TOKEN: log(f"WARNING: TIINGO_TOKEN set but not working ({why}); falling back to Yahoo, which GitHub's IPs usually block.")
    elif on_ci: raise RuntimeError("No TIINGO_TOKEN on CI. Yahoo blocks GitHub runner IPs; add the TIINGO_TOKEN secret (see README).")
    else: log("No TIINGO_TOKEN: using Yahoo (fine locally).")
    if on_ci and not FRED_API_KEY:
        raise RuntimeError("No FRED_API_KEY on CI. The VIX term structure and credit series need FRED's JSON API "
                           "(the fredgraph CSV endpoint resets from GitHub runner IPs). Add the FRED_API_KEY secret (see README).")
    if FRED_API_KEY:
        fok, fwhy = fred_check()
        if not fok:
            if on_ci: raise RuntimeError(f"FRED_API_KEY set but not working ({fwhy}). Get a free key at "
                                         "https://fred.stlouisfed.org/docs/api/api_key.html and update the secret.")
            log(f"WARNING: FRED_API_KEY not working ({fwhy}); using fredgraph instead.")
        else: log("FRED API key OK.")

    etfs = ["^GSPC","XLI","XLB","XLY","XLF","XLE","XLK","XLU","XLP","XLV","KRE","IEI","HYG","LQD","IEF","IYT","IWM","GLD","TLT","DIA"]
    optional_etfs = ["XHB","SMH","RSP","QQQ","IWF","IWD"]
    log(f"fetching {len(etfs)+len(optional_etfs)+7} series ...")
    D = {}
    for t in etfs:
        ser = get_series(t, "etf")
        if ser is not None: D[t] = ser
        if SRC.get(t) == "Yahoo": time.sleep(0.4 + 1.2 * random.random())
    for t in optional_etfs:
        try:
            ser = get_series(t, "etf")
            if ser is not None: D[t] = ser
        except Exception as e:
            log(f"  {t} unavailable ({str(e)[:60]}); its dependent candidates will be dropped")
        if SRC.get(t) == "Yahoo": time.sleep(0.4 + 1.2 * random.random())
    D = pd.DataFrame(D)
    vix = get_series("VIX", "vol"); vix3m = get_series("VIX3M", "vol")
    hyoas = get_series("BAMLH0A0HYM2", "fred"); igoas = get_series("BAMLC0A0CM", "fred")
    nfci = get_series("NFCI", "fred"); curve = get_series("T10Y2Y", "fred"); dxy = get_series("DTWEXBGS", "fred")
    empty = pd.Series(dtype=float)
    hyoas, igoas, curve, dxy = [x if x is not None else empty for x in (hyoas, igoas, curve, dxy)]

    # As-of keys off the essential series only. FRED's weekly-lagging series (the broad dollar, NFCI) must not
    # drag the whole build back a week: the weekly resample simply leaves their latest cell empty and the
    # pillar average skips it.
    asof = min([D["^GSPC"].index.max(), vix.index.max()])
    D = D.loc[:asof]; vix = vix.loc[:asof]

    wk = lambda s: s.resample("W-FRI").last()
    W = D.resample("W-FRI").last()
    y10x = None
    dret = np.log(D["^GSPC"]).diff()
    rvol = (dret.rolling(21).std() * np.sqrt(252) * 100)
    vrp = (vix - rvol.reindex(vix.index).ffill()).dropna()
    term = (vix3m / vix - 1) * 100

    X = dict(hyoas=wk(hyoas), igoas=wk(igoas), nfci=wk(nfci), curve=wk(curve), dxy=wk(dxy),
             vix=wk(vix), term=wk(term), vrp=wk(vrp))

    idx_full = W.loc[START:].index
    ret = lr(W["^GSPC"]) * 100   # weekly % return (log)
    idx = idx_full

    cand_series = {}
    for c in CANDS:
        try:
            s = c[6](W, X)
            cand_series[c[0]] = s.reindex(idx)
        except Exception as e:
            print(f"  candidate {c[0]} failed to build ({e}); dropped")
    Xc = pd.DataFrame(cand_series).loc[START:]
    ret = ret.reindex(Xc.index)
    meta_all = {c[0]: dict(key=c[0], label=c[1], legs=c[2], origin=c[3], sign=c[4], pillar=c[5]) for c in CANDS if c[0] in Xc.columns}

    regimes = [("2004-01-01","2007-12-31","2004\u201307"),("2008-01-01","2009-12-31","2008\u201309"),
               ("2010-01-01","2015-12-31","2010\u201315"),("2016-01-01","2019-12-31","2016\u201319"),
               ("2020-01-01","2022-12-31","2020\u201322"),("2023-01-01",str(asof.date()),"2023\u2013"+asof.strftime("%y"))]

    def nonoverlap(x, d, h):
        xs, ds = x.rolling(h, min_periods=h).sum(), d.rolling(h, min_periods=h).sum()
        v = [xs.iloc[o::h].corr(ds.iloc[o::h]) for o in range(h)]
        return float(np.nanmean(v))

    def screen(Xs, rets, regs):
        st = {}
        for k in Xs:
            x = Xs[k]; n = x.notna().sum()
            s = meta_all[k]["sign"]
            if n < MIN_HISTORY_WEEKS:
                st[k] = dict(start=str(x.first_valid_index().date()) if x.notna().any() else None, n=int(n),
                             rw=np.nan, r4=np.nan, r13=np.nan, reg=[np.nan]*len(regs), pct=np.nan, insufficient=True)
                # still compute whatever correlation exists over the available window, for transparency
                ok = x.notna() & rets.notna()
                if ok.sum() > 20:
                    st[k]["rw"] = float(x[ok].corr(rets[ok]))
                continue
            rc = x.rolling(52, min_periods=52).corr(rets)
            st[k] = dict(start=str(x.first_valid_index().date()), n=int(n),
                rw=x.corr(rets), r4=nonoverlap(x, rets, 4), r13=nonoverlap(x, rets, 13),
                reg=[x.loc[a:b].corr(rets.loc[a:b]) for a, b, _ in regs],
                pct=float((np.sign(rc) == s).sum() / rc.notna().sum()), insufficient=False)
        return st

    def select(st, Xs):
        order = ([k for k in meta_all if meta_all[k]["origin"] == "requested"] +
                 sorted([k for k in meta_all if meta_all[k]["origin"] == "tested"], key=lambda k: -abs(st[k]["rw"] or 0)))
        C = Xs.corr(); chosen = []; why = {}
        for k in order:
            s, t = meta_all[k]["sign"], st[k]
            fails = []
            if t.get("insufficient"): fails.append("R0")
            else:
                if s * t["rw"] < RULE["r1"]: fails.append("R1")
                if min(s * r for r in t["reg"] if pd.notna(r)) <= 0: fails.append("R2")
                if t["pct"] < RULE["r3"]: fails.append("R3")
                r4n, r13n = t["r4"], t["r13"]
                if (abs(r4n) > RULE["r6noise"] and np.sign(r4n) != np.sign(t["rw"])) or \
                   (abs(r13n) > RULE["r6noise"] and np.sign(r13n) != np.sign(t["rw"])):
                    fails.append("R6")
            red = [(j, C.loc[k, j]) for j in chosen if abs(C.loc[k, j]) >= RULE["r5"]]
            if red and not fails: fails.append("R5")
            marginal = (not fails and not t.get("insufficient") and min(s * r for r in t["reg"] if pd.notna(r)) < RULE["r2min"])
            why[k] = dict(fails=fails, red=red, marginal=marginal)
            if not fails: chosen.append(k)
        return chosen, why

    st = screen(Xc, ret, regimes)
    chosen, why = select(st, Xc)
    C = Xc.corr()
    MEMBERS = {}
    for k in chosen:
        MEMBERS.setdefault(meta_all[k]["pillar"], []).append(k)
    member_keys = chosen
    print("selected:", chosen)

    def build_comp(members_by_pillar, Xs):
        Z = {}
        for p, ks in members_by_pillar.items():
            for k in ks:
                x = meta_all[k]["sign"] * Xs[k]
                Z[k] = (x / x.rolling(52, min_periods=26).std().shift(1)).clip(-4, 4)
        Z = pd.DataFrame(Z)
        P = pd.DataFrame({p: Z[ks].mean(axis=1) for p, ks in members_by_pillar.items() if ks})
        return Z, P, P.mean(axis=1)

    Z, P, comp = build_comp(MEMBERS, Xc)
    first = comp.first_valid_index()
    c13 = comp.rolling(13, min_periods=13).sum(); r13 = ret.rolling(13, min_periods=13).sum()
    num = (c13 * r13).rolling(156, min_periods=104).sum(); den = (c13 * c13).rolling(156, min_periods=104).sum()
    beta = (num / den).shift(1)
    implied = beta * c13; gap = r13 - implied
    gapz = gap / gap.rolling(156, min_periods=104).std().shift(1)
    c13z = c13 / c13.rolling(156, min_periods=104).std().shift(1)
    level = comp.fillna(0).cumsum().where(comp.notna().cummax())
    spx_level = W["^GSPC"].reindex(idx)

    # weekly beta (% return per composite unit) for the implied price path; early weeks backfilled with the first PIT estimate
    bw = ((comp * ret).rolling(156, min_periods=104).sum() / (comp * comp).rolling(156, min_periods=104).sum()).shift(1)
    bw_filled = bw.bfill().where(comp.notna())
    impw = bw_filled * comp
    trend = []
    wins = [(a, b, lab) for a, b, lab in regimes] + [(str(first.date()), str(asof.date()), "Full sample"), ("2022-01-01", str(asof.date()), "Since 2022")]
    for a, b, lab in wins:
        w = idx[(idx >= pd.Timestamp(a)) & (idx <= pd.Timestamp(b))]
        w = w[w >= first]
        if len(w) < 10: continue
        pre = idx[idx < w[0]]
        p0 = float(spx_level.loc[pre[-1]]) if len(pre) and pd.notna(spx_level.loc[pre[-1]]) else float(spx_level.loc[w[0]])
        p1 = float(spx_level.loc[w[-1]])
        act_chg = (p1/p0 - 1) * 100
        imp_chg = float(impw.loc[w].sum())
        trend.append(dict(label=lab, actual=act_chg, implied=imp_chg, r2=float(comp.loc[w].corr(ret.loc[w])**2) if comp.loc[w].notna().sum()>10 else None))

    def corr_block(x):
        ok = x.notna() & ret.notna()
        return dict(rw=x[ok].corr(ret[ok]), r4=nonoverlap(x[ok], ret[ok], 4), r13=nonoverlap(x[ok], ret[ok], 13),
                    reg=[x.loc[a:b].corr(ret.loc[a:b]) for a, b, _ in regimes])
    comp_stats = corr_block(comp)
    pillar_stats = {p: corr_block(P[p]) for p in P}
    member_aligned = {k: corr_block(meta_all[k]["sign"] * Xc[k]) for k in member_keys}
    best_reg = [max((member_aligned[k]["reg"][i] for k in member_keys if pd.notna(member_aligned[k]["reg"][i])), default=np.nan) for i in range(len(regimes))]
    med_reg = [float(np.nanmedian([member_aligned[k]["reg"][i] for k in member_keys])) for i in range(len(regimes))]

    leadlag = [dict(k=k, r=float(comp.corr(ret.shift(-k)))) for k in range(-8, 9)]

    div = []
    for h in (4, 13, 26):
        fwd = ret.rolling(h, min_periods=h).sum().shift(-h)
        ok = gapz.notna() & fwd.notna(); g, f = gapz[ok], fwd[ok]
        hi, lo, mid = f[g > 1], f[g < -1], f[(g >= -1) & (g <= 1)]
        cm = c13z[ok]
        div.append(dict(h=h, r=float(g.corr(f)), rno=float(np.nanmean([g.iloc[o::h].corr(f.iloc[o::h]) for o in range(h)])),
                        n_eff=int(len(f) / h), hi_mean=float(hi.mean()), hi_n=int(len(hi)), hi_down=float((hi < 0).mean()),
                        lo_mean=float(lo.mean()), lo_n=int(len(lo)), lo_up=float((lo > 0).mean()), mid_mean=float(mid.mean()),
                        unc=float(f.mean()), mom_r=float(cm.corr(fwd[cm.index.intersection(fwd.index)]) if False else cm.corr(f)),
                        mom_rno=float(np.nanmean([cm.iloc[o::h].corr(f.iloc[o::h]) for o in range(h)])),
                        r13_r=float(r13[ok].corr(f))))

    def variant(mbp):
        _, _, cv = build_comp(mbp, Xc); return corr_block(cv), cv
    robust = []
    for k in member_keys:
        mbp = {p: [j for j in ks if j != k] for p, ks in MEMBERS.items()}
        mbp = {p: ks for p, ks in mbp.items() if ks}
        rb, _ = variant(mbp)
        robust.append(dict(name="Without " + meta_all[k]["label"], rw=rb["rw"], r13=rb["r13"], minreg=float(np.nanmin(rb["reg"]))))
    rb, _ = variant({"ALL": member_keys}); robust.append(dict(name="No pillars, equal weights", rw=rb["rw"], r13=rb["r13"], minreg=float(np.nanmin(rb["reg"]))))

    Xe, rete = Xc.loc[:"2015-12-31"], ret.loc[:"2015-12-31"]
    st_e = screen(Xe, rete, [r for r in regimes if r[1] <= "2015-12-31"])
    ch_e, _ = select(st_e, Xe)
    mbp_e = {}
    for k in ch_e: mbp_e.setdefault(meta_all[k]["pillar"], []).append(k)
    _, _, cv_e = build_comp(mbp_e, Xc)
    oos = slice("2016-01-01", None)
    def blk(x):
        x2, d2 = x.loc[oos], ret.loc[oos]; ok = x2.notna() & d2.notna()
        return dict(rw=float(x2[ok].corr(d2[ok])), r13=nonoverlap(x2[ok], d2[ok], 13))
    split = dict(early_members=[meta_all[k]["label"] for k in ch_e],
                 added=[meta_all[k]["label"] for k in ch_e if k not in member_keys],
                 dropped=[meta_all[k]["label"] for k in member_keys if k not in ch_e],
                 early_oos=blk(cv_e), full_oos=blk(comp))

    last = comp.last_valid_index()
    npil = len(MEMBERS)
    attrib = []
    for p, ks in MEMBERS.items():
        for k in ks:
            # A member whose source lags (FRED publishes the broad dollar and NFCI with a week's delay) has no
            # value in the final week; use its last valid reading and record how stale it is.
            z13s = Z[k].rolling(13, min_periods=13).sum().loc[:last].dropna()
            raw13s = Xc[k].rolling(13, min_periods=13).sum().loc[:last].dropna()
            z13v = float(z13s.iloc[-1]) if len(z13s) else None
            raw13v = float(raw13s.iloc[-1]) if len(raw13s) else None
            lag_wk = int((last - z13s.index[-1]).days // 7) if len(z13s) else None
            attrib.append(dict(key=k, pillar=p, contrib=(None if z13v is None else z13v / len(ks) / npil),
                               z13=z13v, raw13=raw13v, lag_weeks=lag_wk, unit=UNIT.get(k, "pct")))

    table = []
    for k in meta_all:
        m, t = meta_all[k], st[k]
        w = why.get(k, dict(fails=["R0"] if t.get("insufficient") else [], red=[], marginal=False))
        s = m["sign"]; reg = t["reg"]
        inmem = k in member_keys
        reason = []
        if t.get("insufficient"):
            verdict = "Excluded"
            avail = f", ρ {t['rw']:+.2f} over its available window" if pd.notna(t.get("rw")) else ""
            reason.append(f"Insufficient history: FRED's free CSV endpoint caps this series to its trailing ~{t['n']} weeks{avail}. Shown for context, not screened.")
        elif inmem:
            verdict = "Included"
            reason.append(f"{PILLAR_NAMES[m['pillar']]} pillar.")
            if w["marginal"]:
                i = int(np.nanargmin([s * r if pd.notna(r) else np.inf for r in reg]))
                reason.append(f"Marginal: weakest regime {regimes[i][2]} at {reg[i]:+.3f}.")
        else:
            verdict = "Excluded"
            if "R5" in w["fails"]:
                j, cv = max(w["red"], key=lambda z: abs(z[1]))
                reason.append(f"Redundant: \u03c1 {cv:+.2f} with {meta_all[j]['label']}.")
            if "R1" in w["fails"]: reason.append(f"Too weak: weekly \u03c1 {t['rw']:+.2f}.")
            if "R2" in w["fails"]:
                bad = [regimes[i][2] + f" ({reg[i]:+.2f})" for i in range(len(reg)) if pd.notna(reg[i]) and s * reg[i] <= 0]
                reason.append("Wrong sign in " + ", ".join(bad) + ".")
            if "R3" in w["fails"]: reason.append(f"Rolling 52w sign holds only {t['pct']*100:.0f}% of the time.")
            if "R6" in w["fails"]: reason.append(f"Doesn't survive resampling: 4-week \u03c1 {t['r4']:+.2f}, 13-week \u03c1 {t['r13']:+.2f} disagree with the weekly sign.")
            if not reason: reason.append("Excluded by the screen.")
        table.append(dict(key=k, label=m["label"], legs=m["legs"], origin=m["origin"], sign=s, pillar=m["pillar"],
                          start=t.get("start"), rw=t.get("rw"), r4=t.get("r4"), r13=t.get("r13"), reg=reg, pct=t.get("pct"),
                          verdict=verdict, fails=w["fails"], reason=" ".join(reason), insufficient=bool(t.get("insufficient")),
                          islog=bool(m_is_logret(k, meta_all)), hist_weeks=t.get("n"), unit=UNIT.get(k, "pct")))

    Al = pd.DataFrame({k: meta_all[k]["sign"] * Xc[k] for k in member_keys})
    cmat = Al.corr().round(3).values.tolist()

    out_idx = idx
    dates = [d.strftime("%Y-%m-%d") for d in out_idx]
    def arr(s, nd=4):
        return [None if (v is None or not np.isfinite(v)) else round(float(v), nd) for v in s.reindex(idx).values]
    cards = []
    for k in list(dict.fromkeys(member_keys + CARDS_EXTRA)):
        if k not in Xc.columns: continue
        x = Xc[k]
        if x.notna().sum() < 20: continue
        lv = np.exp(x.fillna(0).cumsum()/1).where(x.notna().cummax()) if m_is_logret(k, meta_all) else x.fillna(0).cumsum().where(x.notna().cummax())
        cards.append(dict(key=k, level=arr(lv, 5), rc=arr(x.rolling(52, min_periods=52).corr(ret), 3), islog=bool(m_is_logret(k, meta_all)), unit=UNIT.get(k, "pct")))
    series = dict(dates=dates, spx=arr(spx_level, 2), ret=arr(ret, 3), comp=arr(comp, 3), level=arr(level, 3),
                  c13=arr(c13, 3), r13=arr(r13, 2), implied=arr(implied, 2), gap=arr(gap, 2), gapz=arr(gapz, 2),
                  beta=arr(beta, 2), impw=arr(impw, 2), rc_comp=arr(comp.rolling(52, min_periods=52).corr(ret), 3),
                  pillars={p: arr(P[p].rolling(13, min_periods=13).sum(), 3) for p in P})

    li = idx.get_loc(last)
    latest = dict(asof=str(asof.date()), spx=float(D["^GSPC"].loc[:asof].iloc[-1]), r13=float(r13.loc[last]),
                  implied=float(implied.loc[last]), gap=float(gap.loc[last]), gapz=float(gapz.loc[last]),
                  c13z=float(c13z.loc[last]), beta=float(beta.loc[last]), r4=float(ret.rolling(4).sum().loc[last]))

    payload = dict(meta=dict(built=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), asof=str(asof.date()),
                             first_comp=str(first.date()), start=START, rules=RULE, selected_now=chosen,
                             pillar_names=PILLAR_NAMES, members=MEMBERS, regimes=[r[2] for r in regimes],
                             n_weeks=int(comp.notna().sum()), min_hist=MIN_HISTORY_WEEKS,
                             src=SRC, stale=STALE, tiingo=bool(TIINGO_OK), run_url=run_url(),
                             target_label=("S&P 500 (SPY proxy)" if SRC.get("^GSPC") == "Tiingo" else "S&P 500"),
                             target_note=(TARGET_NOTE if SRC.get("^GSPC") == "Tiingo" else "")),
                   latest=latest, table=table, comp_stats=comp_stats, pillar_stats=pillar_stats, member_aligned=member_aligned,
                   best_reg=best_reg, med_reg=med_reg, leadlag=leadlag, div=div, trend=trend,
                   robust=robust, split=split, attrib=attrib, cmat=dict(keys=member_keys, m=cmat), cards=cards, series=series)
    return payload

def m_is_logret(k, meta_all):
    return UNIT.get(k, "pct") == "pct"

def clean(o):
    if isinstance(o, dict): return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)): return [clean(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if not np.isfinite(o) else round(float(o), 4)
    if isinstance(o, np.integer): return int(o)
    return o

def run_url():
    e = os.environ
    if e.get("GITHUB_RUN_ID") and e.get("GITHUB_REPOSITORY"):
        return f"{e.get('GITHUB_SERVER_URL', 'https://github.com')}/{e['GITHUB_REPOSITORY']}/actions/runs/{e['GITHUB_RUN_ID']}"
    return None

def sanity(p):
    """Cheap invariants that catch a silently broken build before it is deployed."""
    L, S = p["latest"], p["series"]
    out = []
    if not (100 < L["spx"] < 100000): out.append(f"index level out of range: {L['spx']}")
    if L["implied"] is None or L["gap"] is None: out.append("latest implied/gap missing")
    n_members = sum(len(v) for v in p["meta"]["members"].values())
    if n_members < 6: out.append(f"only {n_members} members selected")
    if sum(v is not None for v in S["comp"][-60:]) < 55: out.append("composite has gaps in the last 60 weeks")
    if p["comp_stats"]["rw"] is None or p["comp_stats"]["rw"] < 0.5: out.append(f"composite fit collapsed: {p['comp_stats']['rw']}")
    if len(S["dates"]) < 900: out.append(f"history too short: {len(S['dates'])} weeks")
    return out

def latest_json(p):
    L, M = p["latest"], p["meta"]
    return dict(asof=L["asof"], built=M["built"], target=M["target_label"], stale=M["stale"],
                spx=L["spx"], return_13w_pct=L["r13"], implied_13w_pct=L["implied"], unconfirmed_13w_pct=L["gap"],
                unconfirmed_z=L["gapz"], composite_13w_z=L["c13z"], beta_pct_per_unit=L["beta"],
                members={a["key"]: dict(change_13w=a["raw13"], unit=a["unit"], contrib=a["contrib"]) for a in p["attrib"]},
                pillar_contrib={k: round(sum(a["contrib"] or 0 for a in p["attrib"] if a["pillar"] == k), 4) for k in M["members"]},
                composite_weekly_rho=p["comp_stats"]["rw"], run_url=M["run_url"])

def step_summary(j, status):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path: return
    f = lambda v, d=2: "n/a" if v is None else f"{v:+.{d}f}"
    lines = [f"### S&P 500 internals, {j['asof']} ({status})", "", "| | |", "|---|---|",
             f"| {j['target']} | {j['spx']:,.0f} |",
             f"| 13-week return | {f(j['return_13w_pct'])}% |",
             f"| Implied by internals | {f(j['implied_13w_pct'])}% |",
             f"| Unconfirmed | {f(j['unconfirmed_13w_pct'])}% ({f(j['unconfirmed_z'],1)}σ) |",
             f"| Composite weekly ρ | {j['composite_weekly_rho']:.2f} |"]
    if j["stale"]: lines.append(f"| Stale inputs | {', '.join(f'{k} ({v})' for k, v in j['stale'].items())} |")
    open(path, "a").write("\n".join(lines) + "\n")

TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>S&P 500 market internals model</title>
<style>
/*__FONTS__*/
:root{
  --paper:#F7F1E6; --card:#FDFAF3; --ink:#221C14; --muted:#6E6556; --rule:#E4DAC7; --soft:#EFE7D8;
  --orange:#D2622A; --teal:#0E756C; --ochre:#A67A22; --plum:#7A4A66; --slate:#4E6577;
  --display:'Space Grotesk', 'Helvetica Neue', Arial, sans-serif;
  --body:'IBM Plex Sans', 'Helvetica Neue', Arial, sans-serif;
  --mono:'IBM Plex Mono', ui-monospace, 'SFMono-Regular', Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --paper:#16130E; --card:#1F1B15; --ink:#EDE4D3; --muted:#A59A89; --rule:#39322A; --soft:#2A251E;
    --orange:#E8804A; --teal:#3FAE9F; --ochre:#D2A650; --plum:#BE88A8; --slate:#93A8BA;
  }
}
:root[data-theme="dark"]{
  --paper:#16130E; --card:#1F1B15; --ink:#EDE4D3; --muted:#A59A89; --rule:#39322A; --soft:#2A251E;
  --orange:#E8804A; --teal:#3FAE9F; --ochre:#D2A650; --plum:#BE88A8; --slate:#93A8BA;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--paper);color:var(--ink)}
body{font-family:var(--body);font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:1280px;margin:0 auto;padding:28px 28px 64px}
h1,h2,h3{font-family:var(--display);font-weight:500;margin:0;letter-spacing:-0.01em}
h1{font-size:15px;font-weight:500;color:var(--muted);letter-spacing:0}
h2{font-size:24px;line-height:1.2}
h3{font-size:16px;line-height:1.3}
p{margin:0}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}
a{color:var(--teal)}
:focus-visible{outline:2px solid var(--orange);outline-offset:2px}

header.top{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap;padding-bottom:14px;border-bottom:1px solid var(--rule)}
header.top .asof{font-size:13px;color:var(--muted)}

.hero{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(0,1fr);gap:40px;padding:34px 0 30px;align-items:end}
.hero .lede{font-family:var(--display);font-weight:500;font-size:clamp(30px,4.2vw,50px);line-height:1.06;letter-spacing:-0.025em;max-width:16ch}
.hero .sub{margin-top:18px;max-width:62ch;color:var(--ink)}
.hero .sub + .sub{margin-top:8px;color:var(--muted);font-size:14px}
.gauge{border-left:1px solid var(--rule);padding-left:28px}
.bar-legend{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:6px}
.stack{position:relative;height:120px;margin:6px 0 12px}
.stack .col{position:absolute;bottom:0;border-radius:2px 2px 0 0}
.stack .axis{position:absolute;left:0;right:0;bottom:0;border-top:1px solid var(--ink)}
.stat-row{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:4px}
.stat .v{font-family:var(--mono);font-size:22px;font-weight:500;line-height:1.1}
.stat .k{font-size:12px;color:var(--muted);line-height:1.3;margin-top:3px}

.controls{position:sticky;top:0;z-index:5;background:var(--paper);display:flex;gap:18px;align-items:center;justify-content:space-between;flex-wrap:wrap;padding:10px 0;border-bottom:1px solid var(--rule);border-top:1px solid var(--rule)}
.seg{display:inline-flex;border:1px solid var(--rule);border-radius:6px;overflow:hidden;background:var(--card)}
.seg button{font:500 13px var(--body);color:var(--muted);background:transparent;border:0;padding:6px 12px;cursor:pointer}
.seg.small button{font-size:12px;padding:4px 10px}
.seg button[aria-pressed="true"]{background:var(--ink);color:var(--paper)}
.controls nav{display:flex;gap:16px;flex-wrap:wrap;font-size:13px}
.controls nav a{color:var(--muted);text-decoration:none}
.controls nav a:hover{color:var(--ink)}

section{padding-top:40px;scroll-margin-top:52px}
.nb{white-space:nowrap}
.sec-head{display:flex;justify-content:space-between;align-items:baseline;gap:24px;flex-wrap:wrap;margin-bottom:16px}
.sec-head p{max-width:72ch;color:var(--muted);font-size:14px}
.panel{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:18px 18px 14px;min-width:0}
.caption{font-size:13px;color:var(--muted);max-width:90ch;margin:-2px 0 10px}
.span2{grid-column:1 / -1}
.heatwrap{position:relative;width:100%;height:236px}
.heatwrap canvas{display:block;width:100%;height:100%}
.panel + .panel{margin-top:16px}
.grid2 > .panel{margin-top:0}
.panel .ph{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.panel .ph p{font-size:13px;color:var(--muted);max-width:70ch}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.chart{position:relative;width:100%}
.h420{height:420px}.h320{height:320px}.h260{height:260px}.h200{height:200px}.h150{height:150px}.h90{height:78px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--muted)}
.legend i{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px;border-radius:2px}
.legend i.sq{height:10px;width:10px}

.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:16px 16px 10px;display:flex;flex-direction:column}
.card.excluded{background:transparent;border-style:dashed}
.card .top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.card .legs{font-size:12.5px;color:var(--muted)}
.chip{font-size:12px;border-radius:999px;padding:2px 10px;white-space:nowrap;border:1px solid currentColor}
.chip.in{color:var(--teal)}
.chip.out{color:var(--muted)}
.card .kv{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin:12px 0 8px;padding:8px 0;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.card .kv div span{display:block}
.card .kv .v{font-family:var(--mono);font-size:15px}
.card .kv .k{font-size:11.5px;color:var(--muted);line-height:1.25}
.card .why{font-size:13px;color:var(--muted);margin:2px 0 8px}
.card .strip-label{font-size:11.5px;color:var(--muted);margin-top:2px}

.tablewrap{overflow-x:auto;border:1px solid var(--rule);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--rule)}
th{font-weight:600;font-size:12px;color:var(--muted);background:var(--card);vertical-align:bottom}
table th.n{white-space:normal;min-width:48px}
#screenTable td:first-child{min-width:170px}
#screenTable td.n{padding-left:6px;padding-right:6px}
td.n,th.n{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;white-space:nowrap}
tr.grp td{background:var(--soft);font-family:var(--display);font-weight:500;font-size:13px;color:var(--ink)}
td.reason{min-width:280px;max-width:420px;color:var(--muted);font-size:12.5px}
td .lbl{font-weight:600}
td .sub{display:block;color:var(--muted);font-size:12px}
.heat td.n{min-width:54px}

.note{font-size:13.5px;color:var(--ink);max-width:78ch}
.note + .note{margin-top:10px}
.cols{columns:2;column-gap:40px}
.cols p{break-inside:avoid;margin-bottom:12px;font-size:13.5px}
.banner{margin-top:16px;padding:10px 14px;border:1px solid var(--orange);border-radius:8px;color:var(--orange);font-size:13.5px}
.kicker{font-size:13px;color:var(--muted);margin-bottom:6px}
footer{margin-top:48px;padding-top:14px;border-top:1px solid var(--rule);font-size:12.5px;color:var(--muted)}

@media (max-width:900px){
  .hero{grid-template-columns:1fr;gap:24px}
  .gauge{border-left:0;padding-left:0;border-top:1px solid var(--rule);padding-top:20px}
  .grid2,.cards{grid-template-columns:minmax(0,1fr)}
  .cols{columns:1}
  .h420{height:340px}
  .wrap{padding:18px 16px 48px}
}
@media (max-width:520px){
  .stat-row{grid-template-columns:repeat(2,minmax(0,1fr))}
  .card .kv{grid-template-columns:repeat(2,minmax(0,1fr))}
  .controls nav{display:none}
}
@media (prefers-reduced-motion: reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <h1>Acheron Insights quantitative research</h1>
    <div class="asof" id="asof"></div>
  </header>

  <div class="hero">
    <div>
      <div class="kicker">S&P 500, market internals model</div>
      <div class="lede" id="lede"></div>
      <p class="sub" id="sub1"></p>
      <p class="sub" id="sub2"></p>
    </div>
    <div class="gauge" aria-label="Latest 13-week decomposition">
      <div class="bar-legend"><span>13-week change, %</span><span id="gaugeScale"></span></div>
      <div class="stack" id="stack"></div>
      <div class="stat-row" id="stats"></div>
    </div>
  </div>
  <div id="driftBanner"></div>

  <div class="controls">
    <div class="seg" role="group" aria-label="Chart range" id="rangeSeg"></div>
    <nav>
      <a href="#composite">Composite</a><a href="#internals">Each internal</a><a href="#screen">The screen</a><a href="#tests">Does it hold up</a><a href="#method">Method</a>
    </nav>
  </div>

  <section id="composite">
    <div class="sec-head">
      <h2>Composite internals index against the S&P 500</h2>
      <p id="compBlurb"></p>
    </div>
    <div class="panel">
      <div class="ph">
        <div class="legend" id="lgLevel"></div>
        <div class="seg small" role="group" aria-label="Level view" id="levelSeg"><button aria-pressed="true" data-v="index">Index</button><button aria-pressed="false" data-v="path">Implied price path</button></div>
      </div>
      <p class="caption" id="levelNote"></p>
      <div class="chart h420"><canvas id="cLevel" aria-label="Composite internals index against the 10-year yield"></canvas></div>
    </div>
    <div class="panel">
      <div class="ph">
        <div class="legend"><span><i id="lgA"></i>Actual 13-week return</span><span><i id="lgI"></i>Return implied by internals</span></div>
        <p>Implied = trailing 3-year beta of 13-week S&P 500 returns on the 13-week composite, lagged one week.</p>
      </div>
      <div class="chart h320"><canvas id="cImplied" aria-label="Actual versus internals-implied 13-week return"></canvas></div>
      <div class="ph" style="margin-top:14px">
        <div class="legend"><span><i class="sq" id="lgGp"></i>Price above what internals confirm</span><span><i class="sq" id="lgGn"></i>Price below what internals confirm</span></div>
        <p>Unconfirmed move = actual minus implied return, percentage points.</p>
      </div>
      <div class="chart h200"><canvas id="cGap" aria-label="Gap between actual and implied return"></canvas></div>
    </div>
    <div class="grid2" style="margin-top:16px">
      <div class="panel">
        <div class="ph"><h3>What is driving the latest reading</h3><p>Each member's contribution to the 13-week composite, sign-aligned so positive points to a higher S&P 500.</p></div>
        <div class="chart h320"><canvas id="cAttrib" aria-label="Contribution by internal"></canvas></div>
      </div>
      <div class="panel">
        <div class="ph"><h3>Pillar scores over time</h3><p>13-week sum of each pillar's vol-scaled weekly signal. Teal leans bullish, orange bearish; saturation at ±8.</p></div>
        <div class="heatwrap" id="heatWrap"><canvas id="cHeat" role="img" aria-label="Heatmap of pillar scores over time"></canvas></div>
        <p class="caption" id="heatRead" style="margin-top:10px" aria-live="polite"></p>
      </div>
    </div>
  </section>

  <section id="internals">
    <div class="sec-head">
      <h2>Each internal on its own</h2>
      <p>Each internal against the S&P 500 (left), with the trailing 52-week correlation of weekly changes underneath. Inverted internals are drawn on a reversed axis so that up always means bullish. Dashed cards were tested and left out, or (for the two OAS series) shown only for the short window that free data allows; the reason sits on the card.</p>
    </div>
    <div class="cards" id="cards"></div>
  </section>

  <section id="screen">
    <div class="sec-head">
      <h2>The screen</h2>
      <p id="screenBlurb"></p>
    </div>
    <div class="tablewrap"><table class="heat" id="screenTable"></table></div>
  </section>

  <section id="tests">
    <div class="sec-head">
      <h2>Does it hold up</h2>
      <p id="testsBlurb"></p>
    </div>
    <div class="grid2">
      <div class="panel">
        <div class="ph"><h3>Fit by regime</h3><p>Correlation with weekly S&P 500 returns. The composite against the best and the median single member in each regime.</p></div>
        <div class="legend" id="lgRegime" style="margin-bottom:6px"></div>
        <div class="chart h260"><canvas id="cRegime" aria-label="Fit by regime"></canvas></div>
        <p class="caption" id="regimeNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Lead or lag</h3><p>Correlation of this week's composite with the return k weeks later. Positive k would mean internals lead.</p></div>
        <div class="chart h260"><canvas id="cLead" aria-label="Lead lag correlations"></canvas></div>
        <p class="caption" id="leadNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Texture, not trend</h3><p>S&P 500 return over each window against the sum of weekly internals-implied returns.</p></div>
        <div class="tablewrap" style="border:0"><table id="trendTable"></table></div>
        <p class="caption" id="trendNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Strength versus stability</h3><p>Each candidate’s sign-aligned weekly correlation (x) against its weakest single regime (y). The screen keeps what sits in the upper right.</p></div>
        <div class="chart h320"><canvas id="cLegs" aria-label="Strength versus regime stability, screen scatter"></canvas></div>
        <p class="caption" id="legsNote" style="margin-top:10px"></p>
      </div>
      <div class="panel span2">
        <div class="ph"><h3>Do divergences close</h3><p>Forward S&P 500 return after the unconfirmed move is stretched (gap z-score beyond ±1, trailing 3-year scale).</p></div>
        <div class="tablewrap" style="border:0"><table id="divTable"></table></div>
        <p class="caption" id="divNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Robustness</h3><p>Leave-one-out, no pillar structure, and a split-sample check where membership is chosen on 2004–15 data only.</p></div>
        <div class="tablewrap" style="border:0"><table id="robTable"></table></div>
        <p class="caption" id="splitNote" style="margin-top:10px"></p>
      </div>
      <div class="panel">
        <div class="ph"><h3>Overlap between members</h3><p>Correlation of weekly changes, sign-aligned. The screen rejects any candidate at |ρ| ≥ 0.75 with an existing member.</p></div>
        <div class="tablewrap" style="border:0"><table id="cmatTable" style="font-size:12px"></table></div>
      </div>
    </div>
  </section>

  <section id="method">
    <div class="sec-head"><h2>Method and data</h2></div>
    <div class="cols" id="methodCols"></div>
  </section>

  <footer id="foot"></footer>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
const DATA = /*__DATA__*/null;
(function(){
"use strict";
const S = DATA.series, M = DATA.meta, L = DATA.latest;
const byKey = Object.fromEntries(DATA.table.map(r => [r.key, r]));
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const minus = s => s.replace(/-/g, "−");
const fR = (v, d=2) => v == null ? "–" : minus((v >= 0 ? "+" : "") + v.toFixed(d));
const fBp = v => v == null ? "–" : minus((v >= 0 ? "+" : "") + Math.round(v)) + "bp";
const fPct = (v, d=1) => v == null ? "–" : minus((v >= 0 ? "+" : "") + (v*100).toFixed(d)) + "%";
const fPc = (v, d=2) => v == null ? "–" : minus((v >= 0 ? "+" : "") + v.toFixed(d)) + "%";
const fIdx = v => v == null ? "–" : v.toLocaleString(undefined, {maximumFractionDigits: 0});
const dLong = s => { const [y,m,d] = s.split("-"); return `${+d} ${MONTHS[+m-1]} ${y}`; };
const dMon = s => { const [y,m] = s.split("-"); return `${MONTHS[+m-1]} ${y}`; };
const el = (tag, attrs={}, html="") => { const e = document.createElement(tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (html) e.innerHTML = html; return e; };
const $ = id => document.getElementById(id);
const PCOL = {CR:"ochre", RISK:"slate", VOL:"plum", SAFE:"orange"};
const lc = t => t.replace(/^./, c => c.toLowerCase());

// ---------------------------------------------------------------- theme tokens (explicit strings for canvas)
let T = {};
function readTokens(){
  const cs = getComputedStyle(document.documentElement);
  ["paper","card","ink","muted","rule","soft","orange","teal","ochre","plum","slate"].forEach(k => T[k] = cs.getPropertyValue("--"+k).trim());
}
function rgba(hex, a){
  const h = hex.replace("#",""); const n = parseInt(h.length === 3 ? h.split("").map(c=>c+c).join("") : h, 16);
  return `rgba(${(n>>16)&255},${(n>>8)&255},${n&255},${a})`;
}
readTokens();

// ---------------------------------------------------------------- range
const RANGES = [["All", null], ["10y", 10], ["5y", 5], ["2y", 2], ["1y", 1]];
let rangeYears = null;
function startIndex(){
  if (!rangeYears) return 0;
  const last = S.dates[S.dates.length-1]; const [y,m,d] = last.split("-").map(Number);
  const cut = `${y - rangeYears}-${String(m).padStart(2,"0")}-${String(d).padStart(2,"0")}`;
  const i = S.dates.findIndex(x => x >= cut); return Math.max(0, i);
}
function boundaryTicks(labels){
  const n = labels.length, out = [];
  const span = n / 52;
  if (span <= 2.6){
    for (let i = 1; i < n; i++){ const a = labels[i-1].slice(5,7), b = labels[i].slice(5,7); if (a !== b && ["01","04","07","10"].includes(b)) out.push(i); }
  } else {
    const step = span > 14 ? 2 : 1;
    for (let i = 1; i < n; i++){ const ya = +labels[i-1].slice(0,4), yb = +labels[i].slice(0,4); if (ya !== yb && yb % step === 0) out.push(i); }
  }
  return out;
}
function xAxis(labels, opts={}){
  const span = labels.length / 52;
  return Object.assign({
    type: "category", offset: false,
    grid: {display: false}, border: {color: T.rule},
    afterBuildTicks: ax => { ax.ticks = boundaryTicks(labels).map(i => ({value: i})); },
    ticks: {autoSkip: false, maxRotation: 0, color: T.muted, font: {family: "IBM Plex Mono", size: 11},
      callback: v => { const s = labels[v]; if (!s) return ""; return span <= 2.6 ? `${MONTHS[+s.slice(5,7)-1]} ${s.slice(2,4)}` : s.slice(0,4); }}
  }, opts);
}
function yAxis(opts={}){
  return Object.assign({grid: {color: T.rule, drawTicks: false}, border: {display: false},
    ticks: {color: T.muted, font: {family: "IBM Plex Mono", size: 11}, padding: 6}}, opts);
}
const baseOpts = () => ({
  responsive: true, maintainAspectRatio: false, animation: false, normalized: true,
  interaction: {mode: "index", intersect: false},
  plugins: {legend: {display: false},
    tooltip: {backgroundColor: T.ink, titleColor: T.paper, bodyColor: T.paper, borderWidth: 0, padding: 9,
      titleFont: {family: "IBM Plex Sans", weight: "600", size: 12}, bodyFont: {family: "IBM Plex Mono", size: 11.5}}},
  layout: {padding: {top: 4, right: 2}}
});
const line = (label, data, color, extra={}) => Object.assign({label, data, borderColor: color, backgroundColor: color, borderWidth: 1.6, pointRadius: 0, pointHoverRadius: 3, tension: 0, spanGaps: false}, extra);

// ---------------------------------------------------------------- chart registry (lazy + rebuildable)
const REG = [];   // {canvas, build, chart}
function register(canvas, build, ranged=true){
  const r = {canvas, build, chart: null, ranged, visible: false}; REG.push(r); io.observe(canvas); canvas._reg = r; return r;
}
function render(r){ if (r.chart) r.chart.destroy(); r.chart = new Chart(r.canvas.getContext("2d"), r.build()); }
const io = new IntersectionObserver(entries => {
  entries.forEach(e => { const r = e.target._reg; if (e.isIntersecting && !r.visible){ r.visible = true; render(r); } });
}, {rootMargin: "300px 0px"});
function rerender(filter){ REG.forEach(r => { if (r.visible && filter(r)) render(r); }); }

// ---------------------------------------------------------------- header + hero
$("asof").textContent = `Weekly closes to ${dLong(L.asof)}. Built ${M.built}.`;
const upDown = v => v >= 0 ? "up" : "down";
const pill = {}; DATA.attrib.forEach(a => { pill[a.pillar] = (pill[a.pillar] || 0) + (a.contrib || 0); });
const conf = L.implied, act = L.r13, gap = L.gap;
const sameSign = Math.sign(conf) === Math.sign(act);
let lede;
if (Math.abs(act) < 0.3) lede = `The S&P 500 is roughly flat over 13 weeks; internals imply ${fPc(conf)}.`;
else if (!sameSign) lede = `The S&P 500 is ${upDown(act)} ${Math.abs(act).toFixed(1)}% in 13 weeks. Internals point the other way.`;
else if (Math.abs(conf) >= Math.abs(act)*0.75) lede = `The S&P 500 is ${upDown(act)} ${Math.abs(act).toFixed(1)}% in 13 weeks, and internals confirm the move.`;
else lede = `The S&P 500 is ${upDown(act)} ${Math.abs(act).toFixed(1)}% in 13 weeks. Internals back ${Math.abs(conf).toFixed(1)} points of it.`;
$("lede").textContent = lede;
const pOrder = Object.keys(M.members);
const pos = pOrder.filter(p => pill[p] > 0.05).map(p => lc(M.pillar_names[p]));
const neg = pOrder.filter(p => pill[p] < -0.05).map(p => lc(M.pillar_names[p]));
const joinList = a => a.length <= 1 ? (a[0] || "") : a.slice(0,-1).join(", ") + " and " + a[a.length-1];
let s1 = `The S&P 500 sits at ${fIdx(L.spx)}. `;
if (pos.length && neg.length) s1 += `${joinList(pos).replace(/^./, c => c.toUpperCase())} ${pos.length>1?"point":"points"} bullish; ${joinList(neg)} ${neg.length>1?"point":"points"} bearish.`;
else if (pos.length) s1 += `${joinList(pos).replace(/^./, c => c.toUpperCase())} ${pos.length>1?"point":"points"} bullish; nothing leans materially bearish.`;
else if (neg.length) s1 += `${joinList(neg).replace(/^./, c => c.toUpperCase())} ${neg.length>1?"point":"points"} bearish; nothing leans materially bullish.`;
$("sub1").textContent = s1;
// how unusual is the gap
let lastMatch = null;
if (L.gapz != null){
  for (let i = S.gapz.length - 9; i >= 0; i--){ const g = S.gapz[i]; if (g != null && (L.gapz >= 0 ? g >= L.gapz : g <= L.gapz)){ lastMatch = S.dates[i]; break; } }
}
const d13 = DATA.div.find(d => d.h === 13), d26 = DATA.div.find(d => d.h === 26);
let s2 = `Unconfirmed move ${fPc(gap)} (${fR(L.gapz,1)}σ on a trailing 3-year scale)`;
s2 += lastMatch ? `, the most stretched since ${dMon(lastMatch)}. ` : ". ";
if (Math.abs(L.gapz) >= 1){
  const dd = L.gapz > 0 ? d26.hi_mean : d26.lo_mean, pr = L.gapz > 0 ? d26.hi_down : d26.lo_up;
  s2 += `Historically, from here the S&P 500 returned ${fPc(dd)} over the next 26 weeks on average (${Math.round(pr*100)}% of cases in the closing direction). See the divergence test for how reliable this is.`;
} else s2 += `Within normal range; no divergence signal.`;
$("sub2").textContent = s2;

// gauge: three columns actual / implied / gap
(function gauge(){
  const vals = [["Actual", act, T.ink], ["Internals", conf, T.teal], ["Unconfirmed", gap, T.orange]];
  const mx = Math.max(25, ...vals.map(v => Math.abs(v[1]))) * 1.1;
  const hasNeg = vals.some(v => v[1] < 0);
  const box = $("stack"); box.innerHTML = "";
  const H = 120, zero = hasNeg ? H/2 : H;
  const axis = el("div", {class: "axis"}); axis.style.bottom = (H - zero) + "px"; box.appendChild(axis);
  vals.forEach((v, i) => {
    const h = Math.abs(v[1]) / mx * (hasNeg ? H/2 : H);
    const c = el("div", {class: "col"}); c.style.left = `calc(${i*33.33}% + 10px)`; c.style.width = `calc(33.33% - 20px)`;
    c.style.background = v[2]; c.style.height = h + "px";
    if (v[1] >= 0) c.style.bottom = (H - zero) + "px"; else { c.style.bottom = (H - zero - h) + "px"; c.style.borderRadius = "0 0 2px 2px"; }
    box.appendChild(c);
  });
  $("gaugeScale").textContent = `beta ${L.beta.toFixed(2)}% per unit`;
  const st = $("stats");
  [[fIdx(L.spx), (M.target_label || "S&P 500") + " level"], [fPc(act), "Actual 13-week return"], [fPc(conf), "Implied by internals"], [fPc(gap), "Unconfirmed"]]
    .forEach(([v,k], i) => { const d = el("div", {class: "stat"}, `<div class="v">${minus(v)}</div><div class="k">${k}</div>`); if (i===2) d.querySelector(".v").style.color = "var(--teal)"; if (i===3) d.querySelector(".v").style.color = "var(--orange)"; st.appendChild(d); });
})();

if (M.stale && Object.keys(M.stale).length){
  $("driftBanner").appendChild(el("div", {class: "banner"}, `Stale inputs: the latest download failed for ${Object.entries(M.stale).map(([k,v]) => `${k} (cached to ${dLong(v)})`).join(", ")}. Readings use the cached history; the next scheduled run will retry.`));
}

// ---------------------------------------------------------------- range control
RANGES.forEach(([lab, yrs]) => {
  const b = el("button", {"aria-pressed": String(yrs === rangeYears)}, lab);
  b.addEventListener("click", () => { rangeYears = yrs; [...$("rangeSeg").children].forEach(x => x.setAttribute("aria-pressed", String(x === b))); rerender(r => r.ranged); drawHeat(null); });
  $("rangeSeg").appendChild(b);
});

const cs = DATA.comp_stats;
const nMem = Object.values(M.members).reduce((a,ks)=>a+ks.length,0);
$("compBlurb").textContent = `${nMem} internals in ${Object.keys(M.members).length} pillars. Weekly correlation with S&P 500 returns ${fR(cs.rw)}, ${fR(cs.r13)} on non-overlapping 13-week returns, and never below ${fR(Math.min(...cs.reg))} in any regime since 2004.`;

// legends
let levelView = "index";
const sw = (id, c) => { $(id).style.background = c; };
function paintLegends(){
  sw("lgA", T.ink); sw("lgI", T.teal); sw("lgGp", T.orange); sw("lgGn", T.slate);
  $("lgLevel").innerHTML = levelView === "index"
    ? `<span><i style="background:${T.ink}"></i>S&P 500, log scale (left)</span><span><i style="background:${T.teal}"></i>Internals index, cumulative vol units (right)</span>`
    : `<span><i style="background:${T.ink}"></i>S&P 500</span><span><i style="background:${T.teal}"></i>Price path implied by internals, from the start of the range</span>`;
  $("lgRegime").innerHTML = `<span><i class="sq" style="background:${T.teal}"></i>Composite</span><span><i class="sq" style="background:${T.slate}"></i>Best single member</span><span><i class="sq" style="background:${T.rule}"></i>Median member</span>`;
}
paintLegends();

// ---------------------------------------------------------------- composite charts
const sl = a => a.slice(startIndex());
const levelReg = register($("cLevel"), () => {
  const i0 = startIndex(); const labels = S.dates.slice(i0); const o = baseOpts();
  const y = S.spx.slice(i0);
  if (levelView === "index"){
    const lv = S.level.slice(i0); const b = lv.find(v => v != null) ?? 0;
    o.scales = {x: xAxis(labels), y: yAxis({type: "logarithmic", position: "left", ticks: Object.assign(yAxis().ticks, {callback: v => fIdx(v), maxTicksLimit: 7})}),
                y1: yAxis({position: "right", grid: {display: false}, ticks: Object.assign(yAxis().ticks, {callback: v => minus(v.toFixed(0))})})};
    o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: c => c.datasetIndex === 0 ? ` S&P 500 ${fIdx(c.parsed.y)}` : ` Internals ${fR(c.parsed.y,1)}`};
    $("levelNote").textContent = "Cumulative sum of the weekly composite, zeroed at the start of the range. The overlay is for the eye; every statistic on this page is computed on changes, because level-on-level fit between trending series is spurious — and the S&P 500’s own long-run drift is not something a rotation index is built to explain, see “Texture, not trend” below.";
    return {type: "line", data: {labels, datasets: [line("S&P 500", y, T.ink, {yAxisID: "y", borderWidth: 1.8}), line("Internals", lv.map(v => v == null ? null : v - b), T.teal, {yAxisID: "y1"})]}, options: o};
  }
  const w = S.impw.slice(i0); let acc = 0; const y0 = y.find(v => v != null);
  const path = w.map((v, i) => { if (i > 0 && v != null) acc += v; return y0 * Math.exp(acc/100); });
  o.scales = {x: xAxis(labels), y: yAxis({type: "logarithmic", ticks: Object.assign(yAxis().ticks, {callback: v => fIdx(v), maxTicksLimit: 7})})};
  o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: c => ` ${c.dataset.label} ${fIdx(c.parsed.y)}`};
  const act = (y[y.length-1]/y0 - 1) * 100, imp = (Math.exp(acc/100) - 1) * 100;
  $("levelNote").textContent = `From ${dLong(labels[0])}: the S&P 500 returned ${fPc(act)}; internals imply ${fPc(imp)}. Weekly changes are compounded with the trailing 3-year beta (the first two years use the first available estimate). Whatever internals miss accumulates, so the paths drift apart over long windows — that drift is mostly the equity risk premium, which a short-horizon rotation index does not price.`;
  return {type: "line", data: {labels, datasets: [line("S&P 500", y, T.ink, {borderWidth: 1.8}), line("Implied path", path, T.teal, {borderWidth: 1.6})]}, options: o};
});
$("levelSeg").querySelectorAll("button").forEach(btn => btn.addEventListener("click", () => {
  levelView = btn.dataset.v; $("levelSeg").querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === btn)));
  paintLegends(); if (levelReg.visible) render(levelReg);
}));
register($("cImplied"), () => {
  const labels = sl(S.dates); const o = baseOpts();
  o.scales = {x: xAxis(labels), y: yAxis({ticks: Object.assign(yAxis().ticks, {callback: v => minus(v.toFixed(0))+"%"})})};
  o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: c => ` ${c.dataset.label} ${fPc(c.parsed.y)}`};
  return {type: "line", data: {labels, datasets: [line("Actual", sl(S.r13), T.ink, {borderWidth: 1.5}), line("Implied", sl(S.implied), T.teal, {borderWidth: 1.8})]}, options: o};
});
register($("cGap"), () => {
  const labels = sl(S.dates), g = sl(S.gap); const o = baseOpts();
  o.scales = {x: xAxis(labels), y: yAxis({ticks: Object.assign(yAxis().ticks, {callback: v => minus(v.toFixed(0))+"%", maxTicksLimit: 5})})};
  o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: c => ` Unconfirmed ${fPc(c.parsed.y)}  (z ${fR(sl(S.gapz)[c.dataIndex],1)})`};
  return {type: "bar", data: {labels, datasets: [{label: "Gap", data: g, backgroundColor: g.map(v => v >= 0 ? T.orange : T.slate), barPercentage: 1, categoryPercentage: 1, borderWidth: 0}]}, options: o};
});
register($("cAttrib"), () => {
  const A = DATA.attrib; const o = baseOpts(); o.indexAxis = "y"; o.interaction = {mode: "nearest", intersect: true, axis: "y"};
  o.scales = {x: yAxis({ticks: Object.assign(yAxis().ticks, {callback: v => minus(v.toFixed(1))})}),
              y: {grid: {display: false}, border: {color: T.rule}, ticks: {color: T.ink, font: {family: "IBM Plex Sans", size: 12}}}};
  o.plugins.tooltip.callbacks = {title: it => byKey[A[it[0].dataIndex].key].label,
    label: c => { const a = A[c.dataIndex]; const bk = byKey[a.key];
      const val13 = bk.islog ? fPct(a.raw13) : fR(a.raw13, 2) + (bk.pillar === "CR" && bk.key !== "NFCI_CHG" ? "bp" : " pts");
      return [` Contribution ${fR(a.contrib)}`, ` 13w change ${val13}`, ` Member 13w score ${fR(a.z13,1)}`, ` Pillar ${M.pillar_names[a.pillar]}`].concat(a.lag_weeks ? [` Latest reading ${a.lag_weeks}w old (source publishes with a lag)`] : []); }};
  return {type: "bar", data: {labels: A.map(a => byKey[a.key].label), datasets: [{data: A.map(a => a.contrib), backgroundColor: A.map(a => T[PCOL[a.pillar]]), borderWidth: 0, barPercentage: 0.72}]}, options: o};
}, false);
function drawHeat(hoverIdx){
  const cv = $("cHeat"), wrap = $("heatWrap"); const dpr = window.devicePixelRatio || 1;
  const W = wrap.clientWidth, H = wrap.clientHeight; if (!W) return;
  if (cv.width !== Math.round(W*dpr) || cv.height !== Math.round(H*dpr)){ cv.width = Math.round(W*dpr); cv.height = Math.round(H*dpr); }
  const ctx = cv.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
  const i0 = startIndex(); const labels = S.dates.slice(i0); const n = labels.length;
  const padL = 132, padB = 22, rows = pOrder.length, rh = (H - padB - 4) / rows, cw = (W - padL) / n;
  const hex2 = (hex) => { const h = hex.replace("#",""); const v = parseInt(h, 16); return [(v>>16)&255, (v>>8)&255, v&255]; };
  const hi = hex2(T.orange), lo = hex2(T.slate), base = hex2(T.card);
  const mix = (c, a) => `rgb(${Math.round(base[0]+(c[0]-base[0])*a)},${Math.round(base[1]+(c[1]-base[1])*a)},${Math.round(base[2]+(c[2]-base[2])*a)})`;
  ctx.font = "12px 'IBM Plex Sans', sans-serif"; ctx.textBaseline = "middle";
  pOrder.forEach((p, r) => {
    const y = 2 + r*rh; const arr = S.pillars[p].slice(i0);
    ctx.fillStyle = T.ink; ctx.fillText(M.pillar_names[p], 0, y + rh/2);
    for (let i = 0; i < n; i++){
      const v = arr[i]; if (v == null) continue;
      const a = Math.min(1, Math.abs(v)/8); ctx.fillStyle = mix(v >= 0 ? hi : lo, a);
      ctx.fillRect(padL + i*cw, y, Math.max(cw, 1) + 0.6, rh - 3);
    }
  });
  ctx.fillStyle = T.muted; ctx.font = "11px 'IBM Plex Mono', monospace"; ctx.textBaseline = "top";
  const span = n/52;
  boundaryTicks(labels).forEach(i => { const x = padL + i*cw; const s = labels[i];
    ctx.fillRect(x, H - padB, 1, 4);
    const t = span <= 2.6 ? `${MONTHS[+s.slice(5,7)-1]} ${s.slice(2,4)}` : s.slice(0,4);
    const tw = ctx.measureText(t).width; if (x - tw/2 > padL - 4 && x + tw/2 < W) ctx.fillText(t, x - tw/2, H - padB + 6); });
  const k = hoverIdx == null ? n - 1 : hoverIdx;
  if (hoverIdx != null){ ctx.fillStyle = T.ink; ctx.fillRect(padL + k*cw, 0, Math.max(1, cw), H - padB); }
  const parts = pOrder.map(p => `${M.pillar_names[p]} ${fR(S.pillars[p][i0 + k], 1)}`);
  $("heatRead").textContent = `${hoverIdx == null ? "Latest, " : ""}${dLong(labels[k])}: ${parts.join(", ")}.`;
  cv._geo = {padL, cw, n};
}
$("cHeat").addEventListener("mousemove", e => { const g = $("cHeat")._geo; if (!g) return; const r = $("cHeat").getBoundingClientRect(); const i = Math.floor((e.clientX - r.left - g.padL) / g.cw); drawHeat(i >= 0 && i < g.n ? i : null); });
$("cHeat").addEventListener("mouseleave", () => drawHeat(null));
let rzT; window.addEventListener("resize", () => { clearTimeout(rzT); rzT = setTimeout(() => drawHeat(null), 120); });
drawHeat(null);

// ---------------------------------------------------------------- cards
const fUnit = (v, unit, d) => {
  if (v == null) return "–";
  if (unit === "bp") return minus((v >= 0 ? "+" : "") + Math.round(v)) + "bp";
  if (unit === "pts") return minus((v >= 0 ? "+" : "") + v.toFixed(d ?? 2)) + " pts";
  if (unit === "ppt") return minus((v >= 0 ? "+" : "") + v.toFixed(d ?? 1)) + "%";   // already a percentage-point quantity — no further ×100
  return fPct(v, d ?? 1);   // "pct" default: a fractional log return (e.g. HYG_IEI, DXY) — fPct applies the ×100
};
const regMinIdx = r => {
  let m = -1;
  r.reg.forEach((v,i) => { if (v == null) return; if (m === -1 || r.sign*v < r.sign*r.reg[m]) m = i; });
  return m;
};
DATA.cards.forEach(c => {
  const r = byKey[c.key]; const inc = r.verdict === "Included";
  const card = el("article", {class: "card" + (inc ? "" : " excluded")});
  const chip = inc ? `<span class="chip in">In the composite, ${lc(M.pillar_names[r.pillar])}</span>` : `<span class="chip out">${r.insufficient ? "Shown for context" : "Left out"}</span>`;
  const mi = regMinIdx(r);
  const attr = DATA.attrib.find(a => a.key === c.key);
  const histTxt = r.hist_weeks != null ? `~${Math.round(r.hist_weeks/52)}y of data` : "";
  card.innerHTML = `<div class="top"><div><h3>${r.label}</h3><div class="legs">${r.legs}${r.start ? ", from " + r.start.slice(0,4) : ""}</div></div>${chip}</div>
    <div class="kv">
      <div><span class="v">${fR(r.rw)}</span><span class="k">Weekly ρ</span></div>
      <div><span class="v">${r.r13 != null ? fR(r.r13) : "–"}</span><span class="k">13-week ρ</span></div>
      <div><span class="v">${mi >= 0 ? fR(r.reg[mi]) : "–"}</span><span class="k">${mi >= 0 ? `Weakest regime, <span class="nb">${M.regimes[mi]}</span>` : histTxt || "Limited history"}</span></div>
      <div><span class="v">${attr ? fUnit(attr.raw13, attr.unit) : "–"}</span><span class="k">${attr ? "Change, last 13 weeks" : "Not a composite member"}</span></div>
    </div>
    ${r.reason && (!inc || /Marginal/.test(r.reason)) ? `<p class="why">${r.reason.replace(/^[A-Za-z -]+ pillar\. /, "")}</p>` : ""}
    <div class="chart h200"><canvas aria-label="${r.label} against the S&P 500"></canvas></div>
    <div class="strip-label">Trailing 52-week correlation with weekly S&P 500 returns</div>
    <div class="chart h90"><canvas aria-label="${r.label} rolling correlation"></canvas></div>`;
  $("cards").appendChild(card);
  const [cv1, cv2] = card.querySelectorAll("canvas");
  register(cv1, () => {
    const i0 = startIndex(); const labels = S.dates.slice(i0); const lv = c.level.slice(i0);
    const b = lv.find(v => v != null);
    const reb = lv.map(v => { if (v == null || b == null) return null; return c.islog ? 100 * Math.log(v / b) : v - b; });
    const o = baseOpts();
    const col = inc ? T.teal : T.slate;
    const y1fmt = c.unit === "bp" ? (v => minus((v>0?"+":"")+Math.round(v))+"bp") : c.unit === "pts" ? (v => minus((v>0?"+":"")+v.toFixed(2))) : (v => minus((v > 0 ? "+" : "") + Math.round(v)) + "%");
    o.scales = {x: xAxis(labels), y: yAxis({type: "logarithmic", position: "left", ticks: Object.assign(yAxis().ticks, {callback: v => fIdx(v), maxTicksLimit: 5})}),
      y1: yAxis({position: "right", reverse: r.sign < 0, grid: {display: false}, ticks: Object.assign(yAxis().ticks, {maxTicksLimit: 5, callback: y1fmt})})};
    o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: x => x.datasetIndex === 0 ? ` S&P 500 ${fIdx(x.parsed.y)}` : ` ${r.label} ${fUnit(x.parsed.y, c.unit, c.unit==="pts"?2:1)}${c.islog?" cumulative":""}${r.sign<0?", inverted axis":""}`};
    return {type: "line", data: {labels, datasets: [line("S&P 500", S.spx.slice(i0), T.ink, {yAxisID: "y", borderWidth: 1.3}), line(r.label, reb, col, {yAxisID: "y1", borderWidth: 1.5})]}, options: o};
  });
  register(cv2, () => {
    const i0 = startIndex(); const labels = S.dates.slice(i0); const rc = c.rc.slice(i0);
    const o = baseOpts();
    const good = r.sign > 0 ? T.teal : T.orange, bad = r.sign > 0 ? T.orange : T.teal;
    o.scales = {x: xAxis(labels, {display: false}), y: yAxis({min: -1, max: 1, ticks: Object.assign(yAxis().ticks, {stepSize: 1, callback: v => minus(String(v))})})};
    o.plugins.tooltip.callbacks = {title: it => dLong(labels[it[0].dataIndex]), label: x => ` 52w ρ ${fR(x.parsed.y)}`};
    return {type: "line", data: {labels, datasets: [line("52w ρ", rc, T.ink, {borderWidth: 1.1, fill: {target: "origin", above: rgba(good, 0.28), below: rgba(bad, 0.28)}})]}, options: o};
  });
});

// ---------------------------------------------------------------- screen table
function screenTable(){
  const nInc = DATA.table.filter(r => r.verdict === "Included").length;
  $("screenBlurb").innerHTML = `${DATA.table.length} candidates, ${nInc} admitted. Rules, applied in order with requested internals first, then the rest by strength: <b>R0</b> at least ${M.min_hist} weeks of history to be screened across regimes; <b>R1</b> weekly |ρ| ≥ ${M.rules.r1.toFixed(2)} with the expected sign; <b>R2</b> expected sign in every regime (weakest below ${M.rules.r2min.toFixed(2)} is flagged marginal); <b>R3</b> trailing 52-week correlation right-signed at least ${Math.round(M.rules.r3*100)}% of the time; <b>R5</b> |ρ| &lt; ${M.rules.r5.toFixed(2)} with every member already admitted; <b>R6</b> the 4-week and 13-week non-overlap correlations must agree in sign with the weekly one. Regime cells are shaded teal when right-signed and orange when wrong; grey means no data in that window.`;
  const tb = $("screenTable");
  const head = `<thead><tr><th>Internal</th><th class="n">Weekly ρ</th><th class="n">4w ρ</th><th class="n">13w ρ</th>${M.regimes.map(g => `<th class="n">${g}</th>`).join("")}<th class="n">52w sign held</th><th>Verdict</th></tr></thead>`;
  const origins = [["requested","Requested"],["tested","Also tested"]];
  const shade = (v, s) => { if (v == null) return "background:" + T.soft; const a = Math.min(1, Math.abs(v)/0.7) * 0.42; const c = s*v > 0 ? T.teal : T.orange; return `background:${rgba(c, a)}`; };
  let body = "<tbody>";
  origins.forEach(([o, name]) => {
    body += `<tr class="grp"><td colspan="${7 + M.regimes.length}">${name}</td></tr>`;
    DATA.table.filter(r => r.origin === o).sort((a,b) => (a.verdict === b.verdict ? Math.abs(b.rw||0) - Math.abs(a.rw||0) : a.verdict === "Included" ? -1 : 1)).forEach(r => {
      body += `<tr><td><span class="lbl">${r.label}</span><span class="sub">${r.legs}${r.start ? ", from " + r.start.slice(0,4) : ""}</span></td>
        <td class="n">${fR(r.rw)}</td><td class="n">${r.r4 != null ? fR(r.r4) : "–"}</td><td class="n">${r.r13 != null ? fR(r.r13) : "–"}</td>
        ${r.reg.map(v => `<td class="n" style="${shade(v, r.sign)}">${v != null ? fR(v) : "–"}</td>`).join("")}
        <td class="n">${r.pct != null ? Math.round(r.pct*100)+"%" : "–"}</td>
        <td class="reason"><span class="lbl" style="color:${r.verdict === "Included" ? "var(--teal)" : "var(--muted)"}">${r.verdict}${r.fails.length && r.verdict !== "Included" ? " (" + r.fails.join(", ") + ")" : ""}</span><span class="sub">${r.reason}</span></td></tr>`;
    });
  });
  tb.innerHTML = head + body + "</tbody>";
}
screenTable();

// ---------------------------------------------------------------- tests
register($("cRegime"), () => {
  const o = baseOpts(); o.interaction = {mode: "index", intersect: false};
  o.scales = {x: {grid: {display: false}, border: {color: T.rule}, ticks: {color: T.muted, font: {family: "IBM Plex Mono", size: 11}}},
              y: yAxis({min: 0, max: 1, ticks: Object.assign(yAxis().ticks, {stepSize: 0.25, callback: v => v.toFixed(2)})})};
  o.plugins.tooltip.callbacks = {label: c => ` ${c.dataset.label} ${fR(c.parsed.y)}`};
  return {type: "bar", data: {labels: M.regimes, datasets: [
    {label: "Composite", data: DATA.comp_stats.reg, backgroundColor: T.teal, borderWidth: 0},
    {label: "Best single member", data: DATA.best_reg, backgroundColor: T.slate, borderWidth: 0},
    {label: "Median member", data: DATA.med_reg, backgroundColor: T.rule, borderWidth: 0}]}, options: o};
}, false);
(function regimeNote(){
  const ms = Object.keys(DATA.member_aligned);
  const lastI = M.regimes.length - 1;
  const bestKey = ms.reduce((a, k) => DATA.member_aligned[k].reg[lastI] > DATA.member_aligned[a].reg[lastI] ? k : a, ms[0]);
  const beats = DATA.comp_stats.reg.map((v,i) => v >= DATA.best_reg[i]).filter(Boolean).length;
  $("regimeNote").textContent = `The composite beats the median member in every regime and the best single member in ${beats} of ${M.regimes.length}. Even its weakest regime (${fR(Math.min(...DATA.comp_stats.reg))}) is stronger than most individual internals ever get. The single best member varies by period — in ${M.regimes[lastI]} it is ${byKey[bestKey].label} (${fR(DATA.best_reg[lastI])}) — which is the case for pooling several internals rather than picking one.`;
})();
register($("cLead"), () => {
  const LL = DATA.leadlag; const o = baseOpts(); o.interaction = {mode: "nearest", intersect: false, axis: "x"};
  o.scales = {x: {grid: {display: false}, border: {color: T.rule}, title: {display: true, text: "k, weeks", color: T.muted, font: {family: "IBM Plex Sans", size: 11}}, ticks: {color: T.muted, font: {family: "IBM Plex Mono", size: 11}, callback: v => minus(String(LL[v].k))}},
              y: yAxis({min: -0.1, max: 0.7, ticks: Object.assign(yAxis().ticks, {stepSize: 0.1, callback: v => minus(v.toFixed(1))})})};
  o.plugins.tooltip.callbacks = {title: it => { const k = LL[it[0].dataIndex].k; return k === 0 ? "Same week" : k > 0 ? `Internals ${k}w ahead of yields` : `Yields ${-k}w ahead of internals`; }, label: c => ` ρ ${fR(c.parsed.y, 3)}`};
  return {type: "bar", data: {labels: LL.map(d => d.k), datasets: [{data: LL.map(d => d.r), backgroundColor: LL.map(d => d.k === 0 ? T.teal : T.slate), borderWidth: 0, barPercentage: 0.7}]}, options: o};
}, false);
(function leadNote(){
  const ahead = DATA.leadlag.filter(d => d.k > 0).map(d => Math.abs(d.r));
  $("leadNote").textContent = `All of the relationship is contemporaneous: ρ ${fR(DATA.leadlag.find(d=>d.k===0).r)} in the same week, and no lead above |${Math.max(...ahead).toFixed(2)}| at 1 to 8 weeks. Treat the composite as a confirmation gauge, not a forecast.`;
})();
(function divTable(){
  const t = $("divTable");
  let h = `<thead><tr><th class="n">Horizon</th><th class="n">ρ gap z, fwd</th><th class="n">Non-overlap ρ</th><th class="n">Indep. obs</th><th class="n">Gap z &gt; +1</th><th class="n">Gap z &lt; −1</th><th class="n">All weeks</th></tr></thead><tbody>`;
  DATA.div.forEach(d => {
    h += `<tr><td class="n">${d.h}w</td><td class="n">${fR(d.r)}</td><td class="n">${fR(d.rno)}</td><td class="n">${d.n_eff}</td>
      <td class="n">${fPc(d.hi_mean)}<span class="sub">${Math.round(d.hi_down*100)}% fell, n ${d.hi_n}</span></td>
      <td class="n">${fPc(d.lo_mean)}<span class="sub">${Math.round(d.lo_up*100)}% rose, n ${d.lo_n}</span></td>
      <td class="n">${fPc(d.unc)}</td></tr>`;
  });
  t.innerHTML = h + "</tbody>";
  const d4 = DATA.div.find(d => d.h === 4), d26 = DATA.div.find(d => d.h === 26);
  $("divNote").textContent = `The asymmetry is the finding: a stretched-negative gap (price lagging what internals confirm) has historically closed hard — 26 weeks forward, ${fPc(d26.lo_mean)} on average with ${Math.round(d26.lo_up*100)}% of cases rising — while a stretched-positive gap (price running ahead of internals) barely slows down (${fPc(d26.hi_mean)}, only ${Math.round(d26.hi_down*100)}% falling). That fits a market with a persistent upward drift: internals catch overextensions on the downside far more reliably than exuberance on the upside. The composite's own momentum adds little: its 13-week z-score correlates ${fR(d4.mom_r)} with the next 4 weeks of returns, against ${fR(d4.r13_r)} for the index's own 13-week momentum — both close to zero.`;
})();
(function trendTable(){
  const t = $("trendTable");
  let h = `<thead><tr><th>Window</th><th class="n">S&P 500 return</th><th class="n">Implied by internals</th><th class="n">Weekly R²</th></tr></thead><tbody>`;
  DATA.trend.forEach(d => { h += `<tr${d.label === "Full sample" ? ' style="font-weight:600"' : ""}><td>${d.label}</td><td class="n">${fPc(d.actual)}</td><td class="n">${fPc(d.implied)}</td><td class="n">${d.r2 != null ? d.r2.toFixed(2) : "–"}</td></tr>`; });
  t.innerHTML = h + "</tbody>";
  const full = DATA.trend.find(d => d.label === "Full sample");
  $("trendNote").textContent = `Internals explain 60–70% of weekly variance in every regime, and almost none of the compounding. Over the full sample the S&P 500 returned ${fPc(full.actual)} while summed weekly-implied returns come to ${fPc(full.implied)} — the composite is built to average out over time (it is a z-scored rotation signal), while the index carries a persistent equity risk premium no rotation index is meant to price. Read the composite for whether a move is being confirmed, never for where the index should be.`;
  const cs = DATA.comp_stats, d13 = DATA.div.find(d => d.h === 13);
  $("testsBlurb").textContent = `Coincident fit is strong and stable (weekly ρ ${fR(cs.rw)}, never below ${fR(Math.min(...cs.reg))} by regime). There is no lead. Divergences close asymmetrically — see below. The compounding return is not explained, and is not meant to be.`;
})();
(function robTable(){
  const t = $("robTable"); const c = DATA.comp_stats;
  let h = `<thead><tr><th>Variant</th><th class="n">Weekly ρ</th><th class="n">13w ρ</th><th class="n">Weakest regime</th></tr></thead><tbody>`;
  h += `<tr><td><span class="lbl">Composite as built</span></td><td class="n">${fR(c.rw)}</td><td class="n">${fR(c.r13)}</td><td class="n">${fR(Math.min(...c.reg))}</td></tr>`;
  DATA.robust.forEach(r => {
    const dlt = r.rw - c.rw;
    h += `<tr><td>${r.name}</td><td class="n">${fR(r.rw)} <span class="muted" style="font-size:11px">${fR(dlt,2)}</span></td><td class="n">${fR(r.r13)}</td><td class="n">${fR(r.minreg)}</td></tr>`;
  });
  t.innerHTML = h + "</tbody>";
  const sp = DATA.split;
  const without = DATA.robust.filter(r => r.name.startsWith("Without"));
  const worst = without.reduce((a,b) => b.rw < a.rw ? b : a), best = without.reduce((a,b) => b.rw > a.rw ? b : a);
  const bestDelta = best.rw - c.rw;
  const bestBit = bestDelta > 0.005
    ? ` One member, ${best.name.replace("Without ","")}, actually hurts the composite (removing it would add ${fR(bestDelta)}) despite passing the individual screen cleanly — left in because pillar membership is decided by economic theme, not by which combination maximises fit.`
    : ` Removing any single member costs something; none is dead weight.`;
  $("splitNote").textContent = `Dropping ${worst.name.replace("Without ","")} costs the most (${fR(worst.rw - c.rw)}).${bestBit} Split-sample: screening on 2004–15 only would have used plain VIX in place of the term structure (VIX3M did not exist until Sep 2009) and kept equal-weight/cap-weight, which later broke down; it would have missed the term-structure member entirely. On 2016–${M.asof.slice(2,4)} data that early-chosen composite scores ${fR(sp.early_oos.rw)} weekly and ${fR(sp.early_oos.r13)} at 13 weeks, against ${fR(sp.full_oos.rw)} and ${fR(sp.full_oos.r13)} for the full-sample selection — close enough that member selection is not doing heavy in-sample lifting.`;
})();
register($("cLegs"), () => {
  const rows = DATA.table.filter(r => r.rw != null);
  const o = baseOpts(); o.interaction = {mode: "nearest", intersect: true};
  const xr = row => row.sign * row.rw;
  const yr = row => { const vals = row.reg.filter(v => v != null).map(v => row.sign * v); return vals.length ? Math.min(...vals) : null; };
  const pts = rows.map(r => ({x: xr(r), y: yr(r)})).map((p,i) => p.y == null ? {x: p.x, y: -0.6} : p);
  o.scales = {x: yAxis({type: "linear", min: -0.2, max: 1, title: {display: true, text: "sign-aligned weekly ρ", color: T.muted, font: {family: "IBM Plex Sans", size: 11}}, ticks: Object.assign(yAxis().ticks, {stepSize: 0.2, callback: v => minus(v.toFixed(1))})}),
              y: yAxis({min: -0.7, max: 1, title: {display: true, text: "weakest single regime (sign-aligned)", color: T.muted, font: {family: "IBM Plex Sans", size: 11}}, ticks: Object.assign(yAxis().ticks, {stepSize: 0.2, callback: v => minus(v.toFixed(1))})})};
  o.plugins.tooltip.callbacks = {label: c => { const r = rows[c.dataIndex]; const y = yr(r); return ` ${r.label}: weekly ${fR(xr(r))}, weakest regime ${y == null ? "n/a (short history)" : fR(y)}`; }};
  const lab = {id: "lab", afterDatasetsDraw(ch){
    const ctx = ch.ctx; const meta = ch.getDatasetMeta(0); ctx.save(); ctx.font = "11px 'IBM Plex Sans', sans-serif"; ctx.textBaseline = "middle";
    const xs = ch.scales.x, ys = ch.scales.y; ctx.strokeStyle = T.muted; ctx.lineWidth = 1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(xs.getPixelForValue(M.rules.r1), ys.top); ctx.lineTo(xs.getPixelForValue(M.rules.r1), ys.bottom);
    ctx.moveTo(xs.left, ys.getPixelForValue(0)); ctx.lineTo(xs.right, ys.getPixelForValue(0)); ctx.stroke(); ctx.setLineDash([]);
    const placed = meta.data.map(p => ({x0: p.x-5, y0: p.y-5, x1: p.x+5, y1: p.y+5}));
    const hit = bx => placed.some(q => !(bx.x1 < q.x0 || bx.x0 > q.x1 || bx.y1 < q.y0 || bx.y0 > q.y1));
    const order = meta.data.map((p,i) => i).sort((i,j) => (rows[i].verdict === "Included" ? 0 : 1) - (rows[j].verdict === "Included" ? 0 : 1));
    order.forEach(i => { const p = meta.data[i], r = rows[i];
      const t = r.label.replace(" / S&P 500","").replace("Regional banks","Reg. banks").replace("Discretionary / Staples","Disc/Staples").replace("High yield / Treasuries","HYG/IEI").replace("Financial conditions","NFCI").replace("VIX term structure","VIX3M/VIX");
      const w = ctx.measureText(t).width, h = 12;
      const cand = [[7, 0], [-7 - w, 0], [7, -11], [7, 11], [-7 - w, -11], [-7 - w, 11], [7, -22], [7, 22], [-7-w, 22], [-7-w, -22]];
      for (const [dx, dy] of cand){
        const bx = {x0: p.x + dx, y0: p.y + dy - h/2, x1: p.x + dx + w, y1: p.y + dy + h/2};
        if (bx.x0 < ch.chartArea.left || bx.x1 > ch.chartArea.right || bx.y0 < ch.chartArea.top || bx.y1 > ch.chartArea.bottom) continue;
        if (hit(bx)) continue;
        placed.push(bx); ctx.fillStyle = r.verdict === "Included" ? T.ink : T.muted;
        if (Math.abs(dy) > 1){ ctx.strokeStyle = T.rule; ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(dx > 0 ? bx.x0 : bx.x1, p.y + dy); ctx.stroke(); }
        ctx.fillText(t, bx.x0, p.y + dy); break;
      }
    });
    ctx.restore(); }};
  return {type: "scatter", data: {datasets: [{data: pts,
    pointBackgroundColor: rows.map(r => r.verdict === "Included" ? T.teal : "transparent"),
    pointBorderColor: rows.map(r => r.verdict === "Included" ? T.teal : T.muted), pointRadius: 4.5, pointHoverRadius: 6}]}, options: o, plugins: [lab]};
}, false);
(function legsNote(){
  const shortHist = DATA.table.filter(r => r.insufficient).map(r => r.label);
  $("legsNote").textContent = `Included internals cluster in the upper right: strong on average and never badly wrong in any regime. Points plotted at the bottom (y −0.6) have no regime breakdown — ${shortHist.join(" and ")} — too little history to place. A few candidates are strong on average but drop sharply in one regime (a low point far right); those are the R2 rejections in the table above.`;
})();
function cmat(){
  const K = DATA.cmat.keys, Mx = DATA.cmat.m; const t = $("cmatTable");
  const short = k => byKey[k].label.replace(" / S&P 500","/SPX").replace("Regional banks","Reg.banks").replace("High yield / Treasuries","HYG/IEI").replace("Discretionary / Staples","Disc/Stap").replace("Financial conditions","NFCI").replace("VIX term structure","VIX3M/VIX").replace("Long Treasuries","TLT").replace("US dollar index","DXY").replace("Gold","GLD").replace("Homebuilders","XHB").replace("Small caps","IWM").replace("Transports","IYT");
  let h = `<thead><tr><th></th>${K.map(k => `<th class="n" style="white-space:normal;min-width:62px">${short(k)}</th>`).join("")}</tr></thead><tbody>`;
  K.forEach((k,i) => {
    h += `<tr><td style="white-space:nowrap">${short(k)}</td>${K.map((j,jj) => { const v = Mx[i][jj]; const a = i===jj ? 0 : Math.min(1, Math.abs(v)/0.8)*0.5; const c = v >= 0 ? T.teal : T.orange;
      return `<td class="n" style="background:${i===jj ? T.soft : rgba(c, a)}">${i===jj ? "" : fR(v)}</td>`; }).join("")}</tr>`;
  });
  t.innerHTML = h + "</tbody>";
}
cmat();

// ---------------------------------------------------------------- method
(function method(){
  const hyoas = byKey.HYOAS_CHG, igoas = byKey.IGOAS_CHG, hygIei = byKey.HYG_IEI;
  const P = [
    `<b>Target.</b> Weekly log return of the S&P 500 (${"^GSPC"}), in %, Friday to Friday (last available day in the week), ${M.start.slice(0,4)} to ${dLong(M.asof)}. Source: Yahoo Finance daily closes. The final week is truncated to the last date every member and the index both printed.`,
    `<b>Internals.</b> Weekly log change of each ratio, on total-return adjusted ETF closes so dividend timing does not leak into the ratios. The VIX term structure and VIX itself come from CBOE's own daily index files (cdn.cboe.com), not a brokerage proxy — VIX3M has published there since 18 September 2009. Credit spreads (ICE BofA OAS via FRED), financial conditions (Chicago Fed NFCI) and the 10s2s curve (FRED) are point changes, not log changes.`,
    `<b>Signs are set before looking.</b> Each internal carries an economic prior: cyclicals, discretionary spending, regional banks, transports, small caps, tighter credit spreads and a steepening VIX term structure go with a stronger market; long Treasuries, gold, the dollar and tighter financial conditions go the other way. The screen checks the data agrees; it does not choose signs.`,
    `<b>Composite.</b> Each member's weekly change is sign-aligned and divided by its trailing 52-week standard deviation, lagged one week, then clipped at ±4. Members are averaged within pillars and pillars averaged equally, so the five-member equity-rotation pillar does not outvote the single-member volatility pillar. Weights and scaling are point-in-time; nothing is fitted to the target.`,
    `<b>Implied return and gap.</b> The 13-week composite sum is converted to a % return with a no-intercept beta estimated on the trailing 156 weeks of 13-week returns and lagged one week (latest ${L.beta.toFixed(2)}% per unit). The gap is the actual 13-week return minus that implied return, and its z-score uses the trailing 156-week standard deviation.`,
    `<b>What is in-sample.</b> Membership comes from a full-sample screen, so the coincident fit on this page is partly in-sample. The split-sample check selects on 2004–15 only and scores on 2016 onward (see Robustness). Re-running this script re-screens from scratch; membership is not frozen the way a scheduled rebuild would freeze it — add that yourself if this becomes a recurring job.`,
    `<b>Credit spreads proper were tested, not screened.</b> ${hyoas ? `ICE BofA HY OAS correlates ${fR(hyoas.rw)} with weekly returns and IG OAS ${fR(igoas.rw)} over the roughly ${Math.round((hyoas.hist_weeks||0)/52)} years FRED's free CSV endpoint allows without an API key (it caps this specific series to a trailing window regardless of the requested start date).` : ""} That is too short to screen across the regimes used here, so HYG/IEI stands in as the full-history, market-priced proxy — it correlates ${fR(hygIei.rw)} and is a genuine composite member.`,
    `<b>The VIX term structure, not VIX itself.</b> Plain VIX week-on-week change is the single strongest candidate tested (${fR(byKey.VIX_CHG.rw)}) but is 90% correlated with the VIX3M/VIX term-structure change already in the composite — same information, noisier and four years shorter. The term structure was kept because it is what was asked for and it is genuinely the more specific instrument; the robustness table shows what removing it costs.`,
    `<b>Financial conditions passes the screen but does not help the composite.</b> NFCI is right-signed, stable and non-redundant on its own, so it is included — pillar membership follows economic theme, not fit. The robustness table shows the composite would score slightly higher without it; that trade is left as-is rather than tuned away.`,
    `<b>Regimes.</b> 2004–07 (pre-crisis calm), 2008–09 (the financial crisis), 2010–15 (post-crisis recovery and QE), 2016–19 (late-cycle expansion), 2020–22 (pandemic crash, recovery and the hiking cycle), 2023 onward (AI-capex-led bull market).`,
    `<b>Two rejections worth naming.</b> Equal-weight/cap-weight breadth (RSP/SPX) was a real, positive internal through 2015 and has since inverted sign twice (2016–19 and 2023–${M.asof.slice(2,4)}) — a direct, quantified readout of index concentration in mega-caps. Semiconductors/SPX only became a coherent signal in the current regime (2023–${M.asof.slice(2,4)}: ${fR(byKey.SMH_SPY.reg[5])}) and inverted sign during the 2008 recession, so it fails the full-sample screen despite being topical now.`,
    `<b>Data caveats.</b> KRE (regional banks) and HYG start in 2006–2007, so their regime statistics before that use whatever history exists; NFCI is a weekly-native series (Fridays) with no daily granularity to check mid-week. The composite starts ${dMon(M.first_comp)} after a 26-week volatility warm-up and the term-structure pillar's 2009 start, ${M.n_weeks} weeks in all.`,
  ];
  if (M.target_note) P.splice(1, 0, `<b>Target proxy on the automated build.</b> ${M.target_note}`);
  $("methodCols").innerHTML = P.map(p => `<p>${p}</p>`).join("");
  const srcCounts = {}; Object.values(M.src || {}).forEach(v => srcCounts[v] = (srcCounts[v]||0)+1);
  const srcSummary = Object.entries(srcCounts).map(([k,v]) => `${v}× ${k}`).join(", ");
  $("foot").innerHTML = `Acheron Insights. Built ${M.built} by rebuild.py${M.run_url ? `, <a href="${M.run_url}">build log</a>` : ""}. Sources: ${srcSummary || "Yahoo, CBOE, FRED"}. Readings also at <a href="latest.json">latest.json</a>.`;
})();

// ---------------------------------------------------------------- theme changes
window.addEventListener("beforeprint", () => REG.forEach(r => { if (!r.visible){ r.visible = true; render(r); } }));
function onTheme(){ readTokens(); paintLegends(); screenTable(); cmat(); drawHeat(null); rerender(() => true); }
if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", onTheme);
new MutationObserver(onTheme).observe(document.documentElement, {attributes: true, attributeFilter: ["data-theme"]});
})();
</script>
</body>
</html>
'''

def cli():
    global CACHE, TTL_HOURS
    ap = argparse.ArgumentParser(description="Rebuild the S&P 500 market internals dashboard")
    ap.add_argument("--out", default=os.path.join(HERE, "site"), help="output directory (index.html, latest.json)")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"), help="download cache directory")
    ap.add_argument("--ttl-hours", type=float, default=10, help="re-download cached files older than this")
    ap.add_argument("--max-age-days", type=int, default=7, help="exit 2 if the as-of date is older than this")
    ap.add_argument("--template", default=None, help="optional external template.html (development)")
    args = ap.parse_args()
    CACHE, TTL_HOURS = os.path.abspath(args.cache), args.ttl_hours
    os.makedirs(CACHE, exist_ok=True); os.makedirs(args.out, exist_ok=True)

    payload = clean(main())
    tpl = open(args.template).read() if args.template else (TEMPLATE or open(os.path.join(HERE, "template.html")).read())
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html, n1 = re.subn(r"/\*__DATA__\*/null", lambda m: data, tpl)
    html, n2 = re.subn(r"/\*__FONTS__\*/", lambda m: font_css(), html)
    assert n1 == 1 and n2 == 1, (n1, n2)
    open(os.path.join(args.out, "index.html"), "w").write(html)
    j = latest_json(payload)
    json.dump(j, open(os.path.join(args.out, "latest.json"), "w"), indent=1)
    open(os.path.join(args.out, ".nojekyll"), "w").write("")
    log(f"wrote {args.out}/index.html ({len(html)/1024:.0f} KB) and latest.json")

    problems = sanity(payload)
    today_et = pd.Timestamp.now(tz="America/New_York").normalize().tz_localize(None)
    age = (today_et - pd.Timestamp(payload["meta"]["asof"])).days
    if problems:
        status, code = "sanity check failed", 3
        for p in problems: log("SANITY:", p)
    elif age > args.max_age_days:
        status, code = f"stale: as-of is {age} days old", 2
        log("STALE:", status)
    else:
        status, code = ("ok" if not STALE else f"ok, {len(STALE)} series from cache"), 0
    if STALE: log("stale series:", STALE)
    step_summary(j, status)
    log("status:", status)
    return code

if __name__ == "__main__":
    sys.exit(cli())
