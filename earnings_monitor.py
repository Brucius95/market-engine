"""
EARNINGS & AZIONI SINGOLE
=========================
Colma il buco emerso con il crollo di Alphabet: calendario dei conti
trimestrali, reazione storica del titolo ai conti e monitor dei movimenti
anomali sulle singole azioni. Solo fonti gratuite (yfinance).

Output sempre DESCRITTIVO (storico + incertezza dichiarata), mai ordini.
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

from insider_trading import TITOLI_MONITORATI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATS_FILE = os.path.join(BASE_DIR, "earnings_historical_cases.json")

SOGLIA_CALO_GRANDE = -0.05      # reazione ai conti considerata "forte calo"
SOGLIA_RIALZO_GRANDE = 0.05
ORIZZONTE_DRIFT = 10            # giorni di borsa dopo la reazione
MIN_N_SIGNIFICATIVITA = 20      # sotto questa soglia: solo "indicativo"


# ---------------------------------------------------------------- dati
def fetch_earnings_dates(ticker: str, limit: int = 44) -> pd.DataFrame | None:
    """Date dei conti (passate e future). ~11 anni con limit=44."""
    if yf is None:
        return None
    try:
        df = yf.Ticker(ticker).get_earnings_dates(limit=limit)
        if df is None or df.empty:
            return None
        return df.sort_index()
    except Exception as e:
        print(f"[earnings] errore date conti {ticker}: {e}")
        return None


def next_earnings(df: pd.DataFrame | None, oggi: dt.date | None = None) -> dict | None:
    """Prossima data dei conti (prima riga futura senza EPS reale)."""
    if df is None:
        return None
    oggi = oggi or dt.date.today()
    for ts in df.index:
        d = ts.date()
        if d >= oggi:
            return {"data": d.isoformat(), "ora_ny": ts.strftime("%H:%M"),
                    "dopo_chiusura": ts.hour >= 16}
    return None


def _prezzi(ticker: str, anni: int = 11) -> pd.Series | None:
    if yf is None:
        return None
    try:
        h = yf.Ticker(ticker).history(period=f"{anni}y", auto_adjust=True)
        if h is None or h.empty:
            return None
        s = h["Close"].copy()
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        return s
    except Exception as e:
        print(f"[earnings] errore prezzi {ticker}: {e}")
        return None


# ---------------------------------------------------------------- backtest
def reazioni_storiche(ticker: str) -> list[dict]:
    """Reazione close-to-close ai conti + drift a 10 giorni di borsa."""
    ed = fetch_earnings_dates(ticker)
    px = _prezzi(ticker)
    if ed is None or px is None:
        return []
    oggi = pd.Timestamp(dt.date.today())
    out = []
    for ts in ed.index:
        giorno = pd.Timestamp(ts.date())
        if giorno >= oggi:
            continue
        # dopo la chiusura -> reagisce il giorno di borsa successivo
        idx = px.index.searchsorted(giorno, side="right" if ts.hour >= 16 else "left")
        if idx < 1 or idx + ORIZZONTE_DRIFT >= len(px):
            continue
        ret = px.iloc[idx] / px.iloc[idx - 1] - 1
        drift = px.iloc[idx + ORIZZONTE_DRIFT] / px.iloc[idx] - 1
        out.append({"data": px.index[idx].date().isoformat(),
                    "reazione": float(ret), "drift10": float(drift)})
    return out


def _p_binomiale_unilaterale(k: int, n: int, p0: float = 0.5) -> float:
    return sum(math.comb(n, i) * p0**i * (1 - p0)**(n - i) for i in range(k, n + 1))


def sintesi_ticker(reazioni: list[dict]) -> dict | None:
    if len(reazioni) < 8:
        return None
    r = [x["reazione"] for x in reazioni]
    out = {
        "n": len(r),
        "pct_positive": round(sum(1 for x in r if x > 0) / len(r), 2),
        "mediana": round(statistics.median(r), 4),
        "mediana_assoluta": round(statistics.median(abs(x) for x in r), 4),
        "peggiore": round(min(r), 4),
        "migliore": round(max(r), 4),
        "ultima": reazioni[-1],
    }
    for nome, filtro in (("dopo_calo_forte", lambda x: x["reazione"] <= SOGLIA_CALO_GRANDE),
                         ("dopo_rialzo_forte", lambda x: x["reazione"] >= SOGLIA_RIALZO_GRANDE)):
        casi = [x for x in reazioni if filtro(x)]
        if casi:
            d = [x["drift10"] for x in casi]
            k = sum(1 for x in d if x > 0)
            out[nome] = {
                "n": len(casi),
                "mediana_drift10": round(statistics.median(d), 4),
                "pct_drift_positivo": round(k / len(casi), 2),
                "p_value": round(_p_binomiale_unilaterale(k, len(casi)), 3),
                "significativo": bool(len(casi) >= MIN_N_SIGNIFICATIVITA
                                      and _p_binomiale_unilaterale(k, len(casi)) < 0.05),
            }
    return out


def calcola_tutto() -> dict:
    risultato = {}
    if os.path.exists(STATS_FILE):  # non perdere i vecchi risultati se una fonte fallisce
        try:
            with open(STATS_FILE) as f:
                risultato = json.load(f)
        except Exception:
            risultato = {}
    for ticker in TITOLI_MONITORATI:
        print(f"[earnings] {ticker}...")
        s = sintesi_ticker(reazioni_storiche(ticker))
        if s:
            risultato[ticker] = s
            print(f"  n={s['n']} mediana={s['mediana']:+.1%} |mov| tipico={s['mediana_assoluta']:.1%}")
        else:
            print("  dati insufficienti, tengo il vecchio risultato (se c'è)")
    risultato["_meta"] = {"aggiornato": dt.date.today().isoformat(),
                          "metodo": "reazione close-to-close al primo giorno di borsa utile; drift a 10 giorni"}
    with open(STATS_FILE, "w") as f:
        json.dump(risultato, f, indent=1)
    return risultato


def carica_stats() -> dict:
    try:
        with open(STATS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


# ---------------------------------------------------------------- live
def movimento_anomalo(ticker: str) -> dict | None:
    """Ultima variazione giornaliera vs 1 anno di volatilità. Soglia: max(4%, 3σ)."""
    if yf is None:
        return None
    try:
        h = yf.Ticker(ticker).history(period="1y", auto_adjust=True)["Close"].dropna()
    except Exception as e:
        print(f"[azioni] errore {ticker}: {e}")
        return None
    if len(h) < 60:
        return None
    rend = h.pct_change().dropna()
    ultimo = float(rend.iloc[-1])
    soglia = max(0.04, 3 * float(rend.iloc[:-1].std()))
    return {"data": h.index[-1].date().isoformat(), "variazione": ultimo,
            "soglia": soglia, "anomalo": abs(ultimo) >= soglia,
            "prezzo": float(h.iloc[-1])}


def riga_storico_conti(ticker: str, stats: dict, variazione: float | None = None) -> str:
    s = stats.get(ticker)
    if not s:
        return "Storico conti: dati insufficienti"
    righe = [f"Storico conti ({s['n']} trimestri): mossa tipica ±{s['mediana_assoluta']:.1%}, "
             f"{s['pct_positive']:.0%} positivi, peggiore {s['peggiore']:+.1%}"]
    if variazione is not None and variazione <= SOGLIA_CALO_GRANDE and "dopo_calo_forte" in s:
        c = s["dopo_calo_forte"]
        validita = "significativo" if c["significativo"] else f"indicativo (n={c['n']}, non significativo)"
        righe.append(f"Dopo cali ≥5% sui conti: a 10 giorni mediana {c['mediana_drift10']:+.1%}, "
                     f"{c['pct_drift_positivo']:.0%} in recupero — {validita}")
    return "\n".join(righe)


if __name__ == "__main__":
    calcola_tutto()
