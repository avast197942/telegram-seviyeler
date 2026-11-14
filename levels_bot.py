#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kural bazlı işlem seviyeleri — Telegram.

Yapay zekâ yok. Kağıt işlem / araştırma amaçlıdır, yatırım tavsiyesi değildir.
Tüm giriş, SL, TP ve iğne seviyeleri kural bazlı ve tahmini.
Mesajda gösterge adı (ortalama, bant, pivot) yazılmaz.
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
CRYPTO = {"BTC", "ETH", "XRP", "DOGE"}

log = logging.getLogger("levels_bot")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_env_files() -> None:
    """Load KEY=VALUE from env files without overriding already-set vars."""
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
    sh = logging.StreamHandler(sys.stderr)
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


def fmt_price(value: float, kind: str) -> str:
    if value is None or (isinstance(value, float) and (value != value)):
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
    if kind == "ETH":
        return f"{value:,.2f}"
    if kind == "XRP":
        return f"{value:.5f}"
    if kind == "DOGE":
        return f"{value:.6f}"
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


def prev_month_start(d: date) -> date:
    if d.month == 1:
        return date(d.year - 1, 12, 1)
    return date(d.year, d.month - 1, 1)


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
    informational: bool
    price: float
    change_pct: float
    move_threshold: float
    month_high: Optional[float]
    month_low: Optional[float]
    prev_month_high: Optional[float]
    prev_month_low: Optional[float]
    show_prev_month: bool
    week_high: float
    week_low: float
    prev_high: float
    prev_low: float
    bias: str
    igne_up: list[Pool] = field(default_factory=list)
    igne_down: list[Pool] = field(default_factory=list)
    entry: Optional[float] = None
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


def fetch_hl_daily(coin: str, lookback_days: int = 150) -> list[Bar]:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - lookback_days * 86400 * 1000
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": "1d",
            "startTime": start_ms,
            "endTime": end_ms,
        },
    }
    data = http_json(HL_INFO_URL, payload=payload, timeout=25)
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"Hyperliquid {coin}: boş mum verisi")
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
        raise RuntimeError(f"Hyperliquid {coin}: yetersiz bar ({len(bars)})")
    return bars


def fetch_calendar_high_usd_eur(today: date) -> list[dict]:
    """Forex Factory haftalık JSON; yalnızca bugünkü USD/EUR High (3 yıldız). Hata olursa boş liste."""
    try:
        data = http_json(
            FF_CALENDAR_URL,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0 levels-bot/1.0"},
        )
    except Exception as exc:
        log.warning("Ekonomik takvim alınamadı, 3★ satırı atlanacak: %s", exc)
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
            }
        )
    events.sort(key=lambda e: e["when"])
    return events


# ---------------------------------------------------------------------------
# Calculations (internal rules; names are not printed)
# ---------------------------------------------------------------------------

def classic_pivot(h: float, l: float, c: float) -> float:
    return (h + l + c) / 3.0


def sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rel_tol(kind: str) -> float:
    if kind == "EURUSD":
        return 0.0008
    if kind == "DXY":
        return 0.0012
    if kind in CRYPTO:
        return 0.0025 if kind in ("XRP", "DOGE") else 0.0020
    return 0.0015


def abs_tol(kind: str, price: float) -> float:
    if kind == "EURUSD":
        return 0.00080
    if kind == "DXY":
        return 0.12
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
    """Tight buffer just beyond the iğne zone."""
    if kind == "EURUSD":
        return max(0.00025, min(0.00060, 0.06 * prev_range))
    if kind == "DXY":
        return max(0.04, min(0.12, 0.06 * prev_range))
    if kind in ("XRP", "DOGE"):
        return max(price * 0.0020, min(price * 0.0040, 0.06 * prev_range))
    if kind in ("BTC", "ETH"):
        return max(price * 0.0012, min(price * 0.0030, 0.06 * prev_range))
    return max(price * 0.0007, min(price * 0.0020, 0.06 * prev_range))


def select_ref_bars(bars: list[Bar], asof: date, utc_session: bool) -> tuple[Bar, Bar, bool]:
    """Return (last_bar, completed_day_bar, last_is_in_progress)."""
    last = bars[-1]
    prev = bars[-2]
    today = datetime.now(timezone.utc).date() if utc_session else asof
    in_progress = last.d >= today
    completed = prev if in_progress else last
    return last, completed, in_progress


def month_hl(bars: list[Bar], year: int, month: int) -> tuple[Optional[float], Optional[float]]:
    mb = [b for b in bars if b.d.year == year and b.d.month == month]
    if not mb:
        return None, None
    return max(b.h for b in mb), min(b.l for b in mb)


def unique_targets(levels: list[float], entry: float, direction: str, tol: float, buf: float) -> list[float]:
    out: list[float] = []
    for lv in levels:
        if direction == "AL" and lv <= entry + buf:
            continue
        if direction == "SAT" and lv >= entry - buf:
            continue
        if any(near(lv, x, tol) for x in out):
            continue
        out.append(lv)
    return out


def compute_instrument(
    key: str,
    title: str,
    source_note: str,
    bars: list[Bar],
    asof: date,
    move_threshold: float,
    informational: bool = False,
    utc_session: bool = False,
) -> InstrumentResult:
    last, completed, _in_progress = select_ref_bars(bars, asof, utc_session)
    prev_for_change = bars[-2]
    price = last.c
    change_pct = (last.c / prev_for_change.c - 1.0) * 100.0 if prev_for_change.c else 0.0

    # Internal reference only (not printed): classic central level of the completed day
    mid = classic_pivot(completed.h, completed.l, completed.c)
    prev_high, prev_low = completed.h, completed.l

    ref_date = last.d if utc_session else asof
    week_start, week_end = last_completed_trading_week(ref_date)
    week_bars = [b for b in bars if week_start <= b.d <= week_end]
    if not week_bars:
        week_bars = bars[-6:-1] if len(bars) > 6 else bars[:-1]
    week_high = max(b.h for b in week_bars)
    week_low = min(b.l for b in week_bars)

    month_high, month_low = month_hl(bars, ref_date.year, ref_date.month)
    pm = prev_month_start(ref_date)
    prev_month_high, prev_month_low = month_hl(bars, pm.year, pm.month)
    show_prev_month = False
    if month_high is not None and prev_month_high is not None:
        # Previous month is useful if it extends the range, or current month is still thin
        current_bars = [b for b in bars if b.d.year == ref_date.year and b.d.month == ref_date.month]
        extends = prev_month_high > month_high + abs_tol(key, price) or (
            prev_month_low is not None and month_low is not None and prev_month_low < month_low - abs_tol(key, price)
        )
        show_prev_month = extends or len(current_bars) < 6
    elif prev_month_high is not None:
        show_prev_month = True

    closes = [b.c for b in bars]
    s20 = sma(closes, 20)
    s50 = sma(closes, 50)

    # Internal bias: AL only if close is above the 20- and 50-period averages and the
    # completed-day midpoint; SAT only if below all three; otherwise BEKLE.
    above_mid = price > mid
    below_mid = price < mid
    above_s20 = s20 is not None and price > s20
    below_s20 = s20 is not None and price < s20
    if s50 is not None:
        above_s50 = price > s50
        below_s50 = price < s50
    else:
        above_s50 = above_s20
        below_s50 = below_s20

    if above_mid and above_s20 and above_s50:
        bias = "AL"
    elif below_mid and below_s20 and below_s50:
        bias = "SAT"
    else:
        bias = "BEKLE"

    tol = abs_tol(key, price)
    pools: list[Pool] = []
    merge_pool(pools, prev_high, prev_high, "önceki gün tepe", tol)
    merge_pool(pools, prev_low, prev_low, "önceki gün dip", tol)
    merge_pool(pools, week_high, week_high, "önceki hafta tepe", tol)
    merge_pool(pools, week_low, week_low, "önceki hafta dip", tol)
    if month_high is not None and month_low is not None:
        merge_pool(pools, month_high, month_high, "aylık tepe", tol)
        merge_pool(pools, month_low, month_low, "aylık dip", tol)

    lookback = bars[-40:] if len(bars) >= 40 else bars
    eq_h = cluster_prices([b.h for b in lookback], tol)
    eq_l = cluster_prices([b.l for b in lookback], tol)
    for lo, hi, _n in eq_h:
        merge_pool(pools, lo, hi, "eşit tepeler", tol)
    for lo, hi, _n in eq_l:
        merge_pool(pools, lo, hi, "eşit dipler", tol)

    sw_h, sw_l = fractal_swings(lookback, left=2, right=2)
    for h in sw_h[-6:]:
        merge_pool(pools, h, h, "salınım tepe", tol)
    for l_ in sw_l[-6:]:
        merge_pool(pools, l_, l_, "salınım dip", tol)

    igne_up = sorted([p for p in pools if p.mid > price + tol], key=lambda p: p.lo)[:2]
    igne_down = sorted([p for p in pools if p.mid < price - tol], key=lambda p: p.hi, reverse=True)[:2]

    entry = sl = tp1 = tp2 = None
    prev_range = max(completed.h - completed.l, abs_tol(key, price))
    buf = sl_buffer(key, prev_range, price)
    tick = max(buf * 0.25, abs_tol(key, price) * 0.15)

    if bias == "AL":
        zone = igne_down[0] if igne_down else Pool(prev_low, prev_low, "önceki gün dip")
        entry = zone.hi + tick
        sl = zone.lo - buf
        if sl >= entry:
            sl = entry - buf
        highs = [prev_high, week_high]
        if month_high is not None:
            highs.append(month_high)
        if show_prev_month and prev_month_high is not None:
            highs.append(prev_month_high)
        tps = unique_targets(highs, entry, "AL", tol, buf)
        tp1 = tps[0] if tps else None
        tp2 = tps[1] if len(tps) > 1 else None
    elif bias == "SAT":
        zone = igne_up[0] if igne_up else Pool(prev_high, prev_high, "önceki gün tepe")
        entry = zone.lo - tick
        sl = zone.hi + buf
        if sl <= entry:
            sl = entry + buf
        lows = [prev_low, week_low]
        if month_low is not None:
            lows.append(month_low)
        if show_prev_month and prev_month_low is not None:
            lows.append(prev_month_low)
        tps = unique_targets(lows, entry, "SAT", tol, buf)
        tp1 = tps[0] if tps else None
        tp2 = tps[1] if len(tps) > 1 else None

    return InstrumentResult(
        key=key,
        title=title,
        source_note=source_note,
        informational=informational,
        price=price,
        change_pct=change_pct,
        move_threshold=move_threshold,
        month_high=month_high,
        month_low=month_low,
        prev_month_high=prev_month_high,
        prev_month_low=prev_month_low,
        show_prev_month=show_prev_month,
        week_high=week_high,
        week_low=week_low,
        prev_high=prev_high,
        prev_low=prev_low,
        bias=bias,
        igne_up=igne_up,
        igne_down=igne_down,
        entry=entry,
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


def format_month_line(res: InstrumentResult) -> str:
    k = res.key
    if res.month_high is None or res.month_low is None:
        if res.prev_month_high is not None and res.prev_month_low is not None:
            return (
                f"Aylık: üst {fmt_price(res.prev_month_high, k)} / "
                f"alt {fmt_price(res.prev_month_low, k)} (önceki ay)"
            )
        return "Aylık: veri yok"
    line = f"Aylık: üst {fmt_price(res.month_high, k)} / alt {fmt_price(res.month_low, k)}"
    if res.show_prev_month and res.prev_month_high is not None and res.prev_month_low is not None:
        line += (
            f" (önceki {fmt_price(res.prev_month_high, k)} / "
            f"{fmt_price(res.prev_month_low, k)})"
        )
    return line


def format_instrument(res: InstrumentResult) -> str:
    k = res.key
    if res.error:
        return f"<b>{html_escape(res.title)}</b>\nVeri yok"

    flags = []
    if res.move_alert:
        flags.append("🚩")
    if res.informational:
        flags.append("endeks, bilgi amaçlı")
    suffix = ("  " + " ".join(flags)) if flags else ""

    lines = [
        f"<b>{html_escape(res.title)}</b>  {fmt_price(res.price, k)}  ({fmt_pct(res.change_pct)}){suffix}",
        format_month_line(res),
        f"Haftalık: üst {fmt_price(res.week_high, k)} / alt {fmt_price(res.week_low, k)}",
        f"Günlük: üst {fmt_price(res.prev_high, k)} / alt {fmt_price(res.prev_low, k)}",
        "İğne üst: " + ("; ".join(fmt_pool(x, k) for x in res.igne_up) if res.igne_up else "yok"),
        "İğne alt: " + ("; ".join(fmt_pool(x, k) for x in res.igne_down) if res.igne_down else "yok"),
        f"Yön: <b>{res.bias}</b>",
    ]
    if res.bias in ("AL", "SAT") and res.entry is not None and res.sl is not None:
        bits = [f"Giriş {fmt_price(res.entry, k)}", f"SL {fmt_price(res.sl, k)}"]
        if res.tp1 is not None:
            bits.append(f"TP1 {fmt_price(res.tp1, k)}")
        if res.tp2 is not None:
            bits.append(f"TP2 {fmt_price(res.tp2, k)}")
        lines.append(" · ".join(bits))
    else:
        lines.append("İşlem yok")
    return "\n".join(lines)


def format_attention(events: list[dict]) -> list[str]:
    lines = []
    for ev in events:
        t = ev["when"].strftime("%H:%M")
        lines.append(f"⚠️ 3★ veri: {t} TSİ {ev['title']}")
    return lines


def build_message(
    results: list[InstrumentResult],
    events: list[dict],
    generated_at: datetime,
) -> str:
    parts: list[str] = [f"<b>Seviyeler</b> · {fmt_when(generated_at)}"]
    parts.extend(format_attention(events))
    for res in results:
        parts.append("")
        parts.append(format_instrument(res))
    parts.append("")
    parts.append("Kağıt işlem, kural bazlı, tahmini.")
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


def split_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]
    blocks = text.split("\n\n")
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for block in blocks:
        add = (2 if buf else 0) + len(block)
        if size + add > limit and buf:
            chunks.append("\n\n".join(buf))
            buf, size = [block], len(block)
        else:
            buf.append(block)
            size += add
    if buf:
        chunks.append("\n\n".join(buf))
    # If a single block is still too long, hard-split
    out: list[str] = []
    for ch in chunks:
        if len(ch) <= limit:
            out.append(ch)
            continue
        for i in range(0, len(ch), limit):
            out.append(ch[i : i + limit])
    return out


def send_message(chat_id: str, text: str) -> list[int]:
    ids: list[int] = []
    chunks = split_message(text)
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
        log.info(
            "Mesaj gönderildi (%s/%s), message_id=%s, chat_id=%s, chars=%s",
            i + 1,
            len(chunks),
            mid,
            chat_id,
            len(chunk),
        )
    return ids


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

INSTRUMENT_PLAN = [
    {
        "key": "XAUUSD",
        "title": "XAUUSD",
        "source": "yahoo",
        "tickers": ["GC=F"],
        "note": "Yahoo GC=F (COMEX altın vadelisi)",
        "informational": False,
        "move_threshold": 2.0,
        "utc": False,
    },
    {
        "key": "US100",
        "title": "US100",
        "source": "yahoo",
        "tickers": ["NQ=F", "^NDX"],
        "note": "Yahoo NQ=F (Nasdaq-100 vadelisi; yedek ^NDX)",
        "informational": False,
        "move_threshold": 2.0,
        "utc": False,
    },
    {
        "key": "EURUSD",
        "title": "EURUSD",
        "source": "yahoo",
        "tickers": ["EURUSD=X"],
        "note": "Yahoo EURUSD=X",
        "informational": False,
        "move_threshold": 2.0,
        "utc": False,
    },
    {
        "key": "DXY",
        "title": "DXY",
        "source": "yahoo",
        "tickers": ["DX-Y.NYB"],
        "note": "Yahoo DX-Y.NYB (dolar endeksi, işlem değil)",
        "informational": True,
        "move_threshold": 2.0,
        "utc": False,
    },
    {
        "key": "BTC",
        "title": "BTC",
        "source": "hl",
        "coin": "BTC",
        "note": "Hyperliquid candleSnapshot 1d BTC (UTC)",
        "informational": False,
        "move_threshold": 4.0,
        "utc": True,
    },
    {
        "key": "ETH",
        "title": "ETH",
        "source": "hl",
        "coin": "ETH",
        "note": "Hyperliquid candleSnapshot 1d ETH (UTC)",
        "informational": False,
        "move_threshold": 4.0,
        "utc": True,
    },
    {
        "key": "XRP",
        "title": "XRP",
        "source": "hl",
        "coin": "XRP",
        "note": "Hyperliquid candleSnapshot 1d XRP (UTC)",
        "informational": False,
        "move_threshold": 4.0,
        "utc": True,
    },
    {
        "key": "DOGE",
        "title": "DOGE",
        "source": "hl",
        "coin": "DOGE",
        "note": "Hyperliquid candleSnapshot 1d DOGE (UTC)",
        "informational": False,
        "move_threshold": 4.0,
        "utc": True,
    },
]


def empty_result(plan: dict, error: str) -> InstrumentResult:
    return InstrumentResult(
        key=plan["key"],
        title=plan["title"],
        source_note=plan["note"],
        informational=plan["informational"],
        price=0.0,
        change_pct=0.0,
        move_threshold=plan["move_threshold"],
        month_high=None,
        month_low=None,
        prev_month_high=None,
        prev_month_low=None,
        show_prev_month=False,
        week_high=0,
        week_low=0,
        prev_high=0,
        prev_low=0,
        bias="BEKLE",
        error=error,
    )


def load_instrument(plan: dict, asof: date) -> InstrumentResult:
    key = plan["key"]
    try:
        if plan["source"] == "hl":
            bars = fetch_hl_daily(plan["coin"])
            used = plan["note"]
        else:
            last_err = None
            bars = None
            used = plan["note"]
            for ticker in plan["tickers"]:
                try:
                    bars = fetch_yahoo_daily(ticker)
                    if ticker != plan["tickers"][0]:
                        used = f"Yahoo {ticker} (birincil {plan['tickers'][0]} alınamadı)"
                    else:
                        used = plan["note"]
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
            move_threshold=plan["move_threshold"],
            informational=plan["informational"],
            utc_session=plan["utc"],
        )
    except Exception as exc:
        log.exception("%s hesaplanamadı", key)
        return empty_result(plan, str(exc))


def run(dry_run: bool) -> int:
    generated_at = now_tsi()
    asof = generated_at.date()
    log.info("Hesaplama başladı (%s)", fmt_when(generated_at))

    events = fetch_calendar_high_usd_eur(asof)
    results = [load_instrument(plan, asof) for plan in INSTRUMENT_PLAN]
    ok = sum(1 for r in results if not r.error)
    if ok == 0:
        log.error("Hiçbir enstrüman verisi alınamadı; mesaj gönderilmeyecek.")
        if dry_run:
            print("HATA: Hiçbir enstrüman verisi alınamadı.", file=sys.stderr)
        return 1

    text = build_message(results, events, generated_at)
    print(text)
    print("", flush=True)
    log.info("Mesaj uzunluğu: %s karakter, parça: %s", len(text), len(split_message(text)))

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
