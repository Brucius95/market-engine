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
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

from statistiche import frequenza_normale_rialzo, riassunto

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_FILE = os.path.join(BASE_DIR, "drawdown_historical_cases.json")
ORIZZONTE = 10


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
    base = frequenza_normale_rialzo(close, ORIZZONTE)
    casi = [float(fwd.iloc[i]) for i in episodi_drawdown(close, soglia_pct) if not math.isnan(fwd.iloc[i])]
    if len(casi) < 5:
        return {"n": len(casi), "direzione": "NESSUNA", "significativo": False, "motivo": "campione troppo piccolo"}
    # chiavi salvate: n, pct_rialzo_dopo, pct_rialzo_normale, p_value, significativo, direzione,
    # mediana_10g, p25_10g, p75_10g, peggiore_10g, migliore_10g
    return riassunto(casi, base, ORIZZONTE)


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
