#!/usr/bin/env python3
"""
Paper-Trading-Bot: handelt mit SPIELGELD auf echten Live-Kursen von Kraken.

- Kein Konto, kein API-Schluessel, kein echtes Geld.
- Startet mit 24 EUR virtuellem Kapital pro Strategie.
- Rechnet Gebuehren und Slippage ehrlich ein.
- Vergleicht zwei einfache Strategien mit "einfach kaufen und halten".

Jeder Aufruf verarbeitet die neueste abgeschlossene 15-Minuten-Kerze
und speichert den Stand in state.json, trades.csv und equity.csv.
"""
import csv
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------- Einstellungen
PAIR = "XBTEUR"          # Bitcoin in Euro
INTERVAL = 15            # Kerzenlaenge in Minuten
START_CAPITAL = 24.0     # virtuelles Startkapital in EUR (pro Strategie)
FEE = 0.004              # 0,40 % Gebuehr pro Trade (Kraken Taker, kleines Volumen)
SLIPPAGE = 0.0005        # 0,05 % schlechterer Ausfuehrungspreis

STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"
EQUITY_FILE = "equity.csv"
README_FILE = "README.md"
CHART_FILE = "chart.svg"

STRATEGY_NAMES = {
    "trend": "Trendfolge (EMA 12/48)",
    "rsi": "Gegenbewegung (RSI 14)",
    "hold": "Kaufen & Halten (Vergleich)",
}


# ---------------------------------------------------------------- Kursdaten
def fetch_candles():
    """Holt die letzten 15-Min-Kerzen von Kraken. Gibt [(zeit, schlusskurs)] zurueck,
    nur abgeschlossene Kerzen (die letzte, noch laufende wird verworfen)."""
    url = f"https://api.kraken.com/0/public/OHLC?pair={PAIR}&interval={INTERVAL}"
    req = urllib.request.Request(url, headers={"User-Agent": "paper-trading-bot/1.0"})
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
            if data.get("error"):
                raise RuntimeError(", ".join(data["error"]))
            result = data["result"]
            key = next(k for k in result if k != "last")
            rows = result[key][:-1]  # letzte Kerze laeuft noch
            return [(int(r[0]), float(r[4])) for r in rows]
        except Exception as exc:  # Netzwerkfehler, Wartung usw.
            last_error = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Kraken nicht erreichbar: {last_error}")


# ---------------------------------------------------------------- Indikatoren
def ema(values, n):
    k = 2 / (n + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(values, n=14):
    gains, losses = [], []
    for a, b in zip(values[:-1], values[1:]):
        change = b - a
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


# ---------------------------------------------------------------- Strategien
# Jede Strategie sagt: True = investiert sein, False = in Euro sein, None = nichts tun.
def signal_trend(closes):
    return ema(closes, 12)[-1] > ema(closes, 48)[-1]


def signal_rsi(closes):
    value = rsi(closes, 14)
    if value < 30:
        return True     # stark gefallen -> kaufen
    if value > 55:
        return False    # wieder erholt -> verkaufen
    return None


def signal_hold(closes):
    return True


SIGNALS = {"trend": signal_trend, "rsi": signal_rsi, "hold": signal_hold}


# ---------------------------------------------------------------- Buchhaltung
def new_state():
    return {
        "started": None,
        "start_price": None,
        "last_candle": 0,
        "last_price": None,
        "portfolios": {
            name: {"cash": START_CAPITAL, "btc": 0.0, "trades": 0, "fees": 0.0}
            for name in SIGNALS
        },
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return new_state()


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def append_csv(path, header, row):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(header)
        w.writerow(row)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def buy(pf, price):
    fee = pf["cash"] * FEE
    fill = price * (1 + SLIPPAGE)
    pf["btc"] = (pf["cash"] - fee) / fill
    pf["cash"] = 0.0
    pf["fees"] += fee
    pf["trades"] += 1
    return fill


def sell(pf, price):
    fill = price * (1 - SLIPPAGE)
    gross = pf["btc"] * fill
    fee = gross * FEE
    pf["cash"] = gross - fee
    pf["btc"] = 0.0
    pf["fees"] += fee
    pf["trades"] += 1
    return fill


def equity(pf, price):
    return pf["cash"] + pf["btc"] * price


# ---------------------------------------------------------------- Ausgabe
def fmt_eur(x):
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def write_chart():
    if not os.path.exists(EQUITY_FILE):
        return
    with open(EQUITY_FILE, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if len(rows) < 2:
        return
    names = list(SIGNALS)
    colors = {"trend": "#2b7de9", "rsi": "#e9862b", "hold": "#888888"}
    series = {n: [float(r[n]) for r in rows] for n in names}
    all_vals = [v for s in series.values() for v in s] + [START_CAPITAL]
    lo, hi = min(all_vals), max(all_vals)
    if hi - lo < 0.01:
        hi, lo = hi + 0.5, lo - 0.5
    pad = (hi - lo) * 0.08
    lo, hi = lo - pad, hi + pad
    W, H, L, R, T, B = 800, 320, 70, 20, 20, 50

    def x(i):
        return L + (W - L - R) * i / (len(rows) - 1)

    def y(v):
        return T + (H - T - B) * (hi - v) / (hi - lo)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="sans-serif" font-size="13">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
    ]
    for frac in (0, 0.25, 0.5, 0.75, 1):
        v = lo + (hi - lo) * frac
        parts.append(f'<line x1="{L}" x2="{W-R}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#eeeeee"/>')
        parts.append(f'<text x="{L-8}" y="{y(v)+4:.1f}" text-anchor="end" fill="#666">{v:.2f}</text>')
    parts.append(
        f'<line x1="{L}" x2="{W-R}" y1="{y(START_CAPITAL):.1f}" y2="{y(START_CAPITAL):.1f}" '
        f'stroke="#bbbbbb" stroke-dasharray="4 4"/>'
    )
    for n in names:
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(series[n]))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{colors[n]}" stroke-width="2"/>')
    lx = L
    for n in names:
        parts.append(f'<rect x="{lx}" y="{H-25}" width="14" height="4" fill="{colors[n]}"/>')
        label = STRATEGY_NAMES[n].replace("&", "&amp;")
        parts.append(f'<text x="{lx+20}" y="{H-19}" fill="#333">{label}</text>')
        lx += 240
    parts.append("</svg>")
    with open(CHART_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))


def write_status(state):
    price = state["last_price"]
    lines = [
        "<!-- STATUS:START -->",
        "## Aktueller Stand",
        "",
        f"**Gestartet:** {state['started']}  ",
        f"**Letztes Update:** {iso(state['last_candle'])}  ",
        f"**Bitcoin-Preis:** {fmt_eur(price)} (Start: {fmt_eur(state['start_price'])})",
        "",
        "| Strategie | Kontostand | Ergebnis | Trades | Gezahlte Gebühren | Gerade |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for name, pf in state["portfolios"].items():
        eq = equity(pf, price)
        pct = (eq / START_CAPITAL - 1) * 100
        pct_str = f"{pct:+.2f} %".replace(".", ",")
        pos = "in Bitcoin" if pf["btc"] > 0 else "in Euro"
        lines.append(
            f"| {STRATEGY_NAMES[name]} | {fmt_eur(eq)} | {pct_str} | {pf['trades']} "
            f"| {fmt_eur(pf['fees'])} | {pos} |"
        )
    lines += [
        "",
        "![Verlauf des Kontostands](chart.svg)",
        "",
        "Alles Spielgeld. Gebühren (0,40 %) und Slippage (0,05 %) sind pro Trade eingerechnet.",
        "<!-- STATUS:END -->",
    ]
    block = "\n".join(lines)

    text = ""
    if os.path.exists(README_FILE):
        with open(README_FILE, encoding="utf-8") as f:
            text = f.read()
    start, end = "<!-- STATUS:START -->", "<!-- STATUS:END -->"
    if start in text and end in text:
        before = text.split(start)[0]
        after = text.split(end, 1)[1]
        text = before + block + after
    else:
        text = block + "\n\n" + text
    with open(README_FILE, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------- Hauptablauf
def run(candles=None):
    try:
        candles = candles if candles is not None else fetch_candles()
    except RuntimeError as exc:
        print(f"Übersprungen: {exc}")
        return False
    if len(candles) < 60:
        print("Zu wenige Kursdaten, übersprungen.")
        return False

    state = load_state()
    last_time, price = candles[-1]
    if last_time <= state["last_candle"]:
        print("Noch keine neue Kerze seit dem letzten Lauf.")
        return False

    if state["started"] is None:
        state["started"] = iso(last_time)
        state["start_price"] = price

    closes = [c for _, c in candles]
    for name, signal in SIGNALS.items():
        pf = state["portfolios"][name]
        want_in = signal(closes)
        if want_in is True and pf["btc"] == 0:
            fill = buy(pf, price)
            action = "KAUF"
        elif want_in is False and pf["btc"] > 0:
            fill = sell(pf, price)
            action = "VERKAUF"
        else:
            continue
        append_csv(
            TRADES_FILE,
            ["zeit", "strategie", "aktion", "preis", "kontostand"],
            [iso(last_time), name, action, f"{fill:.2f}", f"{equity(pf, price):.4f}"],
        )
        print(f"{STRATEGY_NAMES[name]}: {action} zu {fill:.2f} EUR")

    state["last_candle"] = last_time
    state["last_price"] = price
    append_csv(
        EQUITY_FILE,
        ["zeit", "preis"] + list(SIGNALS),
        [iso(last_time), f"{price:.2f}"]
        + [f"{equity(state['portfolios'][n], price):.4f}" for n in SIGNALS],
    )
    save_state(state)
    write_chart()
    write_status(state)

    for name, pf in state["portfolios"].items():
        print(f"{STRATEGY_NAMES[name]}: {fmt_eur(equity(pf, price))}")
    return True


if __name__ == "__main__":
    run()
    sys.exit(0)
