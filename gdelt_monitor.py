"""
GDELT NEWS MONITOR — rilevazione anomalie nelle notizie globali
====================================================================
GDELT Project (gdeltproject.org): database di notizie globali aggiornato
ogni 15 minuti, gratuito, nessuna chiave richiesta, mantenuto da un
consorzio accademico (Google Jigsaw è tra gli sponsor storici).

PERCHÉ PER TEMA, NON PER PERSONA: il mercato reagisce al contenuto di una
dichiarazione (dazi, sanzioni, politica commerciale), non a chi la
pronuncia. Un monitoraggio per tema è più robusto — cattura il segnale
indipendentemente da chi sono i protagonisti politici del momento, ed è
esattamente il tipo di evento che ha mosso i mercati "qualche giorno dopo
in modo importante" di cui parlavi: quando un tema esplode nelle notizie
globali, spesso precede di ore/giorni la piena reazione dei prezzi.

METODOLOGIA: per ogni tema monitorato, confrontiamo il volume di notizie
delle ultime ore con la media dei giorni precedenti (baseline). Un volume
anomalo (es. 3x la norma) indica che qualcosa di rilevante sta emergendo.
"""

from __future__ import annotations

import requests
import statistics
import datetime as dt

GDELT_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# Temi monitorati — modifica/aggiungi qui, per TEMA non per persona
TEMI_MONITORATI = {
    "dazi_commercio": "tariffs OR \"trade war\" OR \"trade tariffs\"",
    "sanzioni": "sanctions economic",
    "fed_policy": "\"Federal Reserve\" rate decision",
    "geopolitica_energia": "oil sanctions OR \"energy crisis\"",
}

SPIKE_MOLTIPLICATORE_SOGLIA = 3.0  # segnala se il volume attuale supera 3x la baseline


def fetch_gdelt_volume(query: str, ore: int = 6) -> int | None:
    """Numero di articoli GDELT che matchano la query nelle ultime N ore."""
    params = {
        "query": query,
        "mode": "artlist",
        "timespan": f"{ore}h",
        "format": "json",
        "maxrecords": "250",  # sufficiente per stimare il volume, non serve leggerli tutti
    }
    try:
        r = requests.get(GDELT_BASE_URL, params=params, timeout=20)
        r.raise_for_status()
        dati = r.json()
        return len(dati.get("articles", []))
    except Exception as e:
        print(f"[gdelt] errore su query '{query}': {e}")
        return None


def fetch_gdelt_baseline(query: str, giorni: int = 7) -> float | None:
    """Volume medio giornaliero (in finestre da 6h) sui giorni precedenti, come riferimento normale."""
    volumi = []
    for giorno_fa in range(1, giorni + 1):
        # GDELT non supporta facilmente finestre storiche arbitrarie via
        # timespan relativo — usiamo una query più ampia (72h) come proxy
        # di baseline invece di N chiamate separate, per restare leggeri
        pass
    # Approccio semplificato: baseline = volume su finestra ampia (72h)
    # diviso per il numero di finestre da 6h contenute, come media di riferimento
    volume_ampio = fetch_gdelt_volume(query, ore=72)
    if volume_ampio is None:
        return None
    return volume_ampio / 12  # 72h / 6h = 12 finestre


def check_news_spike(tema: str, query: str) -> dict | None:
    """Confronta il volume recente con la baseline e segnala un'anomalia se supera la soglia."""
    volume_recente = fetch_gdelt_volume(query, ore=6)
    baseline = fetch_gdelt_baseline(query, giorni=7)

    if volume_recente is None or baseline is None or baseline == 0:
        return None

    rapporto = volume_recente / baseline
    return {
        "tema": tema,
        "volume_recente": volume_recente,
        "baseline": round(baseline, 1),
        "rapporto": round(rapporto, 2),
        "spike": rapporto >= SPIKE_MOLTIPLICATORE_SOGLIA,
    }


if __name__ == "__main__":
    for tema, query in TEMI_MONITORATI.items():
        print(f"\n--- {tema} ---")
        risultato = check_news_spike(tema, query)
        print(risultato)
