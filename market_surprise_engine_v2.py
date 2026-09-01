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
  - NTFY_TOPIC: "Market_Brucius_5822"
"""

from __future__ import annotations
import requests
import statistics
import datetime as dt
from dataclasses import dataclass
from typing import Literal

try:
    import yfinance as yf
except ImportError:
    yf = None  # avvisato a runtime se manca

# ---------------------------------------------------------------------------
# CONFIG — modifica questi valori
# ---------------------------------------------------------------------------

FRED_API_KEY = "INSERISCI_LA_TUA_CHIAVE_GRATUITA"

# Scegli UNA delle due modalità di notifica (o entrambe)
NTFY_TOPIC = "Market_Brucius_5822"   # es. "market-alert-mario-8271"
TELEGRAM_BOT_TOKEN = ""   # lascia vuoto se usi solo ntfy
TELEGRAM_CHAT_ID = ""     # lascia vuoto se usi solo ntfy

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
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()["observations"]


# ---------------------------------------------------------------------------
# FETCH — prezzi storici e VIX (via yfinance, nessuna chiave richiesta)
# ---------------------------------------------------------------------------

def fetch_price_history(ticker: str, period: str = "10y", interval: str = "1d"):
    """Scarica storico prezzi. Es: fetch_price_history('SPY'), fetch_price_history('BTC-USD')."""
    if yf is None:
        raise ImportError("Esegui: pip install yfinance")
    return yf.download(ticker, period=period, interval=interval, progress=False)


def fetch_vix_level() -> float:
    """Livello VIX corrente (ultimo close disponibile)."""
    data = fetch_price_history("^VIX", period="5d", interval="1d")
    return float(data["Close"].iloc[-1])


# ---------------------------------------------------------------------------
# FETCH — CFTC Commitment of Traders (positioning istituzionale, gratis)
# ---------------------------------------------------------------------------

def fetch_cot_report(contract_market_name: str = "E-MINI S&P 500") -> dict | None:
    """
    Interroga l'API pubblica Socrata del CFTC (Legacy Futures Only Report).
    contract_market_name esempi: "E-MINI S&P 500", "BITCOIN", "UST 10Y NOTE".
    Nota: i nomi contratto CFTC sono in maiuscolo e devono combaciare
    esattamente — verifica su publicreporting.cftc.gov se non trovi risultati.
    """
    url = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    params = {
        "$where": f"contract_market_name='{contract_market_name}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": 1,
    }
    r = requests.get(url, params=params, timeout=15)
    if r.status_code != 200 or not r.json():
        return None
    row = r.json()[0]
    long_pos = int(row.get("noncomm_positions_long_all", 0))
    short_pos = int(row.get("noncomm_positions_short_all", 0))
    net = long_pos - short_pos
    return {
        "data_report": row.get("report_date_as_yyyy_mm_dd"),
        "long": long_pos,
        "short": short_pos,
        "net": net,
        "direzione": "net-long" if net > 0 else "net-short",
    }


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

def send_ntfy(message: str, title: str = "Market Alert"):
    """Invia notifica push tramite ntfy.sh — nessuna registrazione richiesta."""
    if not NTFY_TOPIC or "inserisci" in NTFY_TOPIC:
        print("[ntfy] NTFY_TOPIC non configurato, notifica saltata")
        return
    requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title},
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
# ORCHESTRAZIONE — esempio di controllo giornaliero
# ---------------------------------------------------------------------------

def check_and_alert():
    """
    Esempio di routine giornaliera: controlla VIX e positioning CFTC su S&P500,
    genera una nota testuale e la invia se supera soglie definite.
    Questo è un ESEMPIO — la logica di soglia va adattata caso per caso.
    """
    try:
        vix = fetch_vix_level()
    except Exception as e:
        print(f"Errore fetch VIX: {e}")
        vix = None

    try:
        cot = fetch_cot_report("E-MINI S&P 500")
    except Exception as e:
        print(f"Errore fetch COT: {e}")
        cot = None

    righe = [f"Controllo del {dt.date.today().isoformat()}"]
    if vix is not None:
        righe.append(f"VIX: {vix:.1f}")
    if cot is not None:
        righe.append(f"Positioning S&P500 (COT {cot['data_report']}): {cot['direzione']}")

    messaggio = "\n".join(righe)
    print(messaggio)

    # Esempio soglia: avvisa solo se VIX sopra 25 (volatilità elevata)
    if vix is not None and vix > 25:
        notify(messaggio, title="VIX elevato")


if __name__ == "__main__":
    check_and_alert()
