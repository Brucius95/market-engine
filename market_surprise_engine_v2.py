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
from datetime import timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass
from typing import Literal

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

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "Market_Brucius_5822")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SURPRISE_THRESHOLD_PP = 0.10
HIT_RATE_THRESHOLD = 0.65  # notifica solo se la storicità mostra almeno il 65% di affidabilità


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


def get_historical_stats(signal_name: str) -> dict | None:
    """Estrae hit-rate, movimento mediano e timing (in ore) per un segnale."""
    playbook = _load_historical_playbook()
    caso = playbook.get(signal_name)
    if not caso:
        return None

    hit_rate_key = next((k for k in caso if k.startswith("hit_rate")), None)
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

    return {"hit_rate": hit_rate, "move": move, "picco_h": picco_h, "fine_h": fine_h}


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

# Crypto monitorate individualmente — drawdown estremo per singola moneta,
# oltre al Fear&Greed aggregato di mercato (che è unico, non per-moneta)
CRYPTO_MONITORATE = {
    "BTC-USD": {"nome": "Bitcoin", "isin": "—"},
    "ETH-USD": {"nome": "Ethereum", "isin": "—"},
    "SOL-USD": {"nome": "Solana", "isin": "—"},
}
CRYPTO_DRAWDOWN_SOGLIA_PCT = -15  # segnala se il prezzo cala oltre il 15% dal massimo a 30gg


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
        hit = stats["hit_rate"]
        righe.append(f"Cosa accadrà: storicamente {stats['move']} ({hit:.0%} dei casi)")

        if stats["picco_h"] is not None:
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
        supera_soglia = stats and stats["hit_rate"] is not None and stats["hit_rate"] >= HIT_RATE_THRESHOLD
        if dati["attivo"] and supera_soglia:
            pos = POSITION_MAP.get(nome, {"nome": nome, "isin": "—"})
            chiave_posizione = pos["isin"] if pos["isin"] != "—" else pos["nome"]
            per_posizione.setdefault(chiave_posizione, []).append((nome, dati, stats))

    for chiave_posizione, candidati in per_posizione.items():
        if episodi.get(chiave_posizione):
            continue  # posizione già notificata in questo episodio, salto

        # scelgo il segnale con l'affidabilità storica più alta come rappresentante
        nome_rappresentante, dati_rappresentante, _ = max(candidati, key=lambda c: c[2]["hit_rate"])
        messaggio = build_clean_alert(nome_rappresentante, dati_rappresentante["soglia"], dati_rappresentante["valore"])
        titolo = POSITION_MAP.get(nome_rappresentante, {}).get("nome", nome_rappresentante)
        notify(messaggio, title=titolo)
        episodi[chiave_posizione] = True

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
        f"Anticipo: {giorni_anticipo} giorni",
    ]
    if info.get("range_storico_1gg"):
        righe.append(f"Range storico tipico: {info['range_storico_1gg']}")

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
        notify(messaggio, title=f"Tra {giorni_mancanti}gg: {evento['nome']}")
        promemoria_inviati[chiave] = True

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

        if attivo:
            if not episodi_insider.get(ticker):
                messaggio = (
                    f"Posizione: {info['nome_tr']} ({info['isin']})\n"
                    f"Risultato atteso: attività insider neutra\n"
                    f"Risultato effettivo: {riepilogo['n_acquisti']} acquisti, {riepilogo['n_vendite']} vendite (30gg)\n"
                    f"Cosa accadrà: segnale rialzista da attività insider — {abs(riepilogo['azioni_nette']):.0f} azioni nette in acquisto"
                )
                notify(messaggio, title=info["nome_tr"])
                episodi_insider[ticker] = True
        else:
            episodi_insider[ticker] = False

    cache["episodi_insider"] = episodi_insider
    _save_cache(cache)


# ---------------------------------------------------------------------------
# CRYPTO PER SINGOLA MONETA — drawdown dal massimo a 30gg, per moneta
# (a differenza del Fear&Greed che è un indice di mercato unico e aggregato)
# ---------------------------------------------------------------------------

def check_crypto_drawdown_signals():
    cache = _load_cache()
    episodi_crypto = cache.get("episodi_crypto_drawdown", {})

    for ticker, info in CRYPTO_MONITORATE.items():
        try:
            prezzi = fetch_price_history(ticker, period="3mo", interval="1d")
            close = prezzi["Close"]
            if hasattr(close, "columns"):
                close = close.iloc[:, 0]
            ultimo = float(close.iloc[-1])
            massimo_30gg = float(close.iloc[-30:].max())
            drawdown_pct = (ultimo / massimo_30gg - 1) * 100
        except Exception as e:
            print(f"[crypto] errore su {ticker}: {e}")
            continue

        print(f"  {info['nome']}: {ultimo:.2f} (drawdown 30gg: {drawdown_pct:.1f}%)")
        attivo = drawdown_pct <= CRYPTO_DRAWDOWN_SOGLIA_PCT

        if attivo:
            if not episodi_crypto.get(ticker):
                messaggio = (
                    f"Posizione: {info['nome']} ({info['isin']})\n"
                    f"Risultato atteso: drawdown ≤ {abs(CRYPTO_DRAWDOWN_SOGLIA_PCT)}% dal massimo 30gg\n"
                    f"Risultato effettivo: drawdown {drawdown_pct:.1f}%\n"
                    f"Cosa accadrà: drawdown estremo per singola moneta, valutare contesto specifico dell'asset"
                )
                notify(messaggio, title=info["nome"])
                episodi_crypto[ticker] = True
        else:
            episodi_crypto[ticker] = False

    cache["episodi_crypto_drawdown"] = episodi_crypto
    _save_cache(cache)


if __name__ == "__main__":
    check_and_alert()
    check_calendar_reminders()
    check_insider_signals()
    check_crypto_drawdown_signals()
