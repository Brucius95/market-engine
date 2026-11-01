"""
MARKET SURPRISE ENGINE v2 — con fetch reali + notifiche
==========================================================
Estende market_surprise_engine.py con:
  - Funzioni di fetch reali verso fonti gratuite (FRED, yfinance, CFTC, alternative.me)
  - Invio notifiche push (ntfy.sh o Telegram) quando scatta un alert

REQUISITI:
  pip install requests yfinance pandas

CONFIGURAZIONE RICHIESTA (vedi sezione CONFIG sotto):
  - FRED_API_KEY: gratuita su https://fred.stlouisfed.org/docs/api/api_key.html
  - NTFY_TOPIC: nessuna registrazione richiesta, basta scegliere un nome unico
    (vedi istruzioni Telegram/ntfy nella chat)
"""

from __future__ import annotations
import requests
import pandas as pd
import statistics
import datetime as dt
import time
import json
import os
from datetime import timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Literal

from statistiche import righe_verdetto

try:
    import registro_risultati as registro
except ImportError:
    registro = None

try:
    import yfinance as yf
except ImportError:
    yf = None  # avvisato a runtime se manca

TZ_ROMA = ZoneInfo("Europe/Rome")  # gestisce CET/CEST automaticamente

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.json")


def _load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)


def _request_with_backoff(func, *args, max_retries: int = 3, **kwargs):
    """
    Esegue una chiamata con retry esponenziale se la fonte risponde
    'troppe richieste' (HTTP 429) o con un errore temporaneo di rete.
    Protezione anti rate-limit: non martella la fonte, aspetta e riprova.
    """
    for tentativo in range(max_retries):
        try:
            return func(*args, **kwargs)
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 429 and tentativo < max_retries - 1:
                attesa = 2 ** (tentativo + 2)  # 4s, 8s, 16s...
                print(f"[rate-limit] risposta 429, attendo {attesa}s e riprovo")
                time.sleep(attesa)
                continue
            raise
        except requests.exceptions.RequestException as e:
            if tentativo < max_retries - 1:
                time.sleep(3)
                continue
            raise

# ---------------------------------------------------------------------------
# CONFIG — le chiavi si leggono da variabili d'ambiente (necessario per
# GitHub Actions, dove le chiavi vivono nei "Secrets", mai nel codice).
# In locale, se la variabile d'ambiente non esiste, si usa il valore qui
# sotto come fallback — così continua a funzionare anche sul tuo Mac.
# ---------------------------------------------------------------------------

FRED_API_KEY = os.environ.get("FRED_API_KEY", "INSERISCI_LA_TUA_CHIAVE_GRATUITA")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # solo dal secret GitHub: mai scrivere il topic nel codice (il repo è pubblico)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SURPRISE_THRESHOLD_PP = 0.10


# ---------------------------------------------------------------------------
# FETCH — FRED (serie macro, spread, curva tassi)
# ---------------------------------------------------------------------------

def fetch_fred_series(series_id: str, limit: int = 10) -> list[dict]:
    """
    Scarica gli ultimi valori di una serie FRED.
    Serie utili:
      - "T10Y2Y"        -> spread Treasury 10y-2y (curva dei tassi)
      - "BAMLH0A0HYM2"  -> spread High Yield (stress creditizio)
      - "ICSA"          -> Initial Jobless Claims (settimanale)
      - "CPIAUCSL"      -> CPI headline USA
    """
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": limit,
    }
    r = _request_with_backoff(requests.get, url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()["observations"]


def fetch_fred_latest_cached(series_id: str) -> dict | None:
    """
    Versione con cache giornaliera di fetch_fred_series — le serie FRED si
    aggiornano al massimo una volta al giorno (spesso meno), quindi non ha
    senso interrogarle ogni 15 minuti. Restituisce {"valore": float, "trend": str}
    confrontando l'ultimo dato con il penultimo.
    """
    if not FRED_API_KEY or "INSERISCI" in FRED_API_KEY:
        return None

    cache = _load_cache()
    cache_key = f"fred_{series_id}"
    oggi = dt.date.today().isoformat()

    if cache.get(cache_key, {}).get("data") == oggi:
        return cache[cache_key]["valore"]

    try:
        oss = fetch_fred_series(series_id, limit=5)
        valori = [float(o["value"]) for o in oss if o["value"] != "."]
        if len(valori) < 2:
            return None
        ultimo, penultimo = valori[0], valori[1]
        risultato = {
            "valore": ultimo,
            "trend": "in salita" if ultimo > penultimo else "in discesa" if ultimo < penultimo else "stabile",
        }
        cache[cache_key] = {"data": oggi, "valore": risultato}
        _save_cache(cache)
        return risultato
    except Exception as e:
        print(f"[fred] errore su {series_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# FETCH — prezzi storici e VIX (via yfinance, nessuna chiave richiesta)
# ---------------------------------------------------------------------------

def fetch_price_history(ticker: str, period: str = "10y", interval: str = "1d"):
    """Scarica storico prezzi. Es: fetch_price_history('SPY'), fetch_price_history('BTC-USD')."""
    if yf is None:
        raise ImportError("Esegui: pip install yfinance")
    for tentativo in range(3):
        try:
            return yf.download(ticker, period=period, interval=interval, progress=False)
        except Exception as e:
            if tentativo < 2:
                print(f"[retry] fetch prezzi fallito, riprovo tra 5s ({e})")
                time.sleep(5)
            else:
                raise


def fetch_vix_level() -> float:
    """Livello VIX corrente (ultimo close disponibile)."""
    data = fetch_price_history("^VIX", period="5d", interval="1d")
    close = data["Close"]
    # yfinance a volte restituisce colonne multi-livello (una colonna
    # per ticker anche se ne chiediamo uno solo) — normalizziamo sempre
    # a un valore scalare singolo, indipendentemente dal formato.
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    valore = close.iloc[-1]
    if hasattr(valore, "item"):
        valore = valore.item()
    return float(valore)


# ---------------------------------------------------------------------------
# FETCH — CFTC Commitment of Traders (positioning istituzionale, gratis)
# ---------------------------------------------------------------------------

def fetch_cot_report(contract_market_name: str = "E-MINI S&P 500") -> dict | None:
    """
    Interroga l'API pubblica Socrata del CFTC (Legacy Futures Only Report).
    Il COT si aggiorna solo settimanalmente (ogni venerdì) — quindi qui
    usiamo una CACHE GIORNALIERA: se abbiamo già interrogato oggi, riusiamo
    il risultato invece di richiamare l'API a ogni esecuzione ravvicinata.
    Questo evita richieste inutili quando lo script gira ogni 15 minuti.
    """
    cache = _load_cache()
    cache_key = f"cot_{contract_market_name}"
    oggi = dt.date.today().isoformat()

    if cache.get(cache_key, {}).get("data") == oggi:
        return cache[cache_key]["valore"]

    url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    params = {
        "$where": f"contract_market_name='{contract_market_name}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 1,
    }
    r = _request_with_backoff(requests.get, url, params=params, timeout=15)
    if r is None or r.status_code != 200 or not r.json():
        return None
    row = r.json()[0]
    long_pos = int(row.get("noncomm_positions_long_all", 0))
    short_pos = int(row.get("noncomm_positions_short_all", 0))
    net = long_pos - short_pos
    risultato = {
        "data_report": row.get("report_date_as_yyyy_mm_dd"),
        "long": long_pos,
        "short": short_pos,
        "net": net,
        "direzione": "net-long" if net > 0 else "net-short",
    }

    cache[cache_key] = {"data": oggi, "valore": risultato}
    _save_cache(cache)
    return risultato


# ---------------------------------------------------------------------------
# FETCH — sentiment crypto gratuito (alternative.me, nessuna chiave)
# ---------------------------------------------------------------------------

def fetch_fedwatch_proxy() -> dict | None:
    """
    Proxy gratuito di CME FedWatch: confronta il tasso implicito dai future
    sui Fed Funds (CME, ticker ZQ=F su Yahoo Finance) con il tasso effettivo
    attuale (FRED, serie FEDFUNDS). La differenza indica cosa il mercato sta
    prezzando per la prossima decisione — taglio, rialzo, o nessun cambio.

    NOTA METODOLOGICA: la vera CME FedWatch pondera su più scadenze future
    contemporaneamente con una formula proprietaria; questo è un proxy
    semplificato su una singola scadenza (il contratto front-month), utile
    come indicazione di direzione ma non identico al tool ufficiale.
    """
    if not FRED_API_KEY or "INSERISCI" in FRED_API_KEY:
        return None

    cache = _load_cache()
    cache_key = "fedwatch_proxy"
    oggi = dt.date.today().isoformat()

    if cache.get(cache_key, {}).get("data") == oggi:
        return cache[cache_key]["valore"]

    try:
        futures = fetch_price_history("ZQ=F", period="5d", interval="1d")
        prezzo_futures = futures["Close"]
        if hasattr(prezzo_futures, "columns"):
            prezzo_futures = prezzo_futures.iloc[:, 0]
        tasso_implicito = 100 - float(prezzo_futures.iloc[-1])

        oss = fetch_fred_series("FEDFUNDS", limit=1)
        tasso_attuale = float(oss[0]["value"])

        delta = tasso_implicito - tasso_attuale
        if delta < -0.05:
            direzione = "taglio atteso"
        elif delta > 0.05:
            direzione = "rialzo atteso"
        else:
            direzione = "nessun cambio atteso"

        risultato = {
            "tasso_implicito": round(tasso_implicito, 2),
            "tasso_attuale": round(tasso_attuale, 2),
            "delta": round(delta, 2),
            "direzione": direzione,
        }
        cache[cache_key] = {"data": oggi, "valore": risultato}
        _save_cache(cache)
        return risultato
    except Exception as e:
        print(f"[fedwatch] errore: {e}")
        return None



def fetch_crypto_fear_greed() -> dict:
    r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10)
    r.raise_for_status()
    data = r.json()["data"][0]
    return {"valore": int(data["value"]), "classificazione": data["value_classification"]}


# ---------------------------------------------------------------------------
# NOTIFICHE
# ---------------------------------------------------------------------------

def _ascii_safe(text: str) -> str:
    """
    Gli header HTTP (come 'Title' per ntfy) richiedono codifica latin-1 e
    non accettano alcuni caratteri Unicode comuni (es. il trattino lungo
    '—'). Li sostituiamo con equivalenti ASCII sicuri.
    """
    sostituzioni = {"—": "-", "–": "-", "'": "'", '"': '"', '"': '"'}
    for orig, sost in sostituzioni.items():
        text = text.replace(orig, sost)
    return text.encode("ascii", "ignore").decode("ascii")


def send_ntfy(message: str, title: str = "Market Alert") -> bool:
    """
    Invia notifica push tramite ntfy.sh. Restituisce True SOLO se il server
    ha confermato la consegna (prima non si controllava l'esito: un rifiuto,
    ad esempio per troppe richieste, passava inosservato e l'episodio veniva
    comunque segnato come 'già notificato').
    """
    if not NTFY_TOPIC or "inserisci" in NTFY_TOPIC:
        print("[ntfy] NTFY_TOPIC non configurato, notifica saltata")
        return False
    for tentativo in range(3):
        try:
            r = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": _ascii_safe(title)},
                timeout=15,
            )
            if r.status_code == 200:
                return True
            print(f"[ntfy] rifiutata (HTTP {r.status_code}): {r.text[:200]} - tentativo {tentativo + 1}/3")
        except requests.exceptions.RequestException as e:
            print(f"[ntfy] errore di rete: {e} - tentativo {tentativo + 1}/3")
        time.sleep(5 * (tentativo + 1))
    print(f"[ntfy] NOTIFICA NON CONSEGNATA: {title}")
    return False


def send_telegram(message: str):
    """Invia notifica tramite bot Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] Bot non configurato, notifica saltata")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)


def notify(message: str, title: str = "Market Alert") -> bool:
    """Manda su tutti i canali configurati."""
    ok = send_ntfy(message, title)
    send_telegram(f"{title}\n\n{message}")
    return ok


# ---------------------------------------------------------------------------
# DEDUPLICA GLOBALE — se più funzioni diverse (check_and_alert, insider,
# asset drawdown, news) puntano tutte alla STESSA posizione impattata
# nella stessa esecuzione, deve arrivare UNA sola notifica, non una per
# ogni funzione. La deduplica dentro check_and_alert copriva solo i 5
# segnali principali tra loro — questa è valida su TUTTO il sistema.
# ---------------------------------------------------------------------------

_posizioni_notificate_questa_esecuzione: set[str] = set()


def notify_posizione(chiave_posizione: str, messaggio: str, titolo: str) -> bool:
    """
    Invia la notifica solo se questa posizione non ha già ricevuto un
    avviso in questa stessa esecuzione da un'altra funzione/segnale.
    Restituisce True se consegnata (o già coperta da un'altra notifica), False se la consegna è fallita.
    """
    if chiave_posizione in _posizioni_notificate_questa_esecuzione:
        print(f"  Posizione '{chiave_posizione}' già notificata in questa esecuzione "
              f"da un altro segnale — salto per evitare il duplicato")
        return True  # la posizione è comunque coperta da un'altra notifica

    if not notify(messaggio, title=titolo):
        return False  # consegna fallita: il chiamante NON deve segnare l'episodio come notificato
    _posizioni_notificate_questa_esecuzione.add(chiave_posizione)
    return True


# ---------------------------------------------------------------------------
# REGISTRO DEI RISULTATI — ogni episodio di segnale (push o solo log) viene
# fotografato con la previsione del momento; dopo N giorni se ne misura
# l'esito (vedi registro_risultati.py). Un errore del registro non deve mai
# bloccare le notifiche: per questo ogni chiamata è protetta.
# ---------------------------------------------------------------------------

def _registra_attivo(chiave: str, modulo: str, segnale: str, posizione: str, isin: str,
                     ticker_misura: str, orizzonte: int, previsione: dict | None,
                     stato_invio: str, valore: str = "",
                     inizio_reale: tuple[str, int] | None = None) -> None:
    if registro is None:
        return
    try:
        registro.segnale_attivo(chiave, modulo, segnale, posizione, isin, ticker_misura,
                                orizzonte, previsione, stato_invio, valore, inizio_reale)
    except Exception as e:
        print(f"[registro] errore su {chiave} (il controllo prosegue): {e}")


def _registra_inattivo(chiave: str) -> None:
    if registro is None:
        return
    try:
        registro.segnale_inattivo(chiave)
    except Exception as e:
        print(f"[registro] errore chiusura {chiave}: {e}")


# asset su cui il backtest di ciascun segnale macro ha misurato l'effetto
MISURA_MACRO = {
    "VIX elevato": ("SPY", 10),
    "spread credito in allargamento": ("SPY", 10),
    "crypto in paura estrema": ("BTC-USD", 14),
    "curva invertita": ("SPY", 10),
    "positioning net-short": ("SPY", 10),
}


# ---------------------------------------------------------------------------
# PLAYBOOK STORICO — contesto (hit-rate, movimento mediano, timing) per
# ogni tipo di segnale, così le notifiche non sono solo un elenco grezzo.
# ---------------------------------------------------------------------------

HISTORICAL_CASES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historical_cases.json")


def _load_historical_playbook() -> dict:
    """Carica il dataset storico con i pattern per ogni tipo di segnale."""
    if not os.path.exists(HISTORICAL_CASES_FILE):
        return {}
    try:
        with open(HISTORICAL_CASES_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"[playbook] errore caricamento: {e}")
        return {}


def get_historical_stats(signal_name: str) -> dict | None:
    """Estrae hit-rate, movimento mediano, timing e significatività statistica per un segnale."""
    playbook = _load_historical_playbook()
    caso = playbook.get(signal_name)
    if not caso:
        return None

    # attenzione: dopo l'aggiunta di hit_rate_prima_meta_campione/seconda_meta,
    # startswith("hit_rate") da solo prenderebbe la chiave sbagliata — escludiamo
    # esplicitamente le chiavi split-half
    hit_rate_key = next((k for k in caso if k.startswith("hit_rate") and "meta" not in k), None)
    hit_rate = caso.get(hit_rate_key)
    move_key = next((k for k in caso if k.startswith("mediana_move")), None)
    move = caso.get(move_key, "n/d")

    # normalizza il timing a ore, qualunque sia l'unità originale nel dataset
    if caso.get("picco_ore") is not None:
        picco_h = caso["picco_ore"]
        fine_h = (caso.get("esaurimento_giorni") or 0) * 24
    elif caso.get("picco_giorni") is not None:
        picco_h = caso["picco_giorni"] * 24
        fine_h = (caso.get("esaurimento_giorni") or 0) * 24
    elif caso.get("picco_mesi") is not None:
        picco_h = caso["picco_mesi"] * 30 * 24
        fine_h = (caso.get("esaurimento_mesi") or 0) * 30 * 24
    else:
        picco_h, fine_h = None, None

    return {
        "hit_rate": hit_rate,
        "move": move,
        "picco_h": picco_h,
        "fine_h": fine_h,
        "p_value": caso.get("p_value"),
        "significativo_95": caso.get("significativo_95"),
        "stabile_nel_tempo": caso.get("stabile_nel_tempo"),
        "verdetto": caso.get("verdetto"),  # test vs frequenza normale + intervallo (statistiche.py)
    }



# ---------------------------------------------------------------------------
# NOME POSIZIONE — come compare tipicamente su Trade Republic.
# Verifica sempre in app: non ho accesso diretto al catalogo di Trade Republic,
# questi sono i nomi/ISIN standard con cui gli strumenti sono generalmente
# quotati ovunque, TR incluso.
# ---------------------------------------------------------------------------

POSITION_MAP = {
    "VIX elevato": {"nome": "iShares Core S&P 500 UCITS ETF", "isin": "IE00B5BMR087"},
    "curva invertita": {"nome": "Mercato obbligazionario USA (nessuno strumento diretto)", "isin": "—"},
    "spread credito in allargamento": {"nome": "iShares $ High Yield Corp Bond UCITS ETF", "isin": "IE00B4PY7Y77"},
    "positioning net-short": {"nome": "iShares Core S&P 500 UCITS ETF", "isin": "IE00B5BMR087"},
    "crypto in paura estrema": {"nome": "Bitcoin", "isin": "—"},
}

# Asset monitorati individualmente per drawdown estremo — soglia calibrata
# per asset class, NON uniforme: la crypto oscilla naturalmente molto più
# dell'oro o del petrolio, quindi la stessa percentuale avrebbe un
# significato statistico completamente diverso a seconda dell'asset.
# La soglia di hit-rate (65%) resta invece uniforme per tutti — qui
# differenziamo la sensibilità del trigger, non il criterio di qualità.
ASSET_DRAWDOWN_MONITORATI = {
    # Crypto — alta volatilità naturale, soglia più ampia
    "BTC-USD": {"nome": "Bitcoin", "isin": "—", "soglia_pct": -15},
    "ETH-USD": {"nome": "Ethereum", "isin": "—", "soglia_pct": -18},
    "SOL-USD": {"nome": "Solana", "isin": "—", "soglia_pct": -20},
    # Metalli preziosi — bassa volatilità tipica, soglia più stretta
    "GC=F": {"nome": "Xetra-Gold (proxy: futures oro CME)", "isin": "DE000A0S9GB0", "soglia_pct": -6},
    "SI=F": {"nome": "WisdomTree Physical Silver (proxy: futures argento CME)", "isin": "JE00B1VS3333", "soglia_pct": -10},
    # Energia — volatilità intermedia, spesso guidata da eventi geopolitici
    "CL=F": {"nome": "WisdomTree WTI Crude Oil (proxy: futures WTI CME)", "isin": "GB00B15KY990", "soglia_pct": -12},
    # Crypto ad alta volatilità — soglie più ampie perché oscillazioni del
    # 25-30% in poche settimane sono relativamente comuni, non anomale;
    # con soglie strette come Bitcoin genererebbero troppi falsi positivi
    "DOGE-USD": {"nome": "Dogecoin", "isin": "—", "soglia_pct": -28},
    "AVAX-USD": {"nome": "Avalanche", "isin": "—", "soglia_pct": -25},
}


def build_clean_alert(signal_name: str, soglia_desc: str, valore_desc: str) -> str:
    """Costruisce il messaggio nel formato richiesto: pulito, un blocco per segnale."""
    stats = get_historical_stats(signal_name)
    pos = POSITION_MAP.get(signal_name, {"nome": signal_name, "isin": "—"})

    ora = dt.datetime.now(TZ_ROMA)
    righe = [
        f"Posizione: {pos['nome']} ({pos['isin']})",
        f"Risultato atteso: {soglia_desc}",
        f"Risultato effettivo: {valore_desc}",
    ]

    if stats:
        righe.append(righe_verdetto(stats.get("verdetto")))

        # date di picco/fine solo se la direzione è verificata: senza direzione
        # un "picco atteso" suggerirebbe un movimento che i dati non confermano
        verificato = (stats.get("verdetto") or {}).get("significativo") is True
        if verificato and stats["picco_h"] is not None:
            picco_dt = ora + timedelta(hours=stats["picco_h"])
            fine_dt = ora + timedelta(hours=stats["fine_h"])
            fmt = "%d/%m %H:%M %Z"
            righe.append(f"Inizio: {ora.strftime(fmt)}")
            righe.append(f"Picco atteso: {picco_dt.strftime(fmt)}")
            righe.append(f"Fine: {fine_dt.strftime(fmt)}")

    return "\n".join(righe)


# ---------------------------------------------------------------------------
# ORCHESTRAZIONE — soglia di affidabilità storica + notifica una sola volta
# per episodio (niente ripetizioni finché il segnale resta attivo)
# ---------------------------------------------------------------------------

def check_and_alert():
    cache = _load_cache()
    letture = {}  # log leggibile in console/log.txt, non nella notifica

    # --- VIX ---
    try:
        vix = fetch_vix_level()
        letture["VIX elevato"] = {"attivo": vix > 25, "soglia": "VIX ≤ 25", "valore": f"VIX {vix:.1f}"}
    except Exception as e:
        print(f"Errore fetch VIX: {e}")

    # --- Positioning CFTC ---
    try:
        cot = fetch_cot_report("E-MINI S&P 500")
        if cot:
            letture["positioning net-short"] = {
                "attivo": cot["direzione"] == "net-short",
                "soglia": "positioning neutro/long",
                "valore": f"positioning {cot['direzione']}",
            }
    except Exception as e:
        print(f"Errore fetch COT: {e}")

    # --- Curva Treasury ---
    curva = fetch_fred_latest_cached("T10Y2Y")
    if curva:
        letture["curva invertita"] = {
            "attivo": curva["valore"] < 0,
            "soglia": "curva 10y-2y positiva",
            "valore": f"spread {curva['valore']:.2f}",
        }

    # --- Spread High Yield ---
    hy = fetch_fred_latest_cached("BAMLH0A0HYM2")
    if hy:
        letture["spread credito in allargamento"] = {
            "attivo": hy["trend"] == "in salita",
            "soglia": "spread HY stabile/in calo",
            "valore": f"spread {hy['valore']:.2f} ({hy['trend']})",
        }

    # --- Crypto Fear & Greed ---
    try:
        fg = fetch_crypto_fear_greed()
        letture["crypto in paura estrema"] = {
            "attivo": fg["valore"] < 25,
            "soglia": "Fear&Greed ≥ 25",
            "valore": f"F&G {fg['valore']} ({fg['classificazione']})",
        }
    except Exception as e:
        print(f"Errore fetch Fear&Greed: {e}")

    print(f"Controllo del {dt.datetime.now(TZ_ROMA).strftime('%Y-%m-%d %H:%M %Z')}")
    for nome, dati in letture.items():
        print(f"  {nome}: {dati['valore']} — attivo: {dati['attivo']}")

    # --- Notifica: solo segnali con hit-rate storico >= soglia. La chiave
    #     di deduplica è la POSIZIONE IMPATTATA (ISIN), non il nome del
    #     segnale-causa: se più segnali puntano allo stesso titolo (es. VIX
    #     elevato + positioning net-short, entrambi su S&P500), arriva UNA
    #     sola notifica — quella del segnale con l'affidabilità più alta.
    episodi = cache.get("episodi_attivi", {})  # ora chiave = ISIN posizione, non nome segnale

    # raggruppo i segnali attivi e qualificati per posizione impattata
    per_posizione: dict[str, list[tuple[str, dict, dict]]] = {}
    for nome, dati in letture.items():
        stats = get_historical_stats(nome)
        if stats is None:
            continue

        # criterio di qualificazione (regola 5): notifica push SOLO se il backtest
        # ha almeno 20 casi e p < 0.05 nel confronto con la frequenza normale
        # dell'asset (vedi statistiche.py). Niente più soglia fissa del 65%:
        # curva invertita (6 casi) e positioning net-short non hanno un test
        # valido e restano solo nei log (in futuro: riepilogo della domenica).
        verdetto = stats.get("verdetto") or {}
        supera_soglia = verdetto.get("significativo") is True

        if dati["attivo"] and not supera_soglia:
            print(f"  [solo log, non verificato] {nome}: {dati['valore']} | "
                  f"{righe_verdetto(verdetto or None).splitlines()[0]}")
        if dati["attivo"] and supera_soglia:
            pos = POSITION_MAP.get(nome, {"nome": nome, "isin": "—"})
            chiave_posizione = pos["isin"] if pos["isin"] != "—" else pos["nome"]
            per_posizione.setdefault(chiave_posizione, []).append((nome, dati, stats))

    esito_invio: dict[str, str] = {}  # segnale -> stato d'invio, per il registro
    for chiave_posizione, candidati in per_posizione.items():
        if episodi.get(chiave_posizione):
            for c in candidati:
                esito_invio[c[0]] = "push_consegnata"  # già notificata in questo episodio
            continue  # posizione già notificata in questo episodio, salto

        # rappresentante: il segnale con l'evidenza statistica più forte (p-value più basso)
        nome_rappresentante, dati_rappresentante, _ = min(candidati, key=lambda c: c[2]["verdetto"]["p_value"])
        messaggio = build_clean_alert(nome_rappresentante, dati_rappresentante["soglia"], dati_rappresentante["valore"])
        titolo = POSITION_MAP.get(nome_rappresentante, {}).get("nome", nome_rappresentante)
        ok = notify_posizione(chiave_posizione, messaggio, titolo)
        esito_invio[nome_rappresentante] = "push_consegnata" if ok else "push_fallita"
        if ok:
            episodi[chiave_posizione] = True

    # registro: ogni segnale attivo (push o solo log) è un episodio da misurare
    for nome, dati in letture.items():
        chiave_reg = f"macro|{nome}"
        if not dati["attivo"]:
            _registra_inattivo(chiave_reg)
            continue
        ticker_m, orizz = MISURA_MACRO.get(nome, ("SPY", 10))
        pos = POSITION_MAP.get(nome, {"nome": nome, "isin": "—"})
        stats = get_historical_stats(nome) or {}
        _registra_attivo(chiave_reg, "macro", nome, pos["nome"], pos["isin"], ticker_m, orizz,
                         stats.get("verdetto"), esito_invio.get(nome, "solo_log"), dati["valore"])

    # libero le posizioni che non hanno più nessun segnale attivo/qualificato
    for chiave_posizione in list(episodi.keys()):
        if chiave_posizione not in per_posizione:
            episodi[chiave_posizione] = False

    cache["episodi_attivi"] = episodi
    _save_cache(cache)


# ---------------------------------------------------------------------------
# CALENDARIO PROATTIVO — promemoria su eventi con data nota da settimane/mesi
# (FOMC, CPI). A differenza dei segnali sopra (reattivi, rilevano condizioni
# già in corso), qui sappiamo IN ANTICIPO quando guardare — non l'esito.
# ---------------------------------------------------------------------------

CALENDAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_events.json")
CALENDAR_PLAYBOOK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calendar_playbook.json")
REMINDER_DAYS_BEFORE = [7, 1]  # invia un promemoria a 7 giorni e a 1 giorno dall'evento


def _load_calendar() -> list[dict]:
    if not os.path.exists(CALENDAR_FILE):
        return []
    with open(CALENDAR_FILE) as f:
        dati = json.load(f)
    # il primo elemento è un blocco di note/fonti, non un evento vero
    return [e for e in dati if "id" in e]


def _load_calendar_playbook() -> dict:
    if not os.path.exists(CALENDAR_PLAYBOOK_FILE):
        return {}
    with open(CALENDAR_PLAYBOOK_FILE) as f:
        return json.load(f)


def build_calendar_reminder(evento: dict, giorni_anticipo: int) -> str:
    """Formatta il promemoria per un evento a calendario, stesso stile pulito delle altre notifiche."""
    playbook = _load_calendar_playbook()
    info = playbook.get(evento["tipo"], {})
    pos = info.get("posizione_riferimento", {"nome": evento["tipo"], "isin": "—"})

    righe = [
        f"Posizione: {pos['nome']} ({pos['isin']})",
        f"Evento: {evento['nome']}",
        f"Data: {evento['data']} · {evento.get('ora_locale', 'orario da confermare')}",
        f"Anticipo: {giorni_anticipo} {'giorno' if giorni_anticipo == 1 else 'giorni'}",
    ]
    if info.get("range_storico_1gg"):
        righe.append(f"Range storico tipico (indicativo, non calcolato dai dati): {info['range_storico_1gg']}")

    # FedWatch ha senso solo per le riunioni FOMC — dice cosa il mercato
    # sta già prezzando prima della decisione (proxy libero di CME FedWatch)
    if evento["tipo"] == "FOMC":
        fedwatch = fetch_fedwatch_proxy()
        if fedwatch:
            righe.append(
                f"Aspettative di mercato: {fedwatch['direzione']} "
                f"(implicito {fedwatch['tasso_implicito']}% vs attuale {fedwatch['tasso_attuale']}%)"
            )

    return "\n".join(righe)


def check_calendar_reminders():
    """
    Controlla se oggi cade a REMINDER_DAYS_BEFORE giorni da un evento noto.
    Notifica una sola volta per (evento, soglia di anticipo) — niente ripetizioni
    se lo script gira più volte nello stesso giorno.
    """
    eventi = _load_calendar()
    if not eventi:
        return

    oggi = dt.datetime.now(TZ_ROMA).date()
    cache = _load_cache()
    promemoria_inviati = cache.get("promemoria_calendario", {})

    for evento in eventi:
        try:
            data_evento = dt.date.fromisoformat(evento["data"])
        except (KeyError, ValueError):
            continue

        giorni_mancanti = (data_evento - oggi).days
        if giorni_mancanti not in REMINDER_DAYS_BEFORE:
            continue

        chiave = f"{evento['id']}_{giorni_mancanti}"
        if promemoria_inviati.get(chiave):
            continue  # già inviato oggi per questa soglia specifica

        messaggio = build_calendar_reminder(evento, giorni_mancanti)

        # anti-doppioni SEPARATO da quello per posizione: un promemoria non è un
        # segnale sulla posizione, quindi non deve mai essere saltato perché la
        # stessa posizione ha già ricevuto un'altra notifica (es. VIX alto), né
        # deve impedirla. Si segna come inviato SOLO se ntfy risponde 200
        # (notify -> send_ntfy restituisce True solo in quel caso); altrimenti
        # si riprova all'esecuzione successiva dello stesso giorno.
        if notify(messaggio, title=f"Tra {giorni_mancanti}gg: {evento['nome']}"):
            promemoria_inviati[chiave] = True
        else:
            print(f"[calendario] promemoria '{chiave}' non consegnato, riprovo alla prossima esecuzione")

    cache["promemoria_calendario"] = promemoria_inviati
    _save_cache(cache)


# ---------------------------------------------------------------------------
# INSIDER TRADING — segnale specifico per titolo, non solo indicatori generali
# ---------------------------------------------------------------------------

def check_insider_signals():
    """
    Controlla l'attività insider (SEC EDGAR) sui titoli monitorati.
    Notifica solo su segnali chiari (almeno 2 transazioni nella stessa
    direzione), una sola volta per episodio, come gli altri segnali.
    """
    try:
        import insider_trading as ins
    except ImportError:
        print("[insider] modulo insider_trading.py non trovato, salto il controllo")
        return

    cache = _load_cache()
    episodi_insider = cache.get("episodi_insider", {})

    for ticker, info in ins.TITOLI_MONITORATI.items():
        oggi = dt.date.today().isoformat()
        cache_key = f"insider_data_{ticker}"

        if cache.get(cache_key, {}).get("data") == oggi:
            riepilogo = cache[cache_key]["valore"]  # già interrogato oggi, riuso
        else:
            try:
                riepilogo = ins.summarize_insider_activity(ticker, giorni=30)
                cache[cache_key] = {"data": oggi, "valore": riepilogo}
            except Exception as e:
                print(f"[insider] errore su {ticker}: {e}")
                continue

        print(f"  insider {ticker}: {riepilogo['segnale']} "
              f"({riepilogo['n_acquisti']} acquisti, {riepilogo['n_vendite']} vendite)")

        # solo l'ACQUISTO netto è un segnale informativo raro e genuino —
        # i dirigenti vendono di routine (compensi in azioni, diversificazione,
        # piani programmati mesi prima), quindi la vendita non porta
        # informazione utile e genererebbe notifiche quasi ogni settimana
        attivo = riepilogo["segnale"] == "acquisto netto"

        chiave_reg = f"insider|{ticker}"
        if attivo:
            stato_invio = "push_consegnata" if episodi_insider.get(ticker) else "solo_log"
            if not episodi_insider.get(ticker):
                # regola 5: nessun backtest sugli acquisti insider, quindi nessuna direzione
                messaggio = (
                    f"Posizione: {info['nome_tr']} ({info['isin']})\n"
                    f"Risultato atteso: attività insider neutra\n"
                    f"Risultato effettivo: {riepilogo['n_acquisti']} acquisti, {riepilogo['n_vendite']} vendite (30gg), "
                    f"{abs(riepilogo['azioni_nette']):.0f} azioni nette in acquisto\n"
                    f"VERDETTO: NESSUNA DIREZIONE VERIFICATA - nessun backtest sugli acquisti insider "
                    f"(l'esito a 10 giorni viene misurato nel registro dei risultati)"
                )
                chiave_posizione = info["isin"] if info["isin"] != "—" else info["nome_tr"]
                ok = notify_posizione(chiave_posizione, messaggio, titolo=info["nome_tr"])
                stato_invio = "push_consegnata" if ok else "push_fallita"
                if ok:
                    episodi_insider[ticker] = True
            _registra_attivo(chiave_reg, "insider", f"acquisti insider ({ticker})", info["nome_tr"], info["isin"],
                             ticker, 10, None, stato_invio,
                             f"{riepilogo['n_acquisti']} acquisti, {riepilogo['n_vendite']} vendite in 30 giorni")
        else:
            episodi_insider[ticker] = False
            _registra_inattivo(chiave_reg)

    cache["episodi_insider"] = episodi_insider
    _save_cache(cache)


# ---------------------------------------------------------------------------
# CRYPTO PER SINGOLA MONETA — drawdown dal massimo a 30gg, per moneta
# (a differenza del Fear&Greed che è un indice di mercato unico e aggregato)
# ---------------------------------------------------------------------------

def _variazione_30gg(ticker: str) -> float | None:
    """Variazione % negli ultimi 30 giorni di borsa di un benchmark (None se non disponibile)."""
    try:
        c = fetch_price_history(ticker, period="3mo", interval="1d")["Close"]
        if hasattr(c, "columns"):
            c = c.iloc[:, 0]
        c = c.dropna()
        return (float(c.iloc[-1]) / float(c.iloc[-30]) - 1) * 100
    except Exception as e:
        print(f"[contesto] benchmark {ticker} non disponibile: {e}")
        return None


def _contesto_drawdown(ticker: str, drawdown_pct: float) -> str:
    """
    Contesto calcolato dal sistema: il calo è specifico dell'asset o fa parte
    di un movimento più ampio? Confronta con i benchmark negli stessi 30 giorni.
    Regole di logica economica (NON backtest): servono a dare una lettura,
    non una probabilità.
    """
    cripto = ticker.endswith("-USD")
    metallo = ticker in ("GC=F", "SI=F")
    sp = _variazione_30gg("^GSPC")
    righe = []
    if cripto:
        btc = _variazione_30gg("BTC-USD") if ticker != "BTC-USD" else None
        if btc is not None:
            righe.append(f"Bitcoin negli stessi 30 giorni: {btc:+.1f}%")
        if sp is not None:
            righe.append(f"S&P 500: {sp:+.1f}%")
        mercato_debole = (btc is not None and btc <= -10) or (ticker == "BTC-USD" and sp is not None and sp <= -3)
        lettura = ("calo diffuso: anche il resto del mercato crypto/azionario è debole"
                   if mercato_debole else
                   "calo più marcato del mercato di riferimento: movimento specifico dell'asset")
    elif metallo:
        usd = _variazione_30gg("DX-Y.NYB")
        if usd is not None:
            righe.append(f"Dollaro USA (DXY): {usd:+.1f}%")
        if sp is not None:
            righe.append(f"S&P 500: {sp:+.1f}%")
        if usd is not None and usd >= 2:
            lettura = "dollaro in rafforzamento: di solito pesa su oro e argento"
        elif usd is not None and usd <= -2:
            lettura = "dollaro in calo, che di solito sostiene i metalli: il calo non è spiegato dal cambio"
        else:
            lettura = "dollaro poco mosso: il calo non è spiegato dal cambio"
    else:  # petrolio e altro
        if sp is not None:
            righe.append(f"S&P 500: {sp:+.1f}%")
        lettura = ("calo in un mercato azionario debole: possibile timore sulla domanda"
                   if sp is not None and sp <= -3 else
                   "calo non accompagnato da debolezza azionaria: movimento specifico del comparto")
    return ("Contesto (stessi 30 giorni): " + "; ".join(righe) + f"\nLettura: {lettura}") if righe else f"Lettura: {lettura}"


def _stats_drawdown(ticker: str) -> dict | None:
    """Risultato del backtest drawdown per l'asset (None se non disponibile)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "drawdown_historical_cases.json")
    try:
        with open(path) as f:
            return json.load(f).get(ticker)
    except Exception:
        return None


def _inizio_reale_drawdown(ticker: str, fino_a: str) -> tuple[str, int] | None:
    """
    Primo giorno della serie continua di giorni sotto soglia che arriva fino a
    `fino_a` (stessa regola del backtest: calo dal massimo a 30 giorni oltre la
    soglia dell'asset), e quanti giorni di borsa sono passati da allora.
    None se a `fino_a` l'asset non era sotto soglia o mancano i dati.
    """
    info = ASSET_DRAWDOWN_MONITORATI.get(ticker)
    if not info:
        return None
    try:
        close = fetch_price_history(ticker, period="1y", interval="1d")["Close"]
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        close = close.dropna()
        close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
        close = close[close.index <= pd.Timestamp(fino_a)]
        dd = (close / close.rolling(30, min_periods=30).max() - 1) * 100
        if dd.empty or not (dd.iloc[-1] <= info["soglia_pct"]):
            return None
        s = len(dd) - 1
        while s > 0 and dd.iloc[s - 1] <= info["soglia_pct"]:
            s -= 1
        return close.index[s].date().isoformat(), len(dd) - 1 - s
    except Exception as e:
        print(f"[registro] inizio reale non calcolabile per {ticker}: {e}")
        return None


def _riga_storico_drawdown(ticker: str) -> str:
    """Cosa è successo nei 10 giorni dopo drawdown analoghi (backtest reale): VERDETTO + intervallo storico."""
    return righe_verdetto(_stats_drawdown(ticker))


def check_asset_drawdown_signals():
    """
    Controlla il drawdown dal massimo a 30gg per ciascun asset monitorato
    (crypto, metalli preziosi, energia), con soglia calibrata per la
    volatilità tipica di ciascuna classe — vedi ASSET_DRAWDOWN_MONITORATI.
    """
    cache = _load_cache()
    episodi = cache.get("episodi_asset_drawdown", {})

    for ticker, info in ASSET_DRAWDOWN_MONITORATI.items():
        try:
            prezzi = fetch_price_history(ticker, period="3mo", interval="1d")
            close = prezzi["Close"]
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            ultimo = float(close.iloc[-1])
            ultimi30 = close.iloc[-30:]
            massimo_30gg = float(ultimi30.max())
            data_massimo = ultimi30.idxmax()
            giorni_da_massimo = (dt.date.today() - data_massimo.date()).days
            drawdown_pct = (ultimo / massimo_30gg - 1) * 100
        except Exception as e:
            print(f"[asset] errore su {ticker}: {e}")
            continue

        soglia = info["soglia_pct"]
        print(f"  {info['nome']}: {ultimo:.2f} (drawdown 30gg: {drawdown_pct:.1f}%, soglia: {soglia}%)")
        attivo = drawdown_pct <= soglia

        chiave_reg = f"drawdown|{ticker}"
        if attivo:
            stato_invio = "push_consegnata" if episodi.get(ticker) else "solo_log"
            if not episodi.get(ticker):
                messaggio = (
                    f"Posizione: {info['nome']} ({info['isin']})\n"
                    f"Cosa è successo: {info['nome']} è sceso del {abs(drawdown_pct):.1f}% dal suo massimo degli ultimi 30 giorni "
                    f"({massimo_30gg:,.2f} il {data_massimo.strftime('%d/%m')}, {giorni_da_massimo} giorni fa; ora {ultimo:,.2f})\n"
                    f"Soglia di allerta: calo oltre {abs(soglia)}% - anomalo per questo tipo di asset\n"
                    f"{_contesto_drawdown(ticker, drawdown_pct)}\n"
                    f"{_riga_storico_drawdown(ticker)}"
                )
                chiave_posizione = info["isin"] if info["isin"] != "—" else info["nome"]
                ok = notify_posizione(chiave_posizione, messaggio, titolo=info["nome"])
                stato_invio = "push_consegnata" if ok else "push_fallita"
                if ok:
                    episodi[ticker] = True
            inizio = None
            if registro is not None and not registro.episodio_aperto(chiave_reg):
                inizio = _inizio_reale_drawdown(ticker, close.index[-1].date().isoformat())
            _registra_attivo(chiave_reg, "drawdown", f"drawdown oltre {soglia}% ({ticker})", info["nome"],
                             info["isin"], ticker, 10, _stats_drawdown(ticker), stato_invio,
                             f"{drawdown_pct:.1f}% dal massimo a 30 giorni", inizio)
        else:
            episodi[ticker] = False
            _registra_inattivo(chiave_reg)

    cache["episodi_asset_drawdown"] = episodi
    _save_cache(cache)


def _load_news_historical_stats() -> dict:
    """Carica i risultati del backtest reale sui temi di notizie, se già calcolato."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news_historical_cases.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def check_news_signals():
    """Controlla anomalie di volume nelle notizie globali su temi rilevanti per il mercato."""
    try:
        import gdelt_monitor as gd
    except ImportError:
        print("[gdelt] modulo gdelt_monitor.py non trovato, salto il controllo")
        return

    cache = _load_cache()
    episodi_news = cache.get("episodi_news", {})

    # GDELT risponde 429 se interrogato a ogni esecuzione: al massimo un
    # controllo ogni 60 minuti, con pausa tra un tema e l'altro
    adesso = time.time()
    if adesso - cache.get("news_ultimo_controllo", 0) < 3600:
        print("[gdelt] ultimo controllo meno di 60 minuti fa, salto per evitare il rate limit")
        return
    cache["news_ultimo_controllo"] = adesso
    _save_cache(cache)

    for i, (tema, query) in enumerate(gd.TEMI_MONITORATI.items()):
        if i:
            time.sleep(8)
        try:
            risultato = gd.check_news_spike(tema, query)
        except Exception as e:
            print(f"[gdelt] errore su {tema}: {e}")
            continue

        if risultato is None:
            continue

        print(f"  news {tema}: volume {risultato['volume_recente']} vs baseline "
              f"{risultato['baseline']} (rapporto {risultato['rapporto']}x)")

        chiave_reg = f"notizie|{tema}"
        if risultato["spike"]:
            news_stats = _load_news_historical_stats().get(tema)
            # regola 5 / punto C: push SOLO se il tema ha un backtest con il
            # test corretto (campo "verdetto": almeno 20 casi, p < 0.05 vs
            # frequenza normale). Temi mai calcolati o rimasti al vecchio
            # test (confronto con il 50%) contano come "non calcolati":
            # solo log + registro + riepilogo della domenica.
            verdetto_news = (news_stats or {}).get("verdetto")
            verificato = bool(verdetto_news and verdetto_news.get("significativo") is True)
            pos_riferimento = gd.TEMA_POSIZIONE_RIFERIMENTO.get(tema, {"nome": "nessuna diretta", "isin": "—"})
            stato_invio = "push_consegnata" if episodi_news.get(tema) else "solo_log"

            if not episodi_news.get(tema) and not verificato:
                stato = righe_verdetto(verdetto_news).splitlines()[0]
                print(f"  [solo log, non verificato] notizie '{tema}': {risultato['rapporto']}x la baseline | {stato}")
            elif not episodi_news.get(tema):
                impatto_tipico = gd.TEMA_CONTESTO.get(tema, "")
                messaggio = (
                    f"Posizione di riferimento: {pos_riferimento['nome']} ({pos_riferimento['isin']})\n"
                    f"Risultato atteso: volume normale (< {gd.SPIKE_MOLTIPLICATORE_SOGLIA}x baseline)\n"
                    f"Risultato effettivo: {risultato['rapporto']}x la baseline ({risultato['volume_recente']} articoli/6h)\n"
                    f"{righe_verdetto(verdetto_news)}"
                )
                if impatto_tipico:
                    messaggio += f"\n\nCanali di impatto tipici (logica economica generale, non backtestato): {impatto_tipico}"
                if risultato.get("titoli_esempio"):
                    messaggio += "\n\nArticoli recenti:\n" + "\n".join(f"• {t}" for t in risultato["titoli_esempio"])
                chiave_posizione = pos_riferimento["isin"] if pos_riferimento["isin"] != "—" else pos_riferimento["nome"]
                ok = notify_posizione(chiave_posizione, messaggio, titolo=f"Notizie in anomalia: {tema.replace('_', ' ')}")
                stato_invio = "push_consegnata" if ok else "push_fallita"
                if ok:
                    episodi_news[tema] = True

            # asset misurato = quello usato dal backtest del tema
            try:
                from compute_news_historical_stats import TEMA_TICKER
                ticker_m = TEMA_TICKER.get(tema, ("SPY", 10))[0]
            except Exception:
                ticker_m = (news_stats or {}).get("ticker_usato", "SPY")
            _registra_attivo(chiave_reg, "notizie", f"notizie {tema}", pos_riferimento["nome"],
                             pos_riferimento["isin"], ticker_m, 10, verdetto_news, stato_invio,
                             f"{risultato['rapporto']}x la baseline")
        else:
            episodi_news[tema] = False
            _registra_inattivo(chiave_reg)

    cache["episodi_news"] = episodi_news
    _save_cache(cache)


def check_earnings_and_stock_signals():
    """
    (1) Promemoria 7 e 1 giorni prima dei conti trimestrali, con la reazione
        storica del titolo. (2) Allerta se un'azione monitorata si muove oltre
        max(4%, 3 deviazioni standard annue) in un giorno, con contesto storico.
    Una sola notifica per evento e per posizione (cache + notify_posizione).
    """
    try:
        import earnings_monitor as em
    except ImportError:
        print("[earnings] modulo earnings_monitor.py non trovato, salto")
        return

    cache = _load_cache()
    stats = em.carica_stats()
    oggi = dt.date.today()
    inviati = cache.get("earnings_notificati", {})
    ora_it = dt.datetime.now(TZ_ROMA)

    for ticker, info in em.TITOLI_MONITORATI.items():
        chiave_posizione = info["isin"] if info["isin"] != "—" else info["nome_tr"]

        # --- calendario conti (interrogo la fonte una volta al giorno)
        ck = f"earn_next_{ticker}"
        if cache.get(ck, {}).get("giorno") != oggi.isoformat():
            nxt = em.next_earnings(em.fetch_earnings_dates(ticker, limit=12), oggi)
            cache[ck] = {"giorno": oggi.isoformat(), "valore": nxt}
        nxt = cache[ck]["valore"]
        if nxt:
            giorni = (dt.date.fromisoformat(nxt["data"]) - oggi).days
            for soglia in (7, 1):
                chiave = f"{ticker}_{nxt['data']}_{soglia}"
                if giorni == soglia and not inviati.get(chiave):
                    momento = "dopo la chiusura USA" if nxt["dopo_chiusura"] else "prima dell'apertura USA"
                    msg = (
                        f"Posizione: {info['nome_tr']} ({info['isin']})\n"
                        f"Evento: conti trimestrali tra {giorni} giorn{'o' if giorni == 1 else 'i'} "
                        f"({nxt['data']}, {momento})\n"
                        f"{em.riga_storico_conti(ticker, stats)}"
                    )
                    # promemoria: anti-doppioni separato da quello per posizione
                    # (non viene saltato né blocca un'altra notifica sullo stesso
                    # titolo) e segnato come inviato SOLO se ntfy risponde 200
                    if notify(msg, title=f"Conti {ticker}"):
                        inviati[chiave] = True
                    else:
                        print(f"[earnings] promemoria '{chiave}' non consegnato, riprovo alla prossima esecuzione")

        # --- movimento anomalo del titolo
        mov = em.movimento_anomalo(ticker)
        chiave_reg = f"azione|{ticker}"
        if mov and not mov["anomalo"]:
            _registra_inattivo(chiave_reg)
        if mov and mov["anomalo"]:
            chiave = f"gap_{ticker}_{mov['data']}"
            stato_invio = "push_consegnata" if inviati.get(chiave) else "solo_log"
            if not inviati.get(chiave):
                segno = "calo" if mov["variazione"] < 0 else "rialzo"
                msg = (
                    f"Posizione: {info['nome_tr']} ({info['isin']})\n"
                    f"Risultato atteso: variazione giornaliera entro ±{mov['soglia']:.1%}\n"
                    f"Risultato effettivo: {mov['variazione']:+.1%} il {mov['data']}\n"
                    f"{em.riga_storico_conti(ticker, stats, mov['variazione'])}"
                )
                ok = notify_posizione(chiave_posizione, msg, titolo=f"{ticker}: {segno} anomalo")
                stato_invio = "push_consegnata" if ok else "push_fallita"
                if ok:
                    inviati[chiave] = True
            # previsione: solo per movimenti di almeno ±5% esiste il backtest "dopo calo/rialzo forte"
            s_t = stats.get(ticker) or {}
            if mov["variazione"] <= em.SOGLIA_CALO_GRANDE:
                prev = s_t.get("dopo_calo_forte")
            elif mov["variazione"] >= em.SOGLIA_RIALZO_GRANDE:
                prev = s_t.get("dopo_rialzo_forte")
            else:
                prev = None
            segno_reg = "calo" if mov["variazione"] < 0 else "rialzo"
            _registra_attivo(chiave_reg, "azioni", f"movimento anomalo in {segno_reg} ({ticker})", info["nome_tr"],
                             info["isin"], ticker, em.ORIZZONTE_DRIFT, prev, stato_invio,
                             f"{mov['variazione']:+.1%} il {mov['data']}")

    # pulizia: tengo solo gli ultimi 400 eventi
    if len(inviati) > 400:
        inviati = dict(list(inviati.items())[-400:])
    cache["earnings_notificati"] = inviati
    _save_cache(cache)


# ---------------------------------------------------------------------------
# RIEPILOGO DELLA DOMENICA — una volta a settimana, prima esecuzione di
# domenica dopo le 9:00 italiane. Segnato come inviato SOLO se ntfy conferma
# (altrimenti riprova all'esecuzione successiva). Contenuto: registro dei
# risultati + segnali "solo log" della settimana. Con il punto 2 del piano
# si aggiungerà "cosa guardare" (conti in arrivo, CPI/FOMC).
# ---------------------------------------------------------------------------

ORA_RIEPILOGO_DOMENICA = 9


def check_riepilogo_domenica():
    if registro is None:
        return
    ora = dt.datetime.now(TZ_ROMA)
    if ora.weekday() != 6 or ora.hour < ORA_RIEPILOGO_DOMENICA:
        return
    settimana = ora.strftime("%G-W%V")
    cache = _load_cache()
    if cache.get("riepilogo_domenica_inviato") == settimana:
        return
    testo = registro.testo_riepilogo_domenica(ora.date())
    if notify(testo, title=registro.titolo_riepilogo_domenica(ora.date())):
        cache["riepilogo_domenica_inviato"] = settimana
        _save_cache(cache)
        print(f"[riepilogo] inviato per la settimana {settimana}")
    else:
        print("[riepilogo] consegna non confermata, riprovo alla prossima esecuzione")


if __name__ == "__main__":
    if registro is not None:
        registro.inizializza()
        try:
            # drawdown registrati prima che esistesse il campo "inizio reale"
            registro.completa_inizi_reali(_inizio_reale_drawdown)
        except Exception as e:
            print(f"[registro] errore completamento inizi reali: {e}")
    check_and_alert()
    check_calendar_reminders()
    check_insider_signals()
    check_asset_drawdown_signals()
    check_news_signals()
    check_earnings_and_stock_signals()
    if registro is not None:
        try:
            registro.aggiorna_esiti()
            registro.scrivi_report()
        except Exception as e:
            print(f"[registro] errore aggiornamento esiti/report: {e}")
    check_riepilogo_domenica()
