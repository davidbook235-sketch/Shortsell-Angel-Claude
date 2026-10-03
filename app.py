import io
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyotp
import requests
import streamlit as st
from SmartApi import SmartConnect

st.set_page_config(page_title="Intraday Short Scanner (Angel One)", layout="wide")
UA = {"User-Agent": "Mozilla/5.0"}
IST = ZoneInfo("Asia/Kolkata")
OR_END, CUT, SQ = dtime(9, 30), dtime(14, 30), dtime(15, 15)
IV = {"5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE"}
NIFTY_TOKEN = "99926000"
FALLBACK = ("RELIANCE,TCS,HDFCBANK,INFY,ICICIBANK,SBIN,ITC,LT,AXISBANK,KOTAKBANK,TATAMOTORS,TATASTEEL,"
            "ADANIENT,BAJFINANCE,MARUTI,SUNPHARMA,WIPRO,HCLTECH,ONGC,NTPC,COALINDIA,JSWSTEEL,HINDALCO,"
            "VEDL,DLF,PNB,BANKBARODA").split(",")
LAST = st.cache_resource(lambda: {})()


@st.cache_data(ttl=86400)
def universe():
    try:
        r = requests.get("https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
                         headers=UA, timeout=15)
        r.raise_for_status()
        return pd.read_csv(io.StringIO(r.text))[["Symbol", "Industry"]].dropna(subset=["Symbol"])
    except Exception:
        return pd.DataFrame({"Symbol": FALLBACK, "Industry": "Unknown"})


@st.cache_data(ttl=86400)
def fo_universe():
    try:
        r = requests.get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", headers=UA, timeout=15)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.strip() for c in df.columns]
        return set(df["SYMBOL"].astype(str).str.strip())
    except Exception:
        return set()


SCRIP_URLS = ["https://margincalculator.angelbroking.com/OpenAPI_Files/files/OpenAPIScripMaster.json",
               "https://margincalculator.angelone.in/OpenAPI_Files/files/OpenAPIScripMaster.json"]
TOK = st.cache_resource(lambda: {})()


@st.cache_data(ttl=43200, show_spinner="Angel One instrument list load ho rahi hai (30-60 sec)...")
def scrip_map():
    """NSE equity symbol -> Angel One token."""
    errs = []
    for url in SCRIP_URLS:
        for _ in range(2):
            try:
                r = requests.get(url, headers=UA, timeout=(15, 180))
                r.raise_for_status()
                rows = r.json()
                m = {x["symbol"][:-3]: x["token"] for x in rows
                     if x.get("exch_seg") == "NSE" and x.get("symbol", "").endswith("-EQ")}
                del rows
                if m:
                    return m
            except Exception as e:
                errs.append(f"{type(e).__name__}")
    raise RuntimeError(", ".join(errs))


def find_tokens(api, syms):
    """Slow fallback: Angel searchScrip se token dhundho (session tak cache)."""
    for s in syms:
        if s in TOK:
            continue
        wait("s", 0.6)
        try:
            TOK[s] = ""
            for x in (api.searchScrip("NSE", s + "-EQ").get("data") or []):
                if x.get("tradingsymbol") == s + "-EQ":
                    TOK[s] = str(x["symboltoken"])
                    break
        except Exception:
            TOK.pop(s, None)
    return {s: TOK[s] for s in syms if TOK.get(s)}


def get_tmap(api, syms):
    if syms and all(s in TOK for s in syms):
        return {s: TOK[s] for s in syms if TOK.get(s)}
    if time.time() - TOK.get("_fail", 0) > 3600:
        try:
            m = scrip_map()
            TOK.update(m)
            return m
        except Exception as e:
            TOK["_fail"] = time.time()
            TOK["_err"] = str(e)
    st.caption(f"ℹ️ Angel instrument file load nahi hui ({TOK.get('_err', '')}), isliye tokens ek-ek karke dhundh raha hoon. "
               "Pehli baar ~1-2 min lagenge, phir session tak cache rahenge.")
    with st.spinner("Tokens dhundh raha hoon..."):
        return find_tokens(api, syms)


def secret(k):
    try:
        return st.secrets[k]
    except Exception:
        return ""


def tg_send(token, chat, text):
    try:
        return requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                             json={"chat_id": chat, "text": text}, timeout=10).ok
    except Exception:
        return False


def login(key, cid, pin, totp):
    try:
        api = SmartConnect(api_key=key)
        r = api.generateSession(cid, pin, pyotp.TOTP(totp.replace(" ", "")).now())
        if r and r.get("status"):
            st.session_state.api = api
            return True, "Login ho gaya ✅"
        return False, (r or {}).get("message", "Login fail")
    except Exception as e:
        return False, f"Login error: {e}"


def wait(kind, gap):
    dt = gap - (time.time() - LAST.get(kind, 0))
    if dt > 0:
        time.sleep(dt)
    LAST[kind] = time.time()


def to_df(rows):
    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    idx = pd.DatetimeIndex(pd.to_datetime(df.pop("ts"), utc=True)).tz_convert(IST)
    return df.astype(float).set_axis(idx).sort_index()


def _get(api, token, iv, days):
    to = datetime.now(IST)
    p = {"exchange": "NSE", "symboltoken": str(token), "interval": IV[iv],
         "fromdate": (to - timedelta(days=days)).strftime("%Y-%m-%d %H:%M"), "todate": to.strftime("%Y-%m-%d %H:%M")}
    for _ in range(2):
        wait("c", 0.4)  # Angel historical limit ~3 req/sec
        try:
            r = api.getCandleData(p)
            if r and r.get("status") and r.get("data"):
                return to_df(r["data"])
        except Exception:
            pass
        time.sleep(1.2)
    raise RuntimeError("candle data nahi mila")


@st.cache_data(ttl=60, show_spinner=False)
def live_c(_api, token, iv, days):
    return _get(_api, token, iv, days)


@st.cache_data(ttl=3600, show_spinner=False)
def hist_c(_api, token, iv, days):
    return _get(_api, token, iv, days)


@st.cache_data(ttl=20, show_spinner=False)
def get_quotes(_api, toks):
    """Live LTP/prev close/VWAP(avgPrice)/volume, 50 tokens per call."""
    out = {}
    for i in range(0, len(toks), 50):
        wait("q", 1.1)
        try:
            r = _api.getMarketData("FULL", {"NSE": list(toks[i:i + 50])})
            for q in r["data"]["fetched"]:
                out[str(q["symbolToken"])] = q
        except Exception:
            pass
    return out


def dchg(ser):
    dt = np.array(ser.index.date)
    prev = ser[dt < dt[-1]]
    return ser.iloc[-1] / prev.iloc[-1] - 1 if len(prev) else np.nan


def ind(d):
    c, h, l = d.Close, d.High, d.Low
    d["e20"] = c.ewm(span=20, adjust=False).mean()
    d["e50"] = c.ewm(span=50, adjust=False).mean()
    dl = c.diff()
    up = dl.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-dl.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return d


def analyze(d, nch):
    """Aaj ke session ka intraday short score."""
    d = ind(d.copy())
    dates = np.array(d.index.date)
    t, p = d[dates == dates[-1]], d[dates < dates[-1]]
    if len(p) == 0 or len(t) < 3:
        return None
    pdt = np.array(p.index.date)
    pc, pl = p.Close.iloc[-1], p[pdt == pdt[-1]].Low.min()
    ltp, n, x = t.Close.iloc[-1], len(t), d.iloc[-1]
    tp = (t.High + t.Low + t.Close) / 3
    vwap = ((tp * t.Volume).cumsum() / t.Volume.cumsum()).iloc[-1]
    ort = t[t.index.time < OR_END]
    orb = bool(len(ort)) and (t.index.time >= OR_END).any() and ltp < ort.Low.min()
    cv = [g.Volume.iloc[:n].sum() for _, g in p.groupby(np.array(p.index.date)) if len(g) >= n]
    rvol = t.Volume.sum() / np.mean(cv) if cv else np.nan
    chg = ltp / pc - 1
    rs = (chg - nch) * 100 if not np.isnan(nch) else np.nan
    sc = (20 * (ltp < vwap) + 10 * (ltp < x.e20) + 10 * (x.e20 < x.e50) + 20 * orb + 10 * (ltp < pl)
          + 10 * (25 < x.rsi < 50) + 10 * (rvol > 1.5) + 10 * (rs < -0.5))
    return dict(ltp=ltp, chg=chg * 100, gap=(t.Open.iloc[0] / pc - 1) * 100, sc=int(sc),
                vd=(ltp / vwap - 1) * 100, orb=orb, rvol=rvol, rs=rs, rsi=x.rsi, atr=x.atr,
                val=t.Volume.sum() * ltp / 1e7)


def backtest(d, k, rr, cost, need_pc):
    d = ind(d.copy())
    d["date"], d["t"] = d.index.date, d.index.time
    pcs = d.groupby("date").Close.last().shift().to_dict()
    rows = []
    for dt, g in d.groupby("date"):
        if len(g) < 10 or pd.isna(pcs.get(dt)):
            continue
        tp = (g.High + g.Low + g.Close) / 3
        vw = (tp * g.Volume).cumsum() / g.Volume.cumsum()
        orl = g[g.t < OR_END].Low.min()
        ok = (g.t >= OR_END) & (g.t <= CUT) & (g.Close < vw) & (g.Close < orl) & (g.Close < g.e20)
        if need_pc:
            ok &= g.Close < pcs[dt]
        idx = np.flatnonzero(ok.values)
        lim = int((g.t < SQ).sum()) - 1
        if not len(idx) or idx[0] + 1 > lim:
            continue
        i = idx[0]
        o, h, l, c = g.Open.values, g.High.values, g.Low.values, g.Close.values
        e, r = o[i + 1], k * g.atr.values[i]
        sl, tgt = e + r, e - rr * r
        for j in range(i + 1, lim + 1):
            if h[j] >= sl:
                ex = sl
                break
            if l[j] <= tgt:
                ex = tgt
                break
        else:
            ex = c[lim]
        rows.append((g.index[i + 1], e, ex, (e - ex) / e - 2 * cost / 100))
    return pd.DataFrame(rows, columns=["Time", "Entry", "Exit", "Ret"])


udf = universe()
syms_all = udf["Symbol"].tolist()
sec_map = dict(zip(udf["Symbol"], udf["Industry"]))
st.title("📉 Intraday Short Scanner")
st.caption("Live data: Angel One SmartAPI")
if "api" not in st.session_state and all(secret(k) for k in ["ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP"]):
    login(secret("ANGEL_API_KEY"), secret("ANGEL_CLIENT_ID"), secret("ANGEL_PIN"), secret("ANGEL_TOTP"))
with st.expander("🔐 Angel One Login " + ("✅" if "api" in st.session_state else ""), expanded="api" not in st.session_state):
    lk = st.text_input("API Key", secret("ANGEL_API_KEY"), type="password")
    lc = st.text_input("Client ID", secret("ANGEL_CLIENT_ID"))
    lp = st.text_input("PIN", secret("ANGEL_PIN"), type="password")
    lt = st.text_input("TOTP secret (QR ke neeche wali key)", secret("ANGEL_TOTP"), type="password")
    if st.button("Login", use_container_width=True):
        ok, m = login(lk, lc, lp, lt)
        (st.success if ok else st.error)(m)
api = st.session_state.get("api")
now = datetime.now(IST)
open_ = now.weekday() < 5 and dtime(9, 15) <= now.time() <= dtime(15, 30)
msg = f"🕒 {now:%H:%M} IST — " + ("Market OPEN" if open_ else "Market CLOSED (last session ka data)")
if open_ and now.time() >= SQ:
    st.error(msg + " — Squareoff time, positions band karo!")
elif open_ and now.time() >= CUT:
    st.warning(msg + " — 2:30 ke baad naya short avoid karo.")
else:
    st.info(msg)
tab1, tab2 = st.tabs(["🔍 Live Scanner", "🧪 Backtest"])
bt_clicked = False

with tab1:
    c1, c2 = st.columns(2)
    iv = c1.selectbox("Candle", ["5m", "15m"])
    thr = c2.slider("Min score", 40, 100, 70, 5)
    c3, c4 = st.columns(2)
    cap = c3.number_input("Capital ₹", 10000, 10000000, 100000, 10000)
    lev = c4.number_input("MIS leverage ×", 1, 10, 5)
    c5, c6 = st.columns(2)
    rk = c5.number_input("Risk per trade %", 0.1, 3.0, 0.5, 0.1)
    atrk = c6.number_input("Stop = ATR ×", 0.5, 4.0, 1.5, 0.1)
    c7, c8 = st.columns(2)
    minp = c7.number_input("Min price ₹", 0, 5000, 100)
    minv = c8.number_input("Min traded value ₹ Cr", 0.0, 500.0, 5.0, 1.0)
    n = st.slider("Max stocks scan", 20, len(syms_all), min(len(syms_all), 500), 10)
    maxc = st.slider("Deep-scan max stocks (API calls bachane ke liye)", 10, 150, 60, 10)
    weak_n = st.slider("Weakest sectors highlight", 1, 10, 4)
    pre_vwap = st.checkbox("Pre-filter: sirf live VWAP ke neeche wale", value=True)
    fo_only = st.checkbox("Sirf F&O stocks (liquid)", value=True)
    neg_only = st.checkbox("Sirf pichhle close se neeche wale", value=True)
    rs_only = st.checkbox("Sirf Nifty se weak (RS < 0)", value=True)
    sec_only = st.checkbox("Sirf weak sectors ke stocks", value=False)
    auto = st.checkbox("Auto-refresh")
    secs = st.slider("Refresh har (sec)", 60, 600, 120, 30) if auto else 120
    tg = st.checkbox("Telegram alert (sirf naye setups)")
    tok = chat = ""
    if tg:
        tok = st.text_input("Bot token", secret("TG_TOKEN"), type="password")
        chat = st.text_input("Chat ID", secret("TG_CHAT"))
    if st.button("🚀 Scan / Refresh", type="primary", use_container_width=True):
        st.session_state.run = True
        live_c.clear()
        get_quotes.clear()
    if st.session_state.get("run") and not api:
        st.error("Pehle upar Angel One login karo.")
    elif st.session_state.get("run"):
        fo = fo_universe()
        if fo_only and not fo:
            st.warning("F&O list load nahi hui, filter skip kiya.")
        pool = [s for s in syms_all if s in fo] if (fo_only and fo) else syms_all
        tmap = get_tmap(api, pool[:n])
        pool_t = [(s, tmap[s]) for s in pool[:n] if s in tmap]
        with st.spinner("Live quotes aa rahe hain..."):
            quotes = get_quotes(api, tuple(t for _, t in pool_t))
        qrows = []
        for s, t in pool_t:
            q = quotes.get(str(t))
            try:
                ltp, pc = float(q["ltp"]), float(q["close"])
                avg, vol = float(q.get("avgPrice") or 0), float(q.get("tradeVolume") or 0)
            except Exception:
                continue
            if ltp > 0 and pc > 0:
                qrows.append((s, t, (ltp / pc - 1) * 100, ltp, avg, vol * ltp / 1e7))
        if not qrows:
            st.error("Quotes/tokens nahi mile. Upar ka warning dekho, login/session check karo aur dobara try karo.")
        else:
            cand = [r for r in qrows if r[3] >= minp and r[5] >= minv and (not neg_only or r[2] < 0)
                    and (not pre_vwap or r[4] <= 0 or r[3] < r[4])]
            cand = sorted(cand, key=lambda r: r[2])[:maxc]
            try:
                nf = live_c(api, NIFTY_TOKEN, iv, 8)
                nch = dchg(nf.Close)
            except Exception:
                nch = np.nan
            bar = st.progress(0.0, "Candles aa rahe hain...")
            res_all = {}
            for k, (s, t, *_r) in enumerate(cand):
                try:
                    a_ = analyze(live_c(api, t, iv, 8), nch)
                except Exception:
                    a_ = None
                if a_:
                    res_all[s] = a_
                bar.progress((k + 1) / max(len(cand), 1))
            bar.empty()
            st.caption(f"Nifty aaj: {nch * 100:.2f}% | {len(qrows)} stocks quote, {len(cand)} deep-scan | {now:%H:%M:%S} IST")
            sd = pd.DataFrame([(sec_map.get(r[0], "Unknown"), r[2]) for r in qrows], columns=["Sector", "chg"])
            sg = sd.groupby("Sector").agg(Stocks=("chg", "size"), AvgChg=("chg", "mean"),
                                          Red=("chg", lambda v: (v < 0).mean() * 100))
            sg = sg[sg.Stocks >= 3]
            sg["r"] = sg.AvgChg.rank() + sg.Red.rank(ascending=False)
            sg = sg.sort_values("r")
            weak = [i for i in sg.index[:weak_n] if sg.AvgChg[i] < 0]
            st.subheader("🏭 Sector Weakness (aaj)")
            st.dataframe(sg.drop(columns="r").round(2).rename(columns={"AvgChg": "Avg chg %", "Red": "Red %"}),
                         use_container_width=True)
            if weak:
                st.info("🔻 Selling: " + ", ".join(f"**{w}** ({sg.AvgChg[w]:.2f}%, {sg.Red[w]:.0f}% red)" for w in weak))
            rows = []
            for s, a in res_all.items():
                if a["sc"] < thr or a["ltp"] < minp or a["val"] < minv:
                    continue
                if (neg_only and a["chg"] >= 0) or (rs_only and a["rs"] >= 0):
                    continue
                sec_ = sec_map.get(s, "Unknown")
                risk = atrk * a["atr"]
                qty = int(min(cap * rk / 100 / risk, cap * lev / a["ltp"])) if risk > 0 else 0
                rows.append({"Stock": s, "Sector": sec_, "LTP": round(a["ltp"], 2), "Chg%": round(a["chg"], 2),
                             "Gap%": round(a["gap"], 2), "Score": a["sc"] + (10 if sec_ in weak else 0),
                             "VWAPdist%": round(a["vd"], 2), "ORB": "✔" if a["orb"] else "",
                             "RVol": round(a["rvol"], 2), "RS%": round(a["rs"], 2), "RSI": round(a["rsi"], 1),
                             "SL": round(a["ltp"] + risk, 2), "T1": round(a["ltp"] - 1.5 * risk, 2),
                             "T2": round(a["ltp"] - 2.5 * risk, 2), "Qty": qty,
                             "🔻": "🔻" if sec_ in weak else ""})
            res = pd.DataFrame(rows)
            if len(res) and sec_only:
                res = res[res["🔻"] == "🔻"]
            st.success(f"{len(res)} intraday short setups mile")
            if len(res):
                res = res.sort_values("Score", ascending=False)
                st.dataframe(res, hide_index=True, use_container_width=True)
                if tg:
                    sent = st.session_state.setdefault("sent", {})
                    new = res[[sent.get(s) != datetime.now().date() for s in res.Stock]]
                    if len(new) and tok and chat:
                        lines = [f"{r.Stock} | LTP {r.LTP} | Score {r.Score} | SL {r.SL} | T1 {r.T1} | Qty {r.Qty}"
                                 for r in new.head(10).itertuples()]
                        if tg_send(tok, chat, f"📉 Intraday shorts ({iv})\nWeak: {', '.join(weak) or '-'}\n" + "\n".join(lines)):
                            sent.update({s: datetime.now().date() for s in new.Stock})
                            st.success("Telegram alert bhej diya ✅")
                        else:
                            st.error("Telegram fail, token/chat ID check karo")
                    elif tg and not (tok and chat):
                        st.warning("Telegram token aur chat ID daalo.")
    st.caption("Score: VWAP ke neeche 20, ORB breakdown 20, EMA20 10, EMA20<EMA50 10, prev-day low 10, RSI 10, "
               "time-adjusted volume 10, Nifty se weak 10 (+10 weak sector). SL/Target ATR based. "
               "Data: Angel One SmartAPI (live). Educational use only, advice nahi.")

with tab2:
    c1, c2 = st.columns(2)
    biv = c1.selectbox("Candle", ["5m", "15m"], key="bi")
    days = c2.selectbox("History (din)", [10, 30, 60, 90], index=1)
    c3, c4 = st.columns(2)
    bn = c3.slider("Stocks (F&O first)", 10, 200, 50, 10)
    bk = c4.number_input("SL ATR ×", 0.5, 4.0, 1.5, 0.1, key="b2")
    c5, c6 = st.columns(2)
    brr = c5.number_input("Reward:Risk", 1.0, 4.0, 1.5, 0.5)
    cost = c6.number_input("Cost per side %", 0.0, 0.5, 0.03, 0.01)
    c7, c8 = st.columns(2)
    alloc = c7.number_input("Capital per trade %", 1, 100, 10)
    need_pc = c8.checkbox("Pichhle close se neeche ho", value=True)
    if st.button("▶️ Run Intraday Backtest", type="primary", use_container_width=True):
        bt_clicked = True
        fo = fo_universe()
        pool = ([s for s in syms_all if s in fo] + [s for s in syms_all if s not in fo])[:bn]
        tm = get_tmap(api, pool) if api else {}
        pool = [(s, tm[s]) for s in pool if s in tm]
        fr = []
        if not api:
            st.error("Pehle Angel One login karo.")
        else:
            bar = st.progress(0.0, "Historical candles aa rahe hain (rate limit ke karan time lagega)...")
            for k, (s, t) in enumerate(pool):
                try:
                    tr_ = backtest(hist_c(api, t, biv, days), bk, brr, cost, need_pc)
                except Exception:
                    tr_ = []
                if len(tr_):
                    tr_.insert(0, "Stock", s)
                    fr.append(tr_)
                bar.progress((k + 1) / len(pool))
            bar.empty()
        if api and not fr:
            st.warning("Koi trade nahi bana. History badhao ya filter dheela karo.")
        elif fr:
            T = pd.concat(fr).sort_values("Time").reset_index(drop=True)
            eq = (1 + T.Ret * alloc / 100).cumprod()
            gp, gl = T.Ret[T.Ret > 0].sum(), -T.Ret[T.Ret < 0].sum()
            m = st.columns(2)
            m[0].metric("Trades", len(T))
            m[1].metric("Win rate", f"{(T.Ret > 0).mean() * 100:.1f}%")
            m = st.columns(2)
            m[0].metric("Profit factor", f"{gp / gl:.2f}" if gl else "∞")
            m[1].metric("Avg trade", f"{T.Ret.mean() * 100:.2f}%")
            m = st.columns(2)
            m[0].metric("Total return", f"{(eq.iloc[-1] - 1) * 100:.1f}%")
            m[1].metric("Max drawdown", f"{(eq / eq.cummax() - 1).min() * 100:.1f}%")
            st.line_chart(pd.Series(eq.values, index=range(len(eq)), name="Equity"))
            st.dataframe(T, hide_index=True, use_container_width=True)
            st.download_button("⬇️ Trades CSV", T.to_csv(index=False), "intraday_trades.csv")
    st.caption("Rule: 9:30–2:30 ke beech pehli candle jo VWAP, Opening Range low aur EMA20 teeno ke neeche close kare → "
               "agli candle open par short. Stop pehle check hota hai, 3:15 PM par squareoff. Roz 1 trade/stock. "
               "Angel history: 5m me max 100 din. Result indicative hai, real slippage alag hota hai.")

if auto and st.session_state.get("run") and not bt_clicked:
    time.sleep(secs)
    st.rerun()
