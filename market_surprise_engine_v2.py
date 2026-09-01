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
import statistics
import datetime as dt
import time
import json
import os
from dataclasses import dataclass
from typing import Literal

try:
    import yfinance as yf
except ImportError:
    yf = None  # avvisato a runtime se manca

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

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "Market_Brucius_5822")
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


def send_ntfy(message: str, title: str = "Market Alert"):
    """Invia notifica push tramite ntfy.sh — nessuna registrazione richiesta."""
    if not NTFY_TOPIC or "inserisci" in NTFY_TOPIC:
        print("[ntfy] NTFY_TOPIC non configurato, notifica saltata")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": _ascii_safe(title)},
        timeout=10,
    )


def send_telegram(message: str):
    """Invia notifica tramite bot Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] Bot non configurato, notifica saltata")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)


def notify(message: str, title: str = "Market Alert"):
    """Manda su tutti i canali configurati."""
    send_ntfy(message, title)
    send_telegram(f"{title}\n\n{message}")


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


def format_historical_context(signal_name: str) -> str:
    """
    Restituisce una riga di contesto storico per un segnale attivo:
    hit-rate, movimento mediano, timing (inizio/picco/esaurimento),
    intervallo di confidenza. Se il segnale non è nel dataset, stringa vuota.
    """
    playbook = _load_historical_playbook()
    caso = playbook.get(signal_name)
    if not caso:
        return ""

    hit_rate_key = next((k for k in caso if k.startswith("hit_rate")), None)
    hit_rate = caso.get(hit_rate_key)
    n_casi = caso.get("n_casi_indicativi")
    orizzonte = caso.get("orizzonte_anni")

    riga = f"  → storico ({orizzonte}y, n={n_casi}): "
    if hit_rate is not None:
        riga += f"{hit_rate:.0%} dei casi coerente, CI95 {caso.get('ci_95', 'n/d')}"

    move_key = next((k for k in caso if k.startswith("mediana_move")), None)
    if move_key:
        riga += f" | mediana: {caso[move_key]}"

    if caso.get("inizio_minuti") == 0:
        picco = caso.get("picco_ore") or caso.get("picco_giorni")
        unita_picco = "h" if caso.get("picco_ore") else "gg"
        esaurimento = caso.get("esaurimento_giorni")
        riga += f" | picco: +{picco}{unita_picco}, esaurimento: ~{esaurimento}gg"
    elif caso.get("picco_mesi"):
        riga += f" | segnale strutturale: effetto su {caso['picco_mesi']}-{caso.get('esaurimento_mesi', '?')} mesi"

    return riga


# ---------------------------------------------------------------------------
# ORCHESTRAZIONE — controllo con tutti i layer gratuiti disponibili
# ---------------------------------------------------------------------------

def check_and_alert():
    """
    Controllo completo: VIX, positioning CFTC, curva Treasury, spread HY,
    sentiment crypto. Calcola una confluenza di segnali di stress/risk-off
    e notifica solo se lo stato È CAMBIATO rispetto all'ultimo controllo
    (evita di re-inviare la stessa notifica ogni 15 minuti).
    """
    segnali_stress = []  # ogni segnale che indica "risk-off" viene aggiunto qui
    righe = [f"Controllo del {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}"]

    # --- VIX ---
    try:
        vix = fetch_vix_level()
        righe.append(f"VIX: {vix:.1f}")
        if vix > 25:
            segnali_stress.append("VIX elevato")
    except Exception as e:
        print(f"Errore fetch VIX: {e}")
        vix = None

    # --- Positioning CFTC (cache giornaliera) ---
    try:
        cot = fetch_cot_report("E-MINI S&P 500")
        if cot:
            righe.append(f"Positioning S&P500: {cot['direzione']}")
            if cot["direzione"] == "net-short":
                segnali_stress.append("positioning net-short")
    except Exception as e:
        print(f"Errore fetch COT: {e}")
        cot = None

    # --- Curva Treasury 10y-2y (cache giornaliera, richiede FRED_API_KEY) ---
    curva = fetch_fred_latest_cached("T10Y2Y")
    if curva:
        righe.append(f"Curva 10y-2y: {curva['valore']:.2f} ({curva['trend']})")
        if curva["valore"] < 0:
            segnali_stress.append("curva invertita")

    # --- Spread High Yield (cache giornaliera, richiede FRED_API_KEY) ---
    hy = fetch_fred_latest_cached("BAMLH0A0HYM2")
    if hy:
        righe.append(f"Spread HY: {hy['valore']:.2f} ({hy['trend']})")
        if hy["trend"] == "in salita":
            segnali_stress.append("spread credito in allargamento")

    # --- Sentiment crypto (sempre disponibile, nessuna chiave) ---
    try:
        fg = fetch_crypto_fear_greed()
        righe.append(f"Crypto Fear&Greed: {fg['valore']} ({fg['classificazione']})")
        if fg["valore"] < 25:
            segnali_stress.append("crypto in paura estrema")
    except Exception as e:
        print(f"Errore fetch Fear&Greed: {e}")

    # --- Confluenza ---
    n_segnali_totali = 5  # VIX, COT, curva, HY, crypto — quelli effettivamente valutati
    confluenza = len(segnali_stress)
    righe.append(f"Confluenza segnali di stress: {confluenza}/{n_segnali_totali}")
    if segnali_stress:
        righe.append("Segnali attivi: " + ", ".join(segnali_stress))

    messaggio = "\n".join(righe)

    if segnali_stress:
        messaggio += "\n\nContesto storico dei segnali attivi:"
        for s in segnali_stress:
            contesto = format_historical_context(s)
            if contesto:
                messaggio += f"\n{s}:{contesto}"

    print(messaggio)

    # --- Notifica solo se lo stato di allerta è cambiato dall'ultima volta ---
    stato_attuale = "allerta" if confluenza >= 2 else "normale"
    cache = _load_cache()
    stato_precedente = cache.get("ultimo_stato", "normale")

    if stato_attuale == "allerta" and stato_precedente != "allerta":
        notify(messaggio, title=f"Confluenza {confluenza}/{n_segnali_totali} - attenzione")
    elif stato_attuale == "normale" and stato_precedente == "allerta":
        notify("Stato tornato normale.\n\n" + messaggio, title="Rientro da allerta")

    cache["ultimo_stato"] = stato_attuale
    _save_cache(cache)


if __name__ == "__main__":
    check_and_alert()
