"""בוט התראות S&P 500: כניסה בבוקר, יציאה במהלך היום. דמה בלבד, לא ייעוץ השקעות.
שימוש: python bot.py live | python bot.py backtest"""
import io, json, os, sys, datetime as dt
from zoneinfo import ZoneInfo
import pandas as pd, requests, yfinance as yf

NY = ZoneInfo("America/New_York")
BAR = pd.Timedelta(minutes=5)
# הגדרות לשיפור בהמשך
C = dict(top_n=10, min_gap=0.015, min_price=10, min_vol=1_000_000,
         rr=2.0, slip=0.0005, max_pos=5, stale=3, liquid_n=150)
FALLBACK = "AAPL MSFT NVDA AMZN GOOGL META TSLA AMD AVGO NFLX JPM BAC XOM COST".split()


def token():
    return (os.getenv("TELEGRAM_TOKEN") or "").strip()


def chat_id():
    """מזהה הצ'אט: מהסוד, מקובץ שמור, או מההודעה האחרונה ששלחת לבוט."""
    c = os.getenv("TELEGRAM_CHAT_ID")
    if c:
        return c
    if os.path.exists("chat_id.txt"):
        return open("chat_id.txt").read().strip()
    try:
        r = requests.get(f"https://api.telegram.org/bot{token()}/getUpdates",
                         timeout=20).json()
        for u in reversed(r.get("result", [])):
            if u.get("message"):
                c = str(u["message"]["chat"]["id"])
                open("chat_id.txt", "w").write(c)
                return c
    except Exception as e:
        print("chat id lookup failed", e)
    return None


def notify(msg):
    print(msg)
    tok = token()
    chat = chat_id() if tok else None
    if tok and not chat:
        print("אין מזהה צ'אט: שלח הודעה לבוט בטלגרם ואז הרץ שוב")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": msg}, timeout=20)
        except Exception as e:
            print("telegram error", e)


def sp500():
    try:
        r = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        df = pd.read_html(io.StringIO(r.text))[0]
        return [s.replace(".", "-") for s in df["Symbol"]]
    except Exception as e:
        print("wikipedia failed, using fallback", e)
        return FALLBACK


def _fix(df, intra):
    idx = pd.to_datetime(df.index)
    if intra:
        idx = idx.tz_localize("UTC") if idx.tz is None else idx
        idx = idx.tz_convert(NY)
    elif idx.tz is not None:
        idx = idx.tz_localize(None)
    df = df.copy()
    df.index = idx
    return df


def get(tickers, period, interval):
    out, intra = {}, interval != "1d"
    for i in range(0, len(tickers), 50):
        ch = tickers[i:i + 50]
        try:
            df = yf.download(ch, period=period, interval=interval, group_by="ticker",
                             progress=False, threads=True)
        except Exception as e:
            print("download error", e)
            continue
        for t in ch:
            try:
                x = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                x = x.dropna(subset=["Close"])
                if len(x):
                    out[t] = _fix(x, intra)
            except KeyError:
                pass
    return out


def pick(d, day):
    """מניות עם פער פתיחה משמעותי: פער למעלה -> לונג בלבד, פער למטה -> שורט בלבד."""
    rows = []
    for t, df in d.items():
        x = df.loc[:pd.Timestamp(day)]
        if len(x) < 22 or x.index[-1].date() != day:
            continue
        prev, cur = x.iloc[-2], x.iloc[-1]
        gap = cur.Open / prev.Close - 1
        if (abs(gap) < C["min_gap"] or prev.Close < C["min_price"]
                or x.Volume.iloc[-21:-1].mean() < C["min_vol"]):
            continue
        rows.append((t, gap))
    rows.sort(key=lambda r: -abs(r[1]))
    return [(t, g, "long" if g > 0 else "short") for t, g in rows[:C["top_n"]]]


def prep(b):
    b = b.between_time("09:30", "15:59").copy()
    if len(b) == 0 or b.Volume.sum() == 0:
        return b.iloc[0:0]
    tp = (b.High + b.Low + b.Close) / 3
    b["vwap"] = (tp * b.Volume).cumsum() / b.Volume.cumsum().replace(0, float("nan"))
    return b.dropna(subset=["vwap"])


P0 = dict(rr=2.0, vwap_exit=True, vol_mult=0, long_only=False, stop="mid")


def signal(b, side, P=P0):
    """פריצת טווח 15 הדקות הראשונות + VWAP באותו כיוון."""
    if P["long_only"] and side == "short":
        return None
    orb = b.between_time("09:30", "09:40")
    if len(orb) < 3:
        return None
    hi, lo = orb.High.max(), orb.Low.min()
    av = b.Volume.expanding().mean().shift(1)
    for ts, r in b.between_time("09:45", "14:55").iterrows():
        up = side == "long" and r.Close > hi and r.Close > r.vwap
        dn = side == "short" and r.Close < lo and r.Close < r.vwap
        if not (up or dn):
            continue
        if P["vol_mult"] and not (r.Volume >= P["vol_mult"] * av[ts]):
            continue
        stop = (hi + lo) / 2 if P["stop"] == "mid" else (lo if up else hi)
        risk = abs(r.Close - stop)
        if risk / r.Close < 0.002:
            continue
        sgn = 1 if up else -1
        return dict(side=side, ts=ts.isoformat(), entry=float(r.Close),
                    stop=float(stop), target=float(r.Close + sgn * P["rr"] * risk))
    return None


def walk(b, p, P=P0):
    """בודק יציאה אחרי הכניסה. מחזיר (זמן, מחיר, סיבה) או None אם הפוזיציה פתוחה."""
    lg = p["side"] == "long"
    for ts, r in b[b.index > pd.Timestamp(p["ts"])].iterrows():
        if (r.Low <= p["stop"]) if lg else (r.High >= p["stop"]):
            return ts, p["stop"], "סטופ"
        if (r.High >= p["target"]) if lg else (r.Low <= p["target"]):
            return ts, p["target"], "יעד"
        if P["vwap_exit"] and ((r.Close < r.vwap) if lg else (r.Close > r.vwap)):
            return ts, float(r.Close), "חזרה ל-VWAP"
        if ts.time() >= dt.time(15, 40):
            return ts, float(r.Close), "סוף יום"
    return None


def result(p, px):
    s = 1 if p["side"] == "long" else -1
    e, x = p["entry"] * (1 + s * C["slip"]), px * (1 - s * C["slip"])
    pct = s * (x / e - 1) * 100
    risk_pct = abs(p["entry"] - p["stop"]) / p["entry"] * 100
    return round(pct, 3), round(pct / risk_pct, 2)


def summary(df, title):
    w, l = df[df.pct > 0], df[df.pct <= 0]
    pf = w.pct.sum() / abs(l.pct.sum()) if len(l) and l.pct.sum() != 0 else float("inf")
    cum = df.pct.cumsum()
    dd = (cum - cum.cummax()).min()
    return (f"📊 {title}\nעסקאות: {len(df)}\nאחוז הצלחה: {len(w) / len(df) * 100:.0f}%\n"
            f"ממוצע לעסקה: {df.pct.mean():+.2f}%\nממוצע R: {df.r.mean():+.2f}\n"
            f"יחס רווח/הפסד: {pf:.2f}\nסה״כ מצטבר: {df.pct.sum():+.1f}%\nירידה מקסימלית: {dd:.1f}%")


def log_trade(day, s, p, ts, px, why):
    pct, r = result(p, px)
    row = pd.DataFrame([dict(date=day, symbol=s, side=p["side"], entry_ts=p["ts"],
                             entry=p["entry"], exit_ts=ts.isoformat(), exit=px,
                             reason=why, pct=pct, r=r)])
    row.to_csv("trades.csv", mode="a", header=not os.path.exists("trades.csv"), index=False)
    return pct, r


def test():
    tok = token()
    print("אורך הטוקן:", len(tok), "| יש נקודתיים:", ":" in tok)
    try:
        me = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=20).json()
        print("getMe:", me.get("ok"), me.get("description", ""), (me.get("result") or {}).get("username", ""))
        up = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=20).json()
        print("getUpdates:", up.get("ok"), up.get("description", ""), "| הודעות:", len(up.get("result", [])))
    except Exception as e:
        print("שגיאת רשת:", e)
    notify("✅ הבוט מחובר ועובד")


def live():
    now = dt.datetime.now(NY)
    today, t = now.date(), now.time()
    if now.weekday() >= 5 or t < dt.time(9, 45):
        return
    st = json.load(open("state.json")) if os.path.exists("state.json") else {}
    if st.get("date") != str(today):
        st = dict(date=str(today), watch=None, pos={}, done=[], eod=False)

    if st["watch"] is None:
        picks = pick(get(sp500(), "3mo", "1d"), today) if t < dt.time(16, 0) else []
        st["watch"] = [dict(s=s, side=sd, gap=g) for s, g, sd in picks]
        if picks:
            lines = [f"{s} {'לונג' if sd == 'long' else 'שורט'} (פער {g * 100:+.1f}%)" for s, g, sd in picks]
            notify("📋 רשימת מעקב להיום:\n" + "\n".join(lines))

    syms = sorted({w["s"] for w in st["watch"]} | set(st["pos"]))
    if syms and t < dt.time(16, 5):
        raw = get(syms, "1d", "5m")
        bars = {s: prep(b[b.index + BAR <= pd.Timestamp(now)]) for s, b in raw.items()}
        for w in st["watch"]:
            s = w["s"]
            if s in st["done"] or s in st["pos"] or s not in bars or len(bars[s]) < 4:
                continue
            if len(st["pos"]) >= C["max_pos"]:
                break
            p = signal(bars[s], w["side"], LIVE_P)
            if not p:
                continue
            st["done"].append(s)
            if bars[s].index[-1] - pd.Timestamp(p["ts"]) > BAR * C["stale"]:
                continue  # האיתות ישן מדי
            st["pos"][s] = p
            icon = "🟢 לונג" if p["side"] == "long" else "🔴 שורט"
            notify(f"{icon} כניסה (דמה): {s}\nמחיר: {p['entry']:.2f}\nסטופ: {p['stop']:.2f}\nיעד: {p['target']:.2f}")
        for s, p in list(st["pos"].items()):
            if s not in bars or len(bars[s]) < 2:
                continue
            res = walk(bars[s], p, LIVE_P)
            if res:
                ts, px, why = res
                pct, r = log_trade(str(today), s, p, ts, px, why)
                notify(f"🏁 יציאה (דמה): {s} ({why})\nמחיר: {px:.2f}\nתוצאה: {pct:+.2f}% ({r:+.2f}R)")
                del st["pos"][s]

    if t >= dt.time(16, 5) and not st["eod"]:
        st["eod"] = True
        for s, p in list(st["pos"].items()):  # פוזיציה שנשארה פתוחה בגלל נתונים חסרים
            b = get([s], "1d", "5m").get(s)
            if b is not None and len(b):
                px = float(b.Close.iloc[-1])
                log_trade(str(today), s, p, b.index[-1], px, "סוף יום")
            del st["pos"][s]
        if os.path.exists("trades.csv"):
            df = pd.read_csv("trades.csv")
            td = df[df.date == str(today)]
            msg = f"סיכום יום (דמה): {len(td)} עסקאות, {td.pct.sum():+.2f}%\n\n" if len(td) else "היום לא היו עסקאות.\n\n"
            notify(msg + summary(df, "מצטבר מאז ההתחלה"))
        else:
            notify("היום לא היו עסקאות.")
    json.dump(st, open("state.json", "w"), ensure_ascii=False, indent=1)


VARIANTS = {
    "א. המקור": {},
    "ב. בלי יציאה ב-VWAP": dict(vwap_exit=False),
    "ג. בלי VWAP, יעד פי 1.5": dict(vwap_exit=False, rr=1.5),
    "ד. בלי VWAP, סטופ רחב": dict(vwap_exit=False, stop="far"),
    "ה. בלי VWAP, נפח חריג": dict(vwap_exit=False, vol_mult=1.5),
    "ו. בלי VWAP, לונג בלבד": dict(vwap_exit=False, long_only=True),
    "ז. נפח חריג + סטופ רחב": dict(vwap_exit=False, vol_mult=1.5, stop="far"),
}


# גרסת הדמה הקדימה: לא משנים אותה במשך כל תקופת הניסוי
LIVE_P = {**P0, **VARIANTS["ה. בלי VWAP, נפח חריג"]}


def load_bt():
    d = get(sp500(), "6mo", "1d")
    liq = sorted(d, key=lambda t: (d[t].Close * d[t].Volume).tail(20).mean(), reverse=True)[:C["liquid_n"]]
    d = {t: d[t] for t in liq}
    m = get(liq, "58d", "5m")
    days = sorted({x for df in m.values() for x in df.index.date})
    return d, m, days, {day: pick(d, day) for day in days}


def simulate(m, days, picks, P):
    rows = []
    for day in days:
        for s, g, sd in picks[day]:
            if s not in m:
                continue
            b = prep(m[s][m[s].index.date == day])
            if len(b) < 20:
                continue
            p = signal(b, sd, P)
            if not p:
                continue
            w = walk(b, p, P)
            ts, px, why = w if w else (b.index[-1], float(b.Close.iloc[-1]), "סוף יום")
            pct, r = result(p, px)
            rows.append(dict(date=str(day), symbol=s, side=sd, entry=p["entry"],
                             exit=px, reason=why, pct=pct, r=r))
    return pd.DataFrame(rows)


def brief(df):
    if len(df) == 0:
        return "אין עסקאות"
    w, l = df[df.pct > 0], df[df.pct <= 0]
    pf = w.pct.sum() / abs(l.pct.sum()) if len(l) and l.pct.sum() != 0 else float("inf")
    return f"{len(df)} עסקאות | {len(w) / len(df) * 100:.0f}% הצלחה | {df.pct.mean():+.2f}% לעסקה | PF {pf:.2f}"


def backtest():
    """משווה גרסאות. אימון: 40 הימים הראשונים. מבחן: הימים שאחריהם."""
    d, m, days, picks = load_bt()
    if len(days) < 20:
        notify("אין מספיק נתונים לבדיקה.")
        return
    cut = str(days[min(40, len(days) - 10)])
    out, allrows = [f"🔬 השוואת גרסאות, {len(days)} ימי מסחר\nאימון: עד {cut} | מבחן: מ-{cut}\nPF מעל 1 = רווח"], []
    for name, ch in VARIANTS.items():
        df = simulate(m, days, picks, {**P0, **ch})
        if len(df):
            df["variant"] = name
            allrows.append(df)
            out.append(f"{name}\nאימון: {brief(df[df.date < cut])}\nמבחן: {brief(df[df.date >= cut])}")
        else:
            out.append(f"{name}\nאין עסקאות")
    if allrows:
        pd.concat(allrows).to_csv("backtest_results.csv", index=False)
    notify("\n\n".join(out))


if __name__ == "__main__":
    {"live": live, "backtest": backtest, "test": test}[sys.argv[1] if len(sys.argv) > 1 else "live"]()
