"""
STATISTICHE COMUNI
==================
Un solo posto per il test di significatività e per il formato "VERDETTO",
usato da tutti i backtest (drawdown, segnali macro, notizie) e dal motore.

Regola: una direzione (RIALZO/RIBASSO) si dichiara solo se
  - ci sono almeno MIN_N casi, e
  - la frequenza di rialzo dopo il segnale è diversa dalla frequenza NORMALE
    di rialzo dello stesso asset sullo stesso orizzonte (binomiale a due code,
    p < ALFA).
Il confronto con il 50% non basta: un asset che sale spesso (es. S&P 500,
~60-65% delle finestre di 10 giorni) sembrerebbe sempre "rimbalzare".
"""
from __future__ import annotations

import math

import pandas as pd

MIN_N = 20
ALFA = 0.05


def p_bilaterale(k: int, n: int, p0: float) -> float:
    """Probabilità di un risultato lontano da p0 almeno quanto k/n (binomiale esatta, due code)."""
    if n <= 0:
        return 1.0
    pmf = [math.comb(n, i) * p0**i * (1 - p0)**(n - i) for i in range(n + 1)]
    return min(1.0, sum(p for p in pmf if p <= pmf[k] + 1e-12))


def frequenza_normale_rialzo(prezzo: pd.Series, orizzonte: int) -> float:
    """Quota di tutte le finestre di `orizzonte` giorni in cui l'asset ha chiuso in rialzo (o pari)."""
    prezzo = prezzo.dropna()
    fwd = (prezzo.shift(-orizzonte) / prezzo - 1).dropna()
    return float((fwd >= 0).mean())


def riassunto(casi: list[float], base: float, orizzonte: int) -> dict:
    """
    casi: rendimenti (frazioni, 0.03 = +3%) a `orizzonte` giorni dopo ogni episodio.
    base: frequenza normale di rialzo dell'asset sullo stesso orizzonte.
    Restituisce verdetto + intervallo storico (25°-75° percentile, mediana, estremi).
    """
    n = len(casi)
    g = f"_{orizzonte}g"
    if n == 0:
        return {"n": 0, "orizzonte_giorni": orizzonte, "significativo": False, "direzione": "NESSUNA"}
    s = pd.Series(casi, dtype=float)
    k = int((s >= 0).sum())
    p = p_bilaterale(k, n, base)
    pct_up = k / n
    sig = bool(n >= MIN_N and p < ALFA)
    return {
        "n": n,
        "orizzonte_giorni": orizzonte,
        "pct_rialzo_dopo": round(pct_up, 3),
        "pct_rialzo_normale": round(base, 3),
        "p_value": round(p, 4),
        "significativo": sig,
        "direzione": ("RIALZO" if pct_up > base else "RIBASSO") if sig else "NESSUNA",
        "mediana" + g: round(float(s.median()), 4),
        "p25" + g: round(float(s.quantile(0.25)), 4),
        "p75" + g: round(float(s.quantile(0.75)), 4),
        "peggiore" + g: round(float(s.min()), 4),
        "migliore" + g: round(float(s.max()), 4),
        "metodo_test": f"binomiale a due code vs frequenza normale, n>={MIN_N}, p<{ALFA}",
    }


def righe_verdetto(r: dict | None) -> str:
    """
    Due righe per la notifica:
      VERDETTO: RIALZO/RIBASSO VERIFICATO ...  oppure  VERDETTO: NESSUNA DIREZIONE VERIFICATA ...
      A N giorni: metà dei casi tra X% e Y% (mediana Z%); estremi A% / B%
    """
    if not r or "pct_rialzo_normale" not in r:
        n = (r or {}).get("n")
        motivo = f"storico insufficiente (n={n})" if n is not None else "storico non ancora calcolato con il test corretto"
        return f"VERDETTO: NESSUNA DIREZIONE VERIFICATA - {motivo}"
    g = r.get("orizzonte_giorni", 10)
    p_txt = "< 0.01" if r["p_value"] < 0.01 else f"{r['p_value']:.2f}"
    confronto = (f"n={r['n']}: in rialzo a {g} giorni nel {r['pct_rialzo_dopo']:.0%} dei casi "
                 f"vs {r['pct_rialzo_normale']:.0%} normale, p {p_txt}")
    if r.get("significativo"):
        riga1 = f"VERDETTO: {r['direzione']} VERIFICATO ({confronto})"
    else:
        nota = ", campione troppo piccolo" if r["n"] < MIN_N else ""
        riga1 = f"VERDETTO: NESSUNA DIREZIONE VERIFICATA ({confronto}{nota})"
    suf = f"_{g}g"
    try:
        riga2 = (f"A {g} giorni: metà dei casi tra {r['p25' + suf]:+.1%} e {r['p75' + suf]:+.1%} "
                 f"(mediana {r['mediana' + suf]:+.1%}); estremi {r['peggiore' + suf]:+.1%} / {r['migliore' + suf]:+.1%}")
    except KeyError:
        riga2 = "Intervallo storico non ancora calcolato"
    return f"{riga1}\n{riga2}"
