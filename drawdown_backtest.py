"""
BACKTEST DRAWDOWN PER ASSET
===========================
Per ogni asset monitorato (crypto, oro, argento, petrolio...) misura cosa è
successo nei 10 giorni DOPO l'inizio di un drawdown oltre la soglia, e lo
confronta con la frequenza normale di rialzo a 10 giorni dello stesso asset
(altrimenti un asset che sale spesso sembrerebbe sempre "rimbalzare").
Output: direzione storica solo se statisticamente distinguibile dal caso.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(BASE_DIR, "drawdown_historical_cases.json")
ORIZZONTE = 10
MIN_N = 20


def _p_bilaterale(k: int, n: int, p0: float) -> float:
    """Probabilità di un risultato lontano da p0 almeno quanto k/n (binomiale, due code)."""
    pmf = [math.comb(n, i) * p0**i * (1 - p0)**(n - i) for i in range(n + 1)]
    return min(1.0, sum(p for p in pmf if p <= pmf[k] + 1e-12))


def episodi_drawdown(close: pd.Series, soglia_pct: float) -> list[int]:
    """Indici (posizioni) del primo giorno di ogni drawdown oltre soglia; episodi separati da >= ORIZZONTE giorni."""
    dd = (close / close.rolling(30, min_periods=30).max() - 1) * 100
    attivo = dd <= soglia_pct
    idx, ultimo = [], -10**9
    for i in range(len(close)):
        if attivo.iloc[i] and not (i > 0 and attivo.iloc[i - 1]) and i - ultimo >= ORIZZONTE:
            idx.append(i)
            ultimo = i
    return idx


def analizza(close: pd.Series, soglia_pct: float) -> dict | None:
    close = close.dropna()
    if len(close) < 300:
        return None
    fwd = close.shift(-ORIZZONTE) / close - 1
    base = float((fwd.dropna() > 0).mean())
    casi = [float(fwd.iloc[i]) for i in episodi_drawdown(close, soglia_pct) if not math.isnan(fwd.iloc[i])]
    if len(casi) < 5:
        return {"n": len(casi), "direzione": "NESSUNA", "significativo": False, "motivo": "campione troppo piccolo"}
    k = sum(1 for x in casi if x > 0)
    n = len(casi)
    p = _p_bilaterale(k, n, base)
    pct_up = k / n
    sig = bool(n >= MIN_N and p < 0.05)
    direzione = ("RIALZO" if pct_up > base else "RIBASSO") if sig else "NESSUNA"
    return {
        "n": n,
        "pct_rialzo_dopo": round(pct_up, 2),
        "pct_rialzo_normale": round(base, 2),
        "mediana_10g": round(statistics.median(casi), 4),
        "p_value": round(p, 3),
        "significativo": sig,
        "direzione": direzione,
    }


def calcola_tutto() -> dict:
    from market_surprise_engine_v2 import ASSET_DRAWDOWN_MONITORATI
    risultato = {}
    if os.path.exists(OUT_FILE):
        try:
            risultato = json.load(open(OUT_FILE))
        except Exception:
            risultato = {}
    for ticker, info in ASSET_DRAWDOWN_MONITORATI.items():
        anni = 5 if ticker.endswith("-USD") else 10
        print(f"[drawdown] {ticker} ({anni} anni, soglia {info['soglia_pct']}%)...")
        try:
            h = yf.Ticker(ticker).history(period=f"{anni}y", auto_adjust=True)["Close"]
        except Exception as e:
            print(f"  errore: {e}")
            continue
        r = analizza(h, info["soglia_pct"])
        if r:
            risultato[ticker] = r
            print(f"  {r}")
    risultato["_meta"] = {"aggiornato": dt.date.today().isoformat(),
                          "metodo": f"primo giorno oltre soglia, episodi separati >= {ORIZZONTE}g, rendimento a {ORIZZONTE}g vs frequenza normale"}
    json.dump(risultato, open(OUT_FILE, "w"), indent=1)
    return risultato


if __name__ == "__main__":
    calcola_tutto()
