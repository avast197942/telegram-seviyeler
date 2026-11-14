#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kural bazlı günlük (1D) işlem seviyeleri — Telegram.

Yapay zekâ yok. Kağıt işlem / araştırma amaçlıdır, yatırım tavsiyesi değildir.
Tüm giriş, SL, TP ve iğne seviyeleri kural bazlı ve tahmini.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Istanbul")
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
YAHOO_UA = "Mozilla/5.0 (compatible; levels-bot/1.0; research)"

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CHAT_ID_FILE = DATA_DIR / "chat_id"
ENV_CANDIDATES = [
    Path.home() / ".config" / "telegram-levels" / "env",
    ROOT / ".env",
]

TOKEN_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
FALLBACK_PRIVATE_CHAT_ID = "680205596"

log = logging.getLogger("levels_bot")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_env_files() -> None:
    """Load KEY=VALUE from  env files without overriding already-set vars."""
    for path in ENV_CANDIDATES:
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'").strip('"')
                if key and key not in os.environ:
                    os.environ[key] = val
        except OSError as exc:
            log.warning("Env dosyası okunamadı (%s): %s", path, exc)


def get_token() -> str:
    raw = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    found = TOKEN_RE.findall(raw)
    return found[-1] if found else raw.strip()


def redact(text: str, token: str = "") -> str:
    tok = token or get_token()
    if tok:
        text = text.replace(tok, "<redacted>")
    return text


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(sh)
    try:
        fh = logging.FileHandler(LOG_DIR / "levels_bot.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass


def now_tsi() -> datetime:
    return datetime.now(TZ)


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


MONTHS_TR = {
    1: "Oca", 2: "Şub", 3: "Mar", 4: "Nis", 5: "May", 6: "Haz",
    7: "Tem", 8: "Ağu", 9: "Eyl", 10: "Eki", 11: "Kas", 12: "Ara",
}


def fmt_when(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    local = dt.astimezone(TZ)
    return f"{local.day} {MONTHS_TR[local.month]} {local.year} {local:%H:%M} TSİ"


def fmt_day(d: date) -> str:
    return f"{d.day} {MONTHS_TR[d.month]} {d.year}"


def fmt_price(value: float, kind: str) -> str:
    if value is None or (isinstance(value, float) and (value != value)):  # NaN
        return "—"
    if kind == "EURUSD":
        return f"{value:.5f}"
    if kind == "DXY":
        return f"{value:.2f}"
    if kind == "XAUUSD":
        return f"{value:,.2f}"
    if kind == "US100":
        return f"{value:,.2f}"
    if kind == "BTC":
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def fmt_pct(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def last_completed_trading_week(asof: date) -> tuple[date, date]:
    """Son tamamlanmış işlem haftası (Pazartesi–Cuma).

    Cumartesi/Pazar: içinde bulunulan haftanın Pzt–Cuma'sı tamamdır.
    Hafta içi: bir önceki Pzt–Cuma.
    """
    wd = asof.isoweekday()  # 1=Mon … 7=Sun
    if wd >= 6:
        monday = asof - timedelta(days=wd - 1)
    else:
        monday = asof - timedelta(days=wd - 1 + 7)
    friday = monday + timedelta(days=4)
    return monday, friday


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    d: date
    o: float
    h: float
    l: float
    c: float


@dataclass
class Pivots:
    p: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float


@dataclass
class Pool:
    lo: float
    hi: float
    name: str

    @property
    def mid(self) -> float:
        return (self.lo + self.hi) / 2.0


@dataclass
class InstrumentResult:
    key: str
    title: str
    source_note: str
    show_dxy: bool
    price: float
    change_pct: float
    move_threshold: float
    pivots: Pivots
    prev_high: float
    prev_low: float
    week_high: float
    week_low: float
    week_range: str
    swing20_high: float
    swing20_low: float
    sma20: Optional[float]
    sma50: Optional[float]
    bias: str
    bias_reason: str
    igne_up: list[Pool] = field(default_factory=list)
    igne_down: list[Pool] = field(default_factory=list)
    entry_lo: Optional[float] = None
    entry_hi: Optional[float] = None
    sl: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    move_alert: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def http_json(url: str, payload: Optional[dict] = None, timeout: int = 25, headers: Optional[dict] = None) -> object:
    hdrs = {"User-Agent": YAHOO_UA, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_yahoo_daily(ticker: str, period: str = "6mo") -> list[Bar]:
    import pandas as pd
    import yfinance as yf

    df = yf.download(
        ticker,
        period=period,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo {ticker}: boş veri")
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    needed = ["Open", "High", "Low", "Close"]
    missing = [c for c in needed if c not in df.columns]
    if missing:
        raise RuntimeError(f"Yahoo {ticker}: kolon yok {missing}")
    df = df.dropna(subset=needed)
    bars: list[Bar] = []
    for idx, row in df.iterrows():
        ts = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if getattr(ts, "tzinfo", None) is not None:
            d = ts.date()
        else:
            d = ts.date() if hasattr(ts, "date") else date.fromisoformat(str(idx)[:10])
        bars.append(
            Bar(
                d=d,
                o=float(row["Open"]),
                h=float(row["High"]),
                l=float(row["Low"]),
                c=float(row["Close"]),
            )
        )
    if len(bars) < 25:
        raise RuntimeError(f"Yahoo {ticker}: yetersiz bar ({len(bars)})")
    return bars


def fetch_hl_btc_daily(lookback_days: int = 120) -> list[Bar]:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_days * 86400 * 1000
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": "BTC",
            "interval": "1d",
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    data = http_json(HL_INFO_URL, payload=payload, timeout=25)
    if not isinstance(data, list) or not data:
        raise RuntimeError("Hyperliquid BTC: boş mum verisi")
    bars: list[Bar] = []
    for row in data:
        t_ms = int(row["t"])
        d = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc).date()
        bars.append(
            Bar(
                d=d,
                o=float(row["o"]),
                h=float(row["h"]),
                l=float(row["l"]),
                c=float(row["c"]),
            )
        )
    bars.sort(key=lambda b: b.d)
    if len(bars) < 25:
        raise RuntimeError(f"Hyperliquid BTC: yetersiz bar ({len(bars)})")
    return bars


def fetch_calendar_high_usd_eur(today: date) -> list[dict]:
    """Forex Factory haftalık JSON; yalnızca bugünkü USD/EUR High. Hata olursa boş liste."""
    try:
        data = http_json(
            FF_CALENDAR_URL,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 levels-bot/1.0"},
        )
    except Exception as exc:
        log.warning("Ekonomik takvim alınamadı, 'Dikkat' satırı atlanacak: %s", exc)
        return []
    if not isinstance(data, list):
        log.warning("Ekonomik takvim beklenmeyen biçimde, atlanıyor")
        return []
    events = []
    for ev in data:
        if ev.get("impact") != "High":
            continue
        if ev.get("country") not in ("USD", "EUR"):
            continue
        raw = ev.get("date") or ""
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(TZ)
        except Exception:
            continue
        if local.date() != today:
            continue
        events.append(
            {
                "title": ev.get("title") or "Veri",
                "country": ev.get("country"),
                "when": local,
                "forecast": (ev.get("forecast") or "").strip(),
            }
        )
    events.sort(key=lambda e: e["when"])
    return events


# ---------------------------------------------------------------------------
# Calculations
# ---------------------------------------------------------------------------

def classic_pivots(h: float, l: float, c: float) -> Pivots:
    p = (h + l + c) / 3.0
    r1 = 2 * p - l
    s1 = 2 * p - h
    r2 = p + (h - l)
    s2 = p - (h - l)
    r3 = h + 2 * (p - l)
    s3 = l - 2 * (h - p)
    return Pivots(p=p, r1=r1, r2=r2, r3=r3, s1=s1, s2=s2, s3=s3)


def sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rel_tol(kind: str) -> float:
    if kind == "EURUSD":
        return 0.0008  # ~8 pip / fiyat
    if kind == "BTC":
        return 0.0020
    return 0.0015  # XAU, US100


def abs_tol(kind: str, price: float) -> float:
    if kind == "EURUSD":
        return 0.00080
    return price * rel_tol(kind)


def cluster_prices(values: list[float], tol: float) -> list[tuple[float, float, int]]:
    if not values:
        return []
    vs = sorted(values)
    clusters: list[list[float]] = [[vs[0]]]
    for v in vs[1:]:
        cur = clusters[-1]
        ref = sum(cur) / len(cur)
        if abs(v - ref) <= tol or abs(v - cur[-1]) <= tol:
            cur.append(v)
        else:
            clusters.append([v])
    return [(min(c), max(c), len(c)) for c in clusters if len(c) >= 2]


def fractal_swings(bars: list[Bar], left: int = 2, right: int = 2) -> tuple[list[float], list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    n = len(bars)
    for i in range(left, n - right):
        h = bars[i].h
        l = bars[i].l
        if all(h >= bars[j].h for j in range(i - left, i + right + 1) if j != i):
            highs.append(h)
        if all(l <= bars[j].l for j in range(i - left, i + right + 1) if j != i):
            lows.append(l)
    return highs, lows


def near(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def merge_pool(existing: list[Pool], lo: float, hi: float, name: str, tol: float) -> None:
    if lo > hi:
        lo, hi = hi, lo
    for p in existing:
        if abs(p.mid - (lo + hi) / 2) <= tol or not (hi < p.lo - tol or lo > p.hi + tol):
            p.lo = min(p.lo, lo)
            p.hi = max(p.hi, hi)
            if name not in p.name:
                p.name = f"{p.name}, {name}"
            return
    existing.append(Pool(lo=lo, hi=hi, name=name))


def sl_buffer(kind: str, prev_range: float, price: float) -> float:
    if kind == "EURUSD":
        return max(0.00040, 0.10 * prev_range)
    frac = 0.10 * prev_range
    pct = 0.0015 * price if kind != "BTC" else 0.0025 * price
    return max(frac, pct)


def select_ref_bars(bars: list[Bar], asof: date, utc_session: bool) -> tuple[Bar, Bar, bool]:
    """Return (last_bar, pivot_source_bar, last_is_in_progress)."""
    last = bars[-1]
    prev = bars[-2]
    if utc_session:
        today = datetime.now(timezone.utc).date()
    else:
        today = asof
    in_progress = last.d >= today
    pivot_bar = prev if in_progress else last
    # önceki gün for display: the completed day used for pivots
    return last, pivot_bar, in_progress


def compute_instrument(
    key: str,
    title: str,
    source_note: str,
    bars: list[Bar],
    asof: date,
    show_dxy: bool,
    move_threshold: float,
    utc_session: bool = False,
) -> InstrumentResult:
    last, pivot_bar, in_progress = select_ref_bars(bars, asof, utc_session)
    # For "önceki gün" H/L: the completed day that produced today's (or next session's) pivots
    # is pivot_bar when last is in progress (yesterday). When last is complete, pivot_bar
    # is last itself (next-session pivots), and önceki gün is that completed day.
    prev_for_change = bars[-2]
    price = last.c
    change_pct = (last.c / prev_for_change.c - 1.0) * 100.0 if prev_for_change.c else 0.0

    piv = classic_pivots(pivot_bar.h, pivot_bar.l, pivot_bar.c)
    prev_high, prev_low = pivot_bar.h, pivot_bar.l

    week_start, week_end = last_completed_trading_week(asof if not utc_session else last.d)
    week_bars = [b for b in bars if week_start <= b.d <= week_end]
    if not week_bars:
        # fallback: last 5 completed bars before last (or before pivot)
        week_bars = bars[-6:-1] if len(bars) > 6 else bars[:-1]
        week_start, week_end = week_bars[0].d, week_bars[-1].d
    week_high = max(b.h for b in week_bars)
    week_low = min(b.l for b in week_bars)
    week_range = f"{fmt_day(week_start)}–{fmt_day(week_end)}"

    window20 = bars[-20:]
    swing20_high = max(b.h for b in window20)
    swing20_low = min(b.l for b in window20)

    closes = [b.c for b in bars]
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)

    # Bias: AL only if close > SMA20 and SMA50 (if present) and pivot; SAT only if all below; else BEKLE
    above_p = price > piv.p
    below_p = price < piv.p
    above_s20 = s20 is not None and price > s20
    below_s20 = s20 is not None and price < s20
    if s50 is not None:
        above_s50 = price > s50
        below_s50 = price < s50
    else:
        above_s50 = above_s20
        below_s50 = below_s20

    if above_p and above_s20 and above_s50:
        bias = "AL"
        parts = ["kapanış pivotun üstünde"]
        parts.append("20 günlük ortalamanın üstünde")
        if s50 is not None:
            parts.append("50 günlük ortalamanın üstünde")
        bias_reason = " ve ".join(parts)
    elif below_p and below_s20 and below_s50:
        bias = "SAT"
        parts = ["kapanış pivotun altında"]
        parts.append("20 günlük ortalamanın altında")
        if s50 is not None:
            parts.append("50 günlük ortalamanın altında")
        bias_reason = " ve ".join(parts)
    else:
        bias = "BEKLE"
        bits = []
        if s20 is not None:
            bits.append("SMA20 " + ("üstünde" if above_s20 else "altında"))
        if s50 is not None:
            bits.append("SMA50 " + ("üstünde" if above_s50 else "altında"))
        bits.append("pivotun " + ("üstünde" if above_p else "altında" if below_p else "üzerinde"))
        bias_reason = "sinyaller karışık (" + ", ".join(bits) + "); kayıp riski için BEKLE"

    tol = abs_tol(key, price)
    pools: list[Pool] = []
    merge_pool(pools, prev_high, prev_high, "önceki gün tepe", tol)
    merge_pool(pools, prev_low, prev_low, "önceki gün dip", tol)
    merge_pool(pools, week_high, week_high, "önceki hafta tepe", tol)
    merge_pool(pools, week_low, week_low, "önceki hafta dip", tol)
    merge_pool(pools, swing20_high, swing20_high, "20g salınım tepe", tol)
    merge_pool(pools, swing20_low, swing20_low, "20g salınım dip", tol)

    lookback = bars[-40:] if len(bars) >= 40 else bars
    eq_h = cluster_prices([b.h for b in lookback], tol)
    eq_l = cluster_prices([b.l for b in lookback], tol)
    for lo, hi, n in eq_h:
        merge_pool(pools, lo, hi, f"eşit tepeler ({n} dokunuş)", tol)
    for lo, hi, n in eq_l:
        merge_pool(pools, lo, hi, f"eşit dipler ({n} dokunuş)", tol)

    sw_h, sw_l = fractal_swings(lookback, left=2, right=2)
    for h in sw_h[-6:]:
        merge_pool(pools, h, h, "salınım tepe", tol)
    for l_ in sw_l[-6:]:
        merge_pool(pools, l_, l_, "salınım dip", tol)

    igne_up = sorted([p for p in pools if p.mid > price + tol], key=lambda p: p.lo)
    igne_down = sorted([p for p in pools if p.mid < price - tol], key=lambda p: p.hi, reverse=True)
    igne_up = igne_up[:2]
    igne_down = igne_down[:2]

    entry_lo = entry_hi = sl = tp1 = tp2 = None
    prev_range = max(pivot_bar.h - pivot_bar.l, abs_tol(key, price))
    buf = sl_buffer(key, prev_range, price)
    entry_pad = max(buf, abs_tol(key, price))

    if bias == "AL":
        nearest_below = igne_down[0].hi if igne_down else (piv.s1 if piv.s1 < price else piv.s2)
        entry_hi = price
        entry_lo = max(nearest_below + buf, price - 2 * entry_pad)
        if entry_lo >= entry_hi:
            entry_lo = price - entry_pad
        sl = nearest_below - buf
        ups = [x for x in (piv.r1, piv.r2, piv.r3) if x > price + buf]
        tp1 = ups[0] if ups else None
        tp2 = ups[1] if len(ups) > 1 else None
    elif bias == "SAT":
        nearest_above = igne_up[0].lo if igne_up else (piv.r1 if piv.r1 > price else piv.r2)
        entry_lo = price
        entry_hi = min(nearest_above - buf, price + 2 * entry_pad)
        if entry_hi <= entry_lo:
            entry_hi = price + entry_pad
        sl = nearest_above + buf
        downs = [x for x in (piv.s1, piv.s2, piv.s3) if x < price - buf]
        tp1 = downs[0] if downs else None
        tp2 = downs[1] if len(downs) > 1 else None

    return InstrumentResult(
        key=key,
        title=title,
        source_note=source_note,
        show_dxy=show_dxy,
        price=price,
        change_pct=change_pct,
        move_threshold=move_threshold,
        pivots=piv,
        prev_high=prev_high,
        prev_low=prev_low,
        week_high=week_high,
        week_low=week_low,
        week_range=week_range,
        swing20_high=swing20_high,
        swing20_low=swing20_low,
        sma20=s20,
        sma50=s50,
        bias=bias,
        bias_reason=bias_reason,
        igne_up=igne_up,
        igne_down=igne_down,
        entry_lo=entry_lo,
        entry_hi=entry_hi,
        sl=sl,
        tp1=tp1,
        tp2=tp2,
        move_alert=abs(change_pct) >= move_threshold,
    )


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------

def fmt_pool(pool: Pool, kind: str) -> str:
    if near(pool.lo, pool.hi, abs_tol(kind, pool.mid) * 0.25):
        level = fmt_price((pool.lo + pool.hi) / 2.0, kind)
    else:
        level = f"{fmt_price(pool.lo, kind)}–{fmt_price(pool.hi, kind)}"
    return f"{level} ({pool.name})"


def format_instrument(res: InstrumentResult, dxy_line: Optional[str]) -> str:
    k = res.key
    if res.error:
        return f"<b>{html_escape(res.title)}</b>\nVeri alınamadı: {html_escape(res.error)}"

    lines: list[str] = []
    alert = ""
    if res.move_alert:
        alert = f"  🚩 günlük hareket {fmt_pct(res.change_pct)} (eşik {res.move_threshold:.0f}%)"
    lines.append(
        f"<b>{html_escape(res.title)}</b>  {fmt_price(res.price, k)}  ({fmt_pct(res.change_pct)}){alert}"
    )
    lines.append(f"Kaynak: {html_escape(res.source_note)}")
    if res.show_dxy and dxy_line:
        lines.append(dxy_line)
    p = res.pivots
    lines.append(
        "Pivot: P {p} | R1 {r1} | R2 {r2} | S1 {s1} | S2 {s2}".format(
            p=fmt_price(p.p, k),
            r1=fmt_price(p.r1, k),
            r2=fmt_price(p.r2, k),
            s1=fmt_price(p.s1, k),
            s2=fmt_price(p.s2, k),
        )
    )
    lines.append(
        f"Önceki gün: H {fmt_price(res.prev_high, k)} / L {fmt_price(res.prev_low, k)}"
    )
    lines.append(
        f"Önceki hafta ({res.week_range}): H {fmt_price(res.week_high, k)} / L {fmt_price(res.week_low, k)}"
    )
    lines.append(
        f"20g salınım: H {fmt_price(res.swing20_high, k)} / L {fmt_price(res.swing20_low, k)}"
    )
    if res.igne_up:
        lines.append("Muhtemel iğne üst: " + "; ".join(fmt_pool(x, k) for x in res.igne_up))
    else:
        lines.append("Muhtemel iğne üst: belirgin havuz yok")
    if res.igne_down:
        lines.append("Muhtemel iğne alt: " + "; ".join(fmt_pool(x, k) for x in res.igne_down))
    else:
        lines.append("Muhtemel iğne alt: belirgin havuz yok")

    lines.append(f"Yön (kural): <b>{res.bias}</b> — {html_escape(res.bias_reason)}.")
    if res.bias in ("AL", "SAT") and res.entry_lo is not None and res.sl is not None:
        ent = f"{fmt_price(res.entry_lo, k)}–{fmt_price(res.entry_hi, k)}"
        tps = []
        if res.tp1 is not None:
            tps.append(f"TP1 {fmt_price(res.tp1, k)}")
        if res.tp2 is not None:
            tps.append(f"TP2 {fmt_price(res.tp2, k)}")
        tp_txt = " | " + " | ".join(tps) if tps else ""
        lines.append(
            f"Giriş (tahmini): {ent} | SL {fmt_price(res.sl, k)}{tp_txt}"
        )
        lines.append("Bu seviyeler kural bazlı, tahmini, kağıt işlemdir.")
    return "\n".join(lines)


def format_dxy_line(bars: Optional[list[Bar]], err: Optional[str]) -> str:
    if err or not bars:
        return "DXY durumu: veri yok"
    last = bars[-1]
    prev = bars[-2] if len(bars) > 1 else last
    chg = (last.c / prev.c - 1.0) * 100.0 if prev.c else 0.0
    return f"DXY durumu: {fmt_price(last.c, 'DXY')} ({fmt_pct(chg)}) — kaynak Yahoo DX-Y.NYB"


def format_attention(events: list[dict]) -> str:
    if not events:
        return ""
    bits = []
    for ev in events:
        t = ev["when"].strftime("%H:%M")
        fc = f", beklenti {ev['forecast']}" if ev["forecast"] else ""
        bits.append(f"{ev['country']} {ev['title']} {t} TSİ{fc}")
    return "⚠️ <b>Dikkat:</b> Bugün yüksek etkili ABD/Avrupa verisi: " + "; ".join(bits) + "."


def build_message(
    results: list[InstrumentResult],
    dxy_line: str,
    events: list[dict],
    generated_at: datetime,
) -> str:
    parts: list[str] = []
    parts.append(f"<b>Günlük seviyeler (1D)</b>")
    parts.append(fmt_when(generated_at))
    parts.append("Kağıt işlem / araştırma. Seviyeler kural bazlı, tahmini. Yatırım tavsiyesi değildir.")
    att = format_attention(events)
    if att:
        parts.append("")
        parts.append(att)
    for res in results:
        parts.append("")
        parts.append(format_instrument(res, dxy_line if res.show_dxy else None))
    parts.append("")
    parts.append(
        "Kaynaklar: XAUUSD Yahoo GC=F (altın vadelisi, spot değil); "
        "US100 Yahoo NQ=F (Nasdaq-100 vadelisi, yoksa ^NDX); "
        "EURUSD Yahoo EURUSD=X; BTC Hyperliquid public /info candleSnapshot 1d (UTC); "
        "DXY Yahoo DX-Y.NYB; takvim Forex Factory haftalık JSON (USD/EUR High)."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_call(method: str, payload: Optional[dict] = None, token: Optional[str] = None) -> dict:
    tok = token or get_token()
    if not tok:
        raise RuntimeError("TELEGRAM_BOT_TOKEN tanımlı değil")
    url = f"https://api.telegram.org/bot{tok}/{method}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        raise RuntimeError(redact(f"Telegram HTTP {exc.code}: {raw[:500]}", tok)) from None
    except Exception as exc:
        raise RuntimeError(redact(str(exc), tok)) from None
    if not data.get("ok"):
        raise RuntimeError(redact(f"Telegram {method} başarısız: {data}", tok))
    return data


def persist_chat_id(chat_id: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CHAT_ID_FILE.write_text(str(chat_id).strip() + "\n", encoding="utf-8")
    try:
        CHAT_ID_FILE.chmod(0o600)
    except OSError:
        pass
    log.info("Chat id kaydedildi: %s", chat_id)


def chat_from_updates() -> Optional[str]:
    """Prefer a supergroup/channel (bot added), else first private chat, else channel_post."""
    try:
        data = tg_call("getUpdates")
    except Exception as exc:
        log.warning("getUpdates başarısız: %s", exc)
        return None
    updates = data.get("result") or []
    groups: list[str] = []
    privates: list[str] = []
    channels: list[str] = []
    for upd in updates:
        for key in ("message", "channel_post", "my_chat_member", "chat_member"):
            obj = upd.get(key) or {}
            chat = obj.get("chat") or {}
            cid = chat.get("id")
            typ = chat.get("type")
            if cid is None or typ is None:
                continue
            sid = str(cid)
            if typ in ("supergroup", "group") and sid not in groups:
                groups.append(sid)
            elif typ == "channel" and sid not in channels:
                channels.append(sid)
            elif typ == "private" and sid not in privates:
                privates.append(sid)
            elif key == "channel_post" and sid not in channels:
                channels.append(sid)
    if groups:
        log.info("getUpdates: grup/supergroup bulundu: %s", groups[-1])
        return groups[-1]
    if channels:
        log.info("getUpdates: kanal bulundu: %s", channels[-1])
        return channels[-1]
    if privates:
        log.info("getUpdates: özel sohbet bulundu: %s", privates[-1])
        return privates[-1]
    return None


def resolve_chat_id() -> Optional[str]:
    env_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if env_id:
        log.info("Chat id ortam değişkeninden alındı: %s", env_id)
        persist_chat_id(env_id)
        return env_id
    if CHAT_ID_FILE.is_file():
        saved = CHAT_ID_FILE.read_text(encoding="utf-8").strip()
        if saved:
            log.info("Chat id kayıtlı dosyadan alındı: %s", saved)
            return saved
    found = chat_from_updates()
    if found:
        persist_chat_id(found)
        return found
    return None


def send_message(chat_id: str, text: str) -> list[int]:
    ids: list[int] = []
    # Telegram limit 4096; split on double newlines if needed
    chunks: list[str] = []
    if len(text) <= 4000:
        chunks = [text]
    else:
        buf = []
        size = 0
        for block in text.split("\n\n"):
            add = (2 if buf else 0) + len(block)
            if size + add > 3900 and buf:
                chunks.append("\n\n".join(buf))
                buf, size = [block], len(block)
            else:
                buf.append(block)
                size += add
        if buf:
            chunks.append("\n\n".join(buf))
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        data = tg_call("sendMessage", payload)
        mid = (data.get("result") or {}).get("message_id")
        if mid is not None:
            ids.append(int(mid))
        log.info("Mesaj gönderildi (%s/%s), message_id=%s, chat_id=%s", i + 1, len(chunks), mid, chat_id)
    return ids


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

INSTRUMENT_PLAN = [
    {
        "key": "XAUUSD",
        "title": "XAUUSD",
        "tickers": ["GC=F"],
        "source": "Yahoo GC=F (COMEX altın vadelisi; spot XAUUSD Yahoo'da yok)",
        "show_dxy": True,
        "move_threshold": 2.0,
        "utc": False,
    },
    {
        "key": "US100",
        "title": "US100",
        "tickers": ["NQ=F", "^NDX"],
        "source": "Yahoo NQ=F (Nasdaq-100 vadelisi; yedek ^NDX nakit endeks)",
        "show_dxy": False,
        "move_threshold": 2.0,
        "utc": False,
    },
    {
        "key": "EURUSD",
        "title": "EURUSD",
        "tickers": ["EURUSD=X"],
        "source": "Yahoo EURUSD=X",
        "show_dxy": True,
        "move_threshold": 2.0,
        "utc": False,
    },
    {
        "key": "BTC",
        "title": "BTC",
        "tickers": [],
        "source": "Hyperliquid mainnet /info candleSnapshot, coin BTC, interval 1d (UTC günü)",
        "show_dxy": True,
        "move_threshold": 4.0,
        "utc": True,
    },
]


def load_instrument(plan: dict, asof: date) -> InstrumentResult:
    key = plan["key"]
    try:
        used = ""
        if key == "BTC":
            bars = fetch_hl_btc_daily()
            used = plan["source"]
        else:
            last_err = None
            bars = None
            for ticker in plan["tickers"]:
                try:
                    bars = fetch_yahoo_daily(ticker)
                    used = f"Yahoo {ticker}" + (
                        "" if ticker == plan["tickers"][0] else f" (yedek; {plan['source']})"
                    )
                    if ticker != plan["tickers"][0]:
                        used = f"Yahoo {ticker} (birincil {plan['tickers'][0]} alınamadı)"
                    else:
                        used = plan["source"] if ticker in plan["source"] else f"Yahoo {ticker}"
                    break
                except Exception as exc:
                    last_err = exc
                    log.warning("%s için %s alınamadı: %s", key, ticker, exc)
            if bars is None:
                raise last_err or RuntimeError("veri yok")
        return compute_instrument(
            key=key,
            title=plan["title"],
            source_note=used,
            bars=bars,
            asof=asof,
            show_dxy=plan["show_dxy"],
            move_threshold=plan["move_threshold"],
            utc_session=plan["utc"],
        )
    except Exception as exc:
        log.exception("%s hesaplanamadı", key)
        return InstrumentResult(
            key=key,
            title=plan["title"],
            source_note=plan["source"],
            show_dxy=plan["show_dxy"],
            price=0.0,
            change_pct=0.0,
            move_threshold=plan["move_threshold"],
            pivots=Pivots(0, 0, 0, 0, 0, 0, 0),
            prev_high=0,
            prev_low=0,
            week_high=0,
            week_low=0,
            week_range="",
            swing20_high=0,
            swing20_low=0,
            sma20=None,
            sma50=None,
            bias="BEKLE",
            bias_reason="veri yok",
            error=str(exc),
        )


def run(dry_run: bool) -> int:
    generated_at = now_tsi()
    asof = generated_at.date()
    log.info("Hesaplama başladı (%s)", fmt_when(generated_at))

    dxy_bars = None
    dxy_err = None
    try:
        dxy_bars = fetch_yahoo_daily("DX-Y.NYB")
    except Exception as exc:
        dxy_err = str(exc)
        log.warning("DXY alınamadı: %s", exc)
    dxy_line = format_dxy_line(dxy_bars, dxy_err)

    events = fetch_calendar_high_usd_eur(asof)

    results = [load_instrument(plan, asof) for plan in INSTRUMENT_PLAN]
    ok = sum(1 for r in results if not r.error)
    if ok == 0:
        log.error("Hiçbir enstrüman verisi alınamadı; mesaj gönderilmeyecek.")
        if dry_run:
            print("HATA: Hiçbir enstrüman verisi alınamadı.", file=sys.stderr)
        return 1

    text = build_message(results, dxy_line, events, generated_at)
    print(text)
    print("", flush=True)

    if dry_run:
        log.info("Dry-run: Telegram'a gönderilmedi.")
        return 0

    if not get_token():
        log.error(
            "TELEGRAM_BOT_TOKEN yok. Env dosyasından veya ortam değişkeninden okunmalı. Mesaj gönderilmedi."
        )
        return 2

    chat_id = resolve_chat_id()
    if not chat_id:
        log.error(
            "Chat id bulunamadı. Bot'a Telegram'dan bir mesaj yazın veya botu kanala/gruba ekleyin, "
            "sonra scripti yeniden çalıştırın; ya da TELEGRAM_CHAT_ID tanımlayın. Mesaj gönderilmedi."
        )
        return 0

    try:
        ids = send_message(chat_id, text)
        log.info("Gönderim tamam, message_id=%s", ids)
        return 0
    except Exception as exc:
        log.error("Gönderim başarısız: %s", exc)
        # fallback private chat only if we were targeting the group and it failed
        if str(chat_id) != FALLBACK_PRIVATE_CHAT_ID:
            log.info("Özel sohbete yedek gönderim deneniyor (%s)", FALLBACK_PRIVATE_CHAT_ID)
            try:
                ids = send_message(FALLBACK_PRIVATE_CHAT_ID, text)
                log.info("Yedek gönderim tamam, message_id=%s", ids)
                return 0
            except Exception as exc2:
                log.error("Yedek gönderim de başarısız: %s", exc2)
        return 1


def main(argv: Optional[list[str]] = None) -> int:
    setup_logging()
    load_env_files()
    parser = argparse.ArgumentParser(description="Kural bazlı günlük seviyeler (Telegram, yapay zekâ yok)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mesajı hesapla ve yazdır; Telegram'a gönderme (token gerekmez)",
    )
    args = parser.parse_args(argv)
    try:
        return run(dry_run=args.dry_run)
    except Exception:
        log.exception("Beklenmeyen hata")
        return 1


if __name__ == "__main__":
    sys.exit(main())
