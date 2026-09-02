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
import time

GDELT_BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# Temi monitorati — modifica/aggiungi qui, per TEMA non per persona
TEMI_MONITORATI = {
    "dazi_commercio": "(tariffs OR \"trade war\")",
    "sanzioni": "sanctions economic",
    "fed_policy": "\"Federal Reserve\" rate decision",
    "geopolitica_energia": "(\"oil sanctions\" OR \"energy crisis\")",
}

# Canali di impatto TIPICI per tema — logica economica generale, NON un dato
# backtestato statisticamente come il VIX. Serve a orientarti su "dove
# guardare", non è una probabilità verificata sui dati storici.
TEMA_CONTESTO = {
    "dazi_commercio": "Impatta tipicamente: indici equity con esposizione export/import (industriali, tech, auto), valute dei paesi coinvolti, materie prime scambiate tra le parti.",
    "sanzioni": "Impatta tipicamente: valuta del paese sanzionato, prezzo petrolio/gas se il paese è esportatore energetico, titoli con esposizione diretta a quel mercato.",
    "fed_policy": "Impatta tipicamente: Treasury USA, dollaro (DXY), indici equity USA (S&P500), oro come bene rifugio.",
    "geopolitica_energia": "Impatta tipicamente: prezzo petrolio (WTI/Brent), titoli del settore energetico, valute di paesi produttori/importatori netti.",
}

# Posizione di riferimento più rilevante per tema (nome/ISIN reali, come
# appaiono tipicamente su Trade Republic) — NON implica una previsione di
# direzione: è solo "quale strumento guardare", non "cosa farà".
TEMA_POSIZIONE_RIFERIMENTO = {
    "dazi_commercio": {"nome": "iShares Core S&P 500 UCITS ETF", "isin": "IE00B5BMR087"},
    "sanzioni": {"nome": "WisdomTree WTI Crude Oil", "isin": "GB00B15KY990"},
    "fed_policy": {"nome": "iShares Core S&P 500 UCITS ETF", "isin": "IE00B5BMR087"},
    "geopolitica_energia": {"nome": "WisdomTree WTI Crude Oil", "isin": "GB00B15KY990"},
}

SPIKE_MOLTIPLICATORE_SOGLIA = 3.0  # segnala se il volume attuale supera 3x la baseline


def fetch_gdelt_volume(query: str, ore: int = 6) -> int | None:
    """Numero di articoli GDELT che matchano la query nelle ultime N ore."""
    articoli = fetch_gdelt_articles(query, ore)
    return len(articoli) if articoli is not None else None


def fetch_gdelt_articles(query: str, ore: int = 6) -> list[dict] | None:
    """Recupera gli articoli reali (titolo, fonte, link) che matchano la query."""
    params = {
        "query": query,
        "mode": "artlist",
        "timespan": f"{ore}h",
        "format": "json",
        "maxrecords": "250",
        "sort": "hybridrel",  # rilevanza, non solo cronologia
    }
    try:
        r = requests.get(GDELT_BASE_URL, params=params, timeout=20)
        r.raise_for_status()
        dati = r.json()
        return dati.get("articles", [])
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


def fetch_gdelt_timeline_volume(query: str, giorni: int = 1095, max_tentativi: int = 3) -> dict | None:
    """
    Recupera lo storico giornaliero del volume di notizie per un tema,
    usando la modalità 'timelinevolraw' di GDELT (conteggio grezzo articoli
    per giorno). Necessario per il backtest storico — a differenza del
    controllo live (finestra di 6h), qui serviamo anni di dati per
    costruire una vera baseline statistica.

    IMPORTANTE: per intervalli pluriennali, GDELT richiede date di inizio/fine
    esplicite (startdatetime/enddatetime) invece del parametro 'timespan',
    che è pensato solo per finestre brevi (ore/giorni).

    Include retry con backoff: GDELT a volte è lento/instabile su richieste
    pesanti consecutive (servizio accademico gratuito, non garanzie di
    uptime commerciali) — ritentiamo invece di arrenderci al primo timeout.
    """
    fine = dt.datetime.now(dt.timezone.utc)
    inizio = fine - dt.timedelta(days=giorni)
    params = {
        "query": query,
        "mode": "timelinevolraw",
        "startdatetime": inizio.strftime("%Y%m%d%H%M%S"),
        "enddatetime": fine.strftime("%Y%m%d%H%M%S"),
        "format": "json",
    }

    for tentativo in range(max_tentativi):
        try:
            r = requests.get(GDELT_BASE_URL, params=params, timeout=45)

            if r.status_code == 429:
                attesa = 60 * (tentativo + 1)  # 60s, 120s, 180s — i rate limit richiedono attese lunghe
                print(f"[gdelt] rate limit (429) su '{query}', attendo {attesa}s prima di riprovare")
                time.sleep(attesa)
                continue

            r.raise_for_status()

            if not r.text or not r.text.strip():
                print(f"[gdelt] risposta vuota per '{query}' (tentativo {tentativo + 1}) — probabile rate limit silenzioso")
                time.sleep(30 * (tentativo + 1))
                continue

            dati = r.json()
            timeline = dati.get("timeline", [])
            if not timeline:
                return None
            serie = timeline[0].get("data", [])
            return {p["date"]: p["value"] for p in serie}

        except requests.exceptions.RequestException as e:
            print(f"[gdelt] errore rete su '{query}' (tentativo {tentativo + 1}/{max_tentativi}): {e}")
            if tentativo < max_tentativi - 1:
                time.sleep(15 * (tentativo + 1))  # backoff crescente: 15s, 30s
        except (ValueError, KeyError) as e:
            print(f"[gdelt] risposta non valida per '{query}': {e}")
            return None

    print(f"[gdelt] tutti i tentativi falliti per '{query}'")
    return None


def check_news_spike(tema: str, query: str) -> dict | None:
    """Confronta il volume recente con la baseline e segnala un'anomalia se supera la soglia."""
    articoli_recenti = fetch_gdelt_articles(query, ore=6)
    if articoli_recenti is None:
        return None
    volume_recente = len(articoli_recenti)

    baseline = fetch_gdelt_baseline(query, giorni=7)
    if baseline is None or baseline == 0:
        return None

    rapporto = volume_recente / baseline
    spike = rapporto >= SPIKE_MOLTIPLICATORE_SOGLIA

    # i titoli reali servono solo se c'è davvero un'anomalia da segnalare —
    # evitiamo di elaborarli inutilmente quando tutto è normale
    titoli_esempio = []
    if spike:
        for art in articoli_recenti[:3]:
            titolo = art.get("title", "").strip()
            fonte = art.get("domain", "")
            if titolo:
                titoli_esempio.append(f"{titolo} ({fonte})")

    return {
        "tema": tema,
        "volume_recente": volume_recente,
        "baseline": round(baseline, 1),
        "rapporto": round(rapporto, 2),
        "spike": spike,
        "titoli_esempio": titoli_esempio,
    }


if __name__ == "__main__":
    for tema, query in TEMI_MONITORATI.items():
        print(f"\n--- {tema} ---")
        risultato = check_news_spike(tema, query)
        print(risultato)
