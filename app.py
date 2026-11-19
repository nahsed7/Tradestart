"""Live market chart with paper trading: real prices, demo balance.

Data sources (public, no API key):
  * Crypto - Binance market-data API, real-time 1-second candles
  * Forex / gold / stocks / indices - Yahoo Finance chart API (quotes refresh every few
    seconds; some exchanges are delayed)

Run:  streamlit run app.py
"""
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit.components.v1 as components
import requests

from eoapi import REGIONS, ExpertOptionClient
import streamlit as st

st.set_page_config(page_title="Live Market Chart", page_icon="📈", layout="wide")

# Binance blocks some cloud regions (e.g. US servers); Binance.US is the fallback.
BINANCE_HOSTS = ["https://data-api.binance.vision/api/v3", "https://api.binance.us/api/v3"]
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
ACCOUNT_FILE = Path(__file__).with_name("paper_account.json")
DEFAULT_BALANCE = 50_000.0

ASSETS = {
    "BTC/USDT": dict(src="binance", sym="BTCUSDT", dec=2, payout=0.90),
    "ETH/USDT": dict(src="binance", sym="ETHUSDT", dec=2, payout=0.90),
    "SOL/USDT": dict(src="binance", sym="SOLUSDT", dec=2, payout=0.88),
    "BNB/USDT": dict(src="binance", sym="BNBUSDT", dec=2, payout=0.88),
    "XRP/USDT": dict(src="binance", sym="XRPUSDT", dec=4, payout=0.86),
    "EUR/USD": dict(src="yahoo", sym="EURUSD=X", dec=5, payout=0.88),
    "GBP/USD": dict(src="yahoo", sym="GBPUSD=X", dec=5, payout=0.86),
    "USD/JPY": dict(src="yahoo", sym="JPY=X", dec=3, payout=0.86),
    "Gold": dict(src="yahoo", sym="GC=F", dec=2, payout=0.84),
    "Apple": dict(src="yahoo", sym="AAPL", dec=2, payout=0.82),
    "Tesla": dict(src="yahoo", sym="TSLA", dec=2, payout=0.82),
    "S&P 500": dict(src="yahoo", sym="^GSPC", dec=2, payout=0.80),
}
# label: (source interval, candle seconds)
TIMEFRAMES = {
    "binance": {"1s": ("1s", 1), "5s": ("1s", 5), "15s": ("1s", 15), "30s": ("1s", 30),
                "1m": ("1m", 60), "5m": ("5m", 300)},
    "yahoo": {"1m": ("1m", 60), "5m": ("5m", 300), "15m": ("15m", 900)},
    "eo": {"5s": (5, 5), "15s": (5, 15), "30s": (5, 30), "1m": (5, 60)},
}
EO_PAYOUT = 0.85

# Expert Option assets the user added this session (see sidebar)
ASSETS.update(st.session_state.get("eo_assets", {}))
YAHOO_RANGE = {"1m": "2d", "5m": "5d", "15m": "1mo"}
EXPIRIES = {"30 sec": 30, "1 min": 60, "2 min": 120, "5 min": 300, "15 min": 900}
VISIBLE = 90                 # candles on screen
STALE_AFTER = 10 * 60        # Yahoo quote older than this → market closed

UP, DOWN, BG, GRID, LINE = "#1fc77e", "#ff4d5a", "#141824", "rgba(255,255,255,0.06)", "#3d8bff"

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "Mozilla/5.0"

st.markdown(f"""
<style>
  .stApp {{ background:{BG}; color:#e6e9f0; }}
  header[data-testid="stHeader"] {{ background:transparent; }}
  /* no fade/blink while the page refreshes every second */
  [data-stale="true"], .stale-element, [data-testid="stElementContainer"],
  [data-testid="element-container"] {{ opacity:1 !important; transition:none !important; }}
  [data-testid="stStatusWidget"] {{ visibility:hidden; }}
  .block-container {{ padding-top:1.2rem; padding-bottom:1rem; max-width:1500px; }}
  .st-key-up_btn button, .st-key-down_btn button {{ border:none; color:#fff; font-weight:700;
      font-size:1.15rem; height:3.4rem; }}
  .st-key-up_btn button {{ background:{UP}; }}
  .st-key-down_btn button {{ background:{DOWN}; }}
  .st-key-up_btn button:hover, .st-key-down_btn button:hover {{ filter:brightness(1.12); color:#fff; }}
  .st-key-up_btn button:disabled, .st-key-down_btn button:disabled {{ opacity:.35; }}
  .bal {{ font-size:1.5rem; font-weight:700; line-height:1.2; }}
  .lbl {{ font-size:.72rem; color:#8a93a6; text-transform:uppercase; letter-spacing:.05em; }}
  .pnl {{ font-size:1.3rem; font-weight:700; color:{UP}; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:.75rem; }}
</style>
""", unsafe_allow_html=True)


# ================================================================ market data
def binance_get(path, params):
    """GET from the first Binance host that isn't geo-blocked; remembers the one that works."""
    hosts = BINANCE_HOSTS[:]
    good = st.session_state.get("binance_host")
    if good in hosts:
        hosts.remove(good)
        hosts.insert(0, good)
    last = None
    for host in hosts:
        try:
            r = HTTP.get(f"{host}/{path}", params=params, timeout=5)
            if r.status_code in (403, 451):          # region blocked → try next host
                last = requests.HTTPError(f"{r.status_code} from {host}")
                continue
            r.raise_for_status()
            st.session_state["binance_host"] = host
            return r
        except requests.RequestException as e:
            last = e
    raise last or RuntimeError("Binance unavailable")


@st.cache_resource(show_spinner=False)
def eo_client(token, region):
    """One shared Expert Option connection per token+region."""
    return ExpertOptionClient(token, region=region, demo=True).connect(timeout=20)


def eo_conn():
    c = st.session_state.get("eo")
    if c is None:
        raise RuntimeError("Expert Option is not connected (see sidebar)")
    return c


@st.cache_data(ttl=1, show_spinner=False)
def binance_klines(sym, interval, total):
    rows, end = [], None
    while len(rows) < total:
        params = {"symbol": sym, "interval": interval, "limit": min(1000, total - len(rows))}
        if end is not None:
            params["endTime"] = end
        r = binance_get("klines", params)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows = batch + rows
        end = batch[0][0] - 1
        if len(batch) < params["limit"]:
            break
    df = pd.DataFrame([k[:6] for k in rows],
                      columns=["t", "open", "high", "low", "close", "volume"]).astype(float)
    df.index = pd.to_datetime(df.pop("t"), unit="ms", utc=True)
    return df


def binance_price(sym):
    r = binance_get("ticker/price", {"symbol": sym})
    r.raise_for_status()
    return float(r.json()["price"]), time.time()


@st.cache_data(ttl=1, show_spinner=False)
def binance_price_cached(sym):
    return binance_price(sym)


def binance_price_at(sym, ts):
    """Close of the 1-second candle that ends exactly at `ts`."""
    start = (int(ts) - 1) * 1000
    r = binance_get("klines", {"symbol": sym, "interval": "1s", "startTime": start, "limit": 1})
    r.raise_for_status()
    k = r.json()
    return float(k[0][4]) if k and int(k[0][0]) == start else None


@st.cache_data(ttl=3, show_spinner=False)
def yahoo_chart(sym, interval):
    r = HTTP.get(YAHOO + quote(sym, safe=""),
                 params={"interval": interval, "range": YAHOO_RANGE[interval]}, timeout=6)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({k: q.get(k) for k in ["open", "high", "low", "close", "volume"]},
                      index=pd.to_datetime(res.get("timestamp", []), unit="s", utc=True))
    df = df.dropna(subset=["close"])
    meta = res["meta"]
    return df, float(meta["regularMarketPrice"]), float(meta.get("regularMarketTime", 0))


def get_candles(asset, tf):
    a = ASSETS[asset]
    interval, step = TIMEFRAMES[a["src"]][tf]
    if a["src"] == "binance":
        total = min(3000, VISIBLE * step) if interval == "1s" else VISIBLE + 5
        df = binance_klines(a["sym"], interval, total)
        if interval == "1s" and step > 1:
            df = df.resample(f"{step}s").agg({"open": "first", "high": "max", "low": "min",
                                              "close": "last", "volume": "sum"}).dropna()
    elif a["src"] == "eo":
        rows = eo_conn().get_candles(a["sym"], interval)
        if not rows:
            raise RuntimeError("waiting for Expert Option candles…")
        df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close"])
        df["volume"] = 0.0
        df.index = pd.to_datetime(df.pop("t"), unit="s", utc=True)
        if step > interval:
            df = df.resample(f"{step}s").agg({"open": "first", "high": "max", "low": "min",
                                              "close": "last", "volume": "sum"}).dropna()
    else:
        df, price, _ = yahoo_chart(a["sym"], interval)
        if len(df):  # push the live quote into the forming candle
            i = df.index[-1]
            df.loc[i, "close"] = price
            df.loc[i, "high"] = max(df.loc[i, "high"], price)
            df.loc[i, "low"] = min(df.loc[i, "low"], price)
    return df.tail(VISIBLE)


def get_price(asset):
    """(price, quote time) for an asset."""
    a = ASSETS[asset]
    if a["src"] == "binance":
        return binance_price_cached(a["sym"])
    if a["src"] == "eo":
        lp = eo_conn().last_price(a["sym"])
        if lp is None:
            raise RuntimeError("no Expert Option price yet")
        return lp
    _, price, qt = yahoo_chart(a["sym"], "1m")
    return price, qt


def market_open(asset):
    src = ASSETS[asset]["src"]
    if src == "binance":
        return True, None
    _, qt = get_price(asset)
    return time.time() - qt < (60 if src == "eo" else STALE_AFTER), qt


def safe(key, fn, *args):
    """Call fn; on network error fall back to the last good value."""
    cache = st.session_state.setdefault("last_good", {})
    try:
        cache[key] = fn(*args)
        return cache[key], None
    except Exception as e:  # noqa: BLE001
        return cache.get(key), e


# ================================================================ paper account
def new_account(balance):
    return {"balance": float(balance), "start": float(balance), "trades": [], "next_id": 1}


def load_account():
    try:
        return json.loads(ACCOUNT_FILE.read_text())
    except Exception:  # noqa: BLE001
        return new_account(DEFAULT_BALANCE)


def save_account():
    try:
        ACCOUNT_FILE.write_text(json.dumps(st.session_state.acct, indent=1))
    except Exception as e:  # noqa: BLE001
        st.toast(f"⚠️ Could not save account: {e}")


if "acct" not in st.session_state:
    st.session_state.acct = load_account()


def open_trade(asset, direction, amount, seconds):
    acct = st.session_state.acct
    if amount <= 0 or amount > acct["balance"]:
        st.toast("⚠️ Not enough demo balance")
        return
    a = ASSETS[asset]
    try:
        entry, qt = binance_price(a["sym"]) if a["src"] == "binance" else get_price(asset)
    except Exception as e:  # noqa: BLE001
        st.toast(f"⚠️ Could not get a price, trade not opened: {e}")
        return
    now = time.time()
    acct["balance"] -= amount
    acct["trades"].append(dict(
        id=acct["next_id"], asset=asset, dir=direction, amount=float(amount), entry=entry,
        entry_quote_time=qt, opened=now, expiry=int(now) + seconds, payout=a["payout"],
        status="open", close=None, result=None, closed_at=None))
    acct["next_id"] += 1
    save_account()


def settle():
    acct = st.session_state.acct
    now, changed = time.time(), False
    for tr in acct["trades"]:
        if tr["status"] != "open" or now < tr["expiry"]:
            continue
        if tr["asset"] not in ASSETS:        # e.g. an Expert Option asset not connected now
            continue
        a, close = ASSETS[tr["asset"]], None
        try:
            if a["src"] == "binance":
                if now < tr["expiry"] + 1.5:      # wait for the expiry candle to close
                    continue
                close = binance_price_at(a["sym"], tr["expiry"])
                if close is None:
                    close = binance_price(a["sym"])[0]
            else:
                price, qt = get_price(tr["asset"])
                if qt < tr["expiry"] and now < tr["expiry"] + 60:
                    continue                      # wait for a quote stamped after expiry
                close = price
        except Exception:  # noqa: BLE001
            continue
        tr["close"], tr["closed_at"] = close, now
        diff = close - tr["entry"]
        if diff == 0:
            tr["status"], tr["result"] = "tie", 0.0
            acct["balance"] += tr["amount"]
        elif (diff > 0) == (tr["dir"] == "up"):
            tr["status"], tr["result"] = "win", tr["amount"] * tr["payout"]
            acct["balance"] += tr["amount"] + tr["result"]
        else:
            tr["status"], tr["result"] = "loss", -tr["amount"]
        changed = True
        icon = {"win": "✅", "loss": "❌", "tie": "➖"}[tr["status"]]
        st.toast(f"{icon} {tr['asset']} {tr['dir'].upper()} ${tr['amount']:,.0f} → "
                 f"{'+' if tr['result'] >= 0 else ''}{tr['result']:,.2f}")
    if changed:
        save_account()


# ================================================================ chart
def local_tz():
    try:
        tz = ZoneInfo(st.context.timezone)
        return tz
    except Exception:  # noqa: BLE001
        return datetime.now().astimezone().tzinfo


_live_chart = components.declare_component(
    "live_chart", path=str(Path(__file__).with_name("live_chart")))


def live_chart(asset, chart_type, tf, df, price, height=560, message=""):
    """Persistent chart: stays mounted between reruns and animates new prices smoothly."""
    a = ASSETS[asset]
    step = TIMEFRAMES[a["src"]][tf][1]
    secs = (df.index.asi8 // 10**9).tolist()
    candles = [[int(t), float(o), float(h), float(lo), float(c), float(v or 0)]
               for t, o, h, lo, c, v in zip(secs, df["open"], df["high"], df["low"],
                                             df["close"], df["volume"].fillna(0))]
    start = secs[0] if secs else 0
    trades = [dict(opened=float(t["opened"]), expiry=float(t["expiry"]), entry=float(t["entry"]),
                   dir=t["dir"], amount=float(t["amount"]), status=t["status"],
                   result=None if t["result"] is None else float(t["result"]))
              for t in st.session_state.acct["trades"]
              if t["asset"] == asset and t["expiry"] >= start]
    return _live_chart(key="live_chart", default=None, height=height, kind=chart_type,
                       step=step, dec=a["dec"], candles=candles, price=float(price),
                       trades=trades, message=message, chart_id=f"{asset}|{tf}")


# ================================================================ sidebar
with st.sidebar:
    st.header("Paper account")
    acct = st.session_state.acct
    st.write(f"Started with **${acct['start']:,.2f}**")
    new_bal = st.number_input("Reset with balance ($)", min_value=100.0, value=DEFAULT_BALANCE,
                              step=1000.0)
    if st.button("Reset account", type="secondary", use_container_width=True):
        st.session_state.acct = new_account(new_bal)
        save_account()
        st.rerun()
    st.caption(f"Saved to `{ACCOUNT_FILE.name}` next to app.py.")
    st.divider()
    st.caption("**Data:** crypto from Binance (real-time 1s candles); forex, gold, stocks and "
               "indices from Yahoo Finance (refreshes every few seconds, some markets delayed). "
               "Trades are simulated against these real prices. No real money is involved.")

    st.divider()
    with st.expander("Expert Option feed (optional)"):
        st.caption("Uses the bundled `eoapi` client. Prices are Expert Option's own quotes. "
                   "Use a demo account; unofficial clients may break their terms.")
        try:
            secret_token = st.secrets.get("EO_TOKEN", "")
            secret_region = st.secrets.get("EO_REGION", "EUROPE")
        except Exception:  # noqa: BLE001  (no secrets file)
            secret_token, secret_region = "", "EUROPE"
        token = st.text_input("Token", value=secret_token, type="password")
        regions = list(REGIONS)
        region = st.selectbox("Region", regions, index=regions.index(secret_region)
                              if secret_region in regions else 0)
        if st.button("Connect", use_container_width=True, disabled=not token):
            try:
                with st.spinner("Connecting…"):
                    st.session_state.eo = eo_client(token, region)
            except Exception as e:  # noqa: BLE001
                eo_client.clear()
                st.error(f"Connection failed: {e}")
        eo = st.session_state.get("eo")
        if eo is not None:
            st.success(f"Connected: {eo.connected_url}" if eo.is_connected else "Reconnecting…")
            ids = sorted(eo.assets)
            if ids:
                aid = st.selectbox("Asset", ids, format_func=eo.asset_name)
            else:
                st.caption("Asset list not received yet. Enter an asset ID (240 is the "
                           "library's example).")
                aid = st.number_input("Asset ID", min_value=0, value=240, step=1)
            if st.button("Add to chart", use_container_width=True):
                info = eo.assets.get(int(aid), {})
                name = f"EO · {eo.asset_name(aid)}"
                eo.subscribe_candles(int(aid), (0, 5))
                eo.request_history(int(aid), tf=5, seconds=900)
                st.session_state.setdefault("eo_assets", {})[name] = dict(
                    src="eo", sym=int(aid), dec=int(info.get("precision", 5) or 5),
                    payout=EO_PAYOUT)
                st.session_state.asset = name
                st.rerun()
            with st.popover("Debug: raw messages", use_container_width=True):
                st.write("Actions received:", sorted(eo.last_by_action))
                pick = st.selectbox("Show", sorted(eo.last_by_action) or ["-"])
                if pick in eo.last_by_action:
                    raw = dict(eo.last_by_action[pick])
                    raw.pop("token", None)
                    st.code(json.dumps(raw, indent=1, default=str)[:6000], language="json")
                if eo.errors:
                    st.write("Errors:", list(eo.errors)[-5:])


# ================================================================ main (live)
@st.fragment(run_every=1)
def live():
    settle()
    acct = st.session_state.acct
    tz = local_tz()

    c1, c2, c3, c4 = st.columns([2, 1.2, 1.8, 2])
    asset = c1.selectbox("Asset", list(ASSETS), key="asset",
                         format_func=lambda n: f"{n}  ·  {int(ASSETS[n]['payout'] * 100)}%")
    a = ASSETS[asset]
    chart_type = c2.segmented_control("Chart", ["Area", "Candles"], default="Area",
                                      key="ctype") or "Area"
    tfs = list(TIMEFRAMES[a["src"]])
    tf = c3.segmented_control("Timeframe", tfs, default=tfs[0], key=f"tf_{a['src']}") or tfs[0]

    pnl = acct["balance"] + sum(t["amount"] for t in acct["trades"] if t["status"] == "open") \
        - acct["start"]
    c4.markdown(
        f"<div class='lbl'>Demo balance</div><div class='bal'>${acct['balance']:,.2f}</div>"
        f"<div style='color:{UP if pnl >= 0 else DOWN};font-size:.85rem'>"
        f"{'+' if pnl >= 0 else ''}{pnl:,.2f} since start</div>", unsafe_allow_html=True)

    df, err1 = safe(("c", asset, tf), get_candles, asset, tf)
    px, err2 = safe(("p", asset), get_price, asset)
    if df is None or px is None or len(df) == 0:
        st.error(f"Couldn't load market data for {asset}: {err1 or err2 or 'no data'}")
        return
    # shown inside the chart so the layout never jumps
    note = f"📡 connection problem, showing last data ({type(err1 or err2).__name__})" \
        if (err1 or err2) else ""
    price = px[0]
    is_open, qt = safe(("o", asset), market_open, asset)[0] or (True, None)

    chart_col, panel = st.columns([4, 1])
    with chart_col:
        live_chart(asset, chart_type, tf, df, price, message=note)

    with panel:
        if a["src"] == "binance":
            st.markdown(f"<span class='badge' style='background:#1f3a2c;color:{UP}'>● LIVE</span>"
                        " <span class='lbl'>Binance</span>", unsafe_allow_html=True)
        elif is_open:
            st.markdown(f"<span class='badge' style='background:#1f3a2c;color:{UP}'>● LIVE</span>"
                        " <span class='lbl'>Yahoo Finance</span>", unsafe_allow_html=True)
        else:
            last = datetime.fromtimestamp(qt, tz).strftime("%a %H:%M") if qt else "?"
            st.markdown(f"<span class='badge' style='background:#3a1f24;color:{DOWN}'>● CLOSED</span>"
                        f" <span class='lbl'>last quote {last}</span>", unsafe_allow_html=True)
        amount = st.number_input("Amount ($)", min_value=1, max_value=1_000_000, value=100,
                                 step=50, key="amount")
        exp_label = st.selectbox("Time", list(EXPIRIES), index=1, key="expiry")
        st.markdown(f"<div class='lbl'>Profit</div><div class='pnl'>+${amount * a['payout']:,.2f}"
                    f" <span style='font-size:.85rem;color:#8a93a6'>"
                    f"({int(a['payout'] * 100)}%)</span></div>", unsafe_allow_html=True)
        st.write("")
        with st.container(key="up_btn"):
            if st.button("▲  UP", use_container_width=True, disabled=not is_open):
                open_trade(asset, "up", amount, EXPIRIES[exp_label])
        with st.container(key="down_btn"):
            if st.button("▼  DOWN", use_container_width=True, disabled=not is_open):
                open_trade(asset, "down", amount, EXPIRIES[exp_label])

    now = time.time()
    open_tr = [t for t in acct["trades"] if t["status"] == "open"]
    closed = [t for t in acct["trades"] if t["status"] != "open"]
    tab_open, tab_hist = st.tabs([f"Open trades ({len(open_tr)})", f"History ({len(closed)})"])
    with tab_open:
        if not open_tr:
            st.caption("No open trades. Press UP or DOWN.")
        for t in open_tr:
            d = ASSETS.get(t["asset"], {}).get("dec", 5)
            cur = (safe(("p", t["asset"]), get_price, t["asset"])[0] or (t["entry"], 0))[0]
            winning = cur != t["entry"] and (cur > t["entry"]) == (t["dir"] == "up")
            left = max(0, t["expiry"] - now)
            state = (f"<span style='color:{UP}'>winning</span>" if winning
                     else f"<span style='color:{DOWN}'>losing</span>")
            st.markdown(
                f"**{t['asset']}** &nbsp; {'🟢 UP' if t['dir'] == 'up' else '🔴 DOWN'} &nbsp; "
                f"${t['amount']:,.0f} &nbsp;·&nbsp; entry `{t['entry']:.{d}f}` → now `{cur:.{d}f}`"
                f" &nbsp;·&nbsp; ⏱ {int(left // 60)}:{int(left % 60):02d} &nbsp;·&nbsp; {state}",
                unsafe_allow_html=True)
    with tab_hist:
        if not closed:
            st.caption("No closed trades yet.")
        else:
            wins = sum(t["status"] == "win" for t in closed)
            net = sum(t["result"] for t in closed)
            m1, m2, m3 = st.columns(3)
            m1.metric("Trades", len(closed))
            m2.metric("Win rate", f"{wins / len(closed) * 100:.1f}%")
            m3.metric("Net P&L", f"${net:,.2f}")
            hist = pd.DataFrame([{
                "Asset": t["asset"], "Dir": t["dir"].upper(), "Amount": t["amount"],
                "Entry": t["entry"], "Close": t["close"], "Result": round(t["result"], 2),
                "Opened": datetime.fromtimestamp(t["opened"], tz).strftime("%Y-%m-%d %H:%M:%S"),
                "Expiry": datetime.fromtimestamp(t["expiry"], tz).strftime("%H:%M:%S"),
            } for t in reversed(closed)])
            st.dataframe(hist, hide_index=True, use_container_width=True, height=300)
            st.download_button("Download CSV", hist.to_csv(index=False), "trades.csv",
                               "text/csv", key="dl")


live()
