"""
COMPUTE NEWS HISTORICAL STATS — backtest reale sui segnali GDELT
====================================================================
Stessa metodologia rigorosa usata per VIX/spread HY/crypto in
compute_historical_stats.py, applicata ai temi di notizie monitorati da
GDELT: identifica gli episodi storici in cui il volume di notizie su un
tema è esploso, e misura DAVVERO cosa è successo dopo al prezzo
dell'asset di riferimento — invece di limitarsi a una soglia grezza
(3x la baseline) senza mai verificarne il valore predittivo.

IMPORTANTE — vincolo pratico noto: questo script richiede accesso di rete
verso api.gdeltproject.org. Se la tua rete locale blocca quel dominio
(capita, verificato durante lo sviluppo), esegui questo script SOLO tramite
il workflow GitHub Actions dedicato, non in locale.

USO:
  python3 compute_news_historical_stats.py
  Genera/aggiorna news_historical_cases.json con hit-rate, significatività
  statistica, expectancy — stesso rigore del backtest principale.
"""

from __future__ import annotations
import json
import os
import statistics
import datetime as dt
import time

import gdelt_monitor as gd
from compute_historical_stats import (
    fetch_price_series_incrementale,
    event_study,
    test_significativita,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(BASE_DIR, "news_historical_cases.json")

# Asset sottostante (ticker yfinance) usato per il backtest di ciascun tema —
# stesso strumento indicato come "posizione di riferimento" nelle notifiche
TEMA_TICKER = {
    "dazi_commercio": ("SPY", 10),
    "sanzioni": ("CL=F", 10),
    "fed_policy": ("SPY", 10),
    "geopolitica_energia": ("CL=F", 10),
}


def trova_episodi_spike(volumi_giornalieri: dict, finestra_baseline: int = 14,
                         soglia_deviazioni: float = 2.0) -> list[dt.date]:
    """
    Identifica i giorni in cui il volume di notizie è anomalo rispetto
    alla media mobile dei giorni precedenti — soglia statistica (media +
    N deviazioni standard), non un moltiplicatore fisso arbitrario.
    Restituisce solo il PRIMO giorno di ogni episodio (non ogni giorno
    in cui il volume resta alto), per non contare più volte lo stesso evento.
    """
    date_ordinate = sorted(volumi_giornalieri.keys())
    valori = [volumi_giornalieri[d] for d in date_ordinate]

    episodi = []
    precedente_era_spike = False
    for i in range(finestra_baseline, len(valori)):
        finestra = valori[i - finestra_baseline:i]
        media = statistics.mean(finestra)
        dev_std = statistics.pstdev(finestra) if len(finestra) > 1 else 0
        soglia = media + soglia_deviazioni * dev_std

        e_spike = valori[i] > soglia and soglia > 0
        if e_spike and not precedente_era_spike:
            data_str = date_ordinate[i]
            try:
                episodi.append(dt.datetime.strptime(data_str, "%Y%m%d%H%M%S").date())
            except ValueError:
                try:
                    episodi.append(dt.date.fromisoformat(data_str[:10]))
                except ValueError:
                    continue
        precedente_era_spike = e_spike

    return episodi


def backtest_tema(tema: str, query: str) -> dict | None:
    print(f"\nScarico storico notizie per '{tema}'...")
    volumi = gd.fetch_gdelt_timeline_volume(query, giorni=1095)  # ~3 anni
    if not volumi:
        print(f"  nessun dato storico disponibile per '{tema}'")
        return None

    episodi = trova_episodi_spike(volumi)
    print(f"  {len(episodi)} episodi di spike trovati")

    if len(episodi) < 5:
        print(f"  troppo pochi episodi ({len(episodi)}) per un backtest affidabile")
        return None

    ticker, anni = TEMA_TICKER.get(tema, ("SPY", 10))
    prezzo = fetch_price_series_incrementale(ticker, anni)

    # direzione attesa: partiamo dall'ipotesi che un picco di notizie
    # geopolitiche/commerciali sia "risk-off" (calo atteso) — come per il
    # VIX, se i dati reali mostrano il contrario, andrà corretta di conseguenza
    risultato = event_study(prezzo, episodi, orizzonte_giorni=10, direzione_attesa=-1)
    if risultato is None:
        return None

    risultato["tema"] = tema
    risultato["ticker_usato"] = ticker
    return risultato


def main():
    risultati_finali = {}
    temi = list(gd.TEMI_MONITORATI.items())

    for i, (tema, query) in enumerate(temi):
        risultato = backtest_tema(tema, query)
        if risultato is None:
            # pausa più lunga dopo un fallimento, potrebbe indicare
            # che GDELT sta rallentando/limitando le richieste
            if i < len(temi) - 1:
                print("  pausa di 15s prima del prossimo tema...")
                time.sleep(15)
            continue

        sig_str = "SIGNIFICATIVO" if risultato["significativo_95"] else "non significativo" if risultato["significativo_95"] is False else "n/d"
        print(f"[{tema}] hit-rate {risultato['hit_rate_calcolato']:.0%}, "
              f"n={risultato['n_casi_indicativi']}, {sig_str}, "
              f"expectancy {risultato['expectancy']}")

        risultati_finali[tema] = risultato

        if i < len(temi) - 1:
            time.sleep(8)  # pausa normale tra temi, per non concatenare richieste pesanti

    with open(OUTPUT_FILE, "w") as f:
        json.dump(risultati_finali, f, indent=2, ensure_ascii=False, default=str)

    print(f"\nSalvato in {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
