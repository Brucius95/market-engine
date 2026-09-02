"""
COMPUTE HISTORICAL STATS — backtest reale sui 5 segnali
=========================================================
Sostituisce i numeri "illustrativi" di historical_cases.json con statistiche
calcolate DAVVERO sui dati storici reali: ogni volta che un indicatore ha
superato la sua soglia negli ultimi anni, cosa è successo realmente al
prezzo dell'asset collegato nei giorni successivi.

Metodologia (event study):
  1. Scarica lo storico dell'indicatore e del prezzo dell'asset impattato
  2. Trova ogni "episodio" (il PRIMO giorno in cui la condizione scatta,
     non ogni giorno in cui resta attiva — evita di contare 10 volte lo
     stesso episodio se la condizione dura 10 giorni consecutivi)
  3. Per ogni episodio, misura il rendimento dell'asset nei giorni successivi
  4. Aggrega su tutti gli episodi: hit-rate, movimento mediano, giorno di
     picco, giorno di esaurimento

Segnali coperti da backtest reale: VIX elevato, spread credito, crypto F&G
(hanno dati storici giornalieri gratuiti facilmente scaricabili).
Segnali lasciati come stima illustrativa: curva invertita (orizzonte troppo
lungo, mesi non giorni, l'event-study giornaliero non è lo strumento giusto),
positioning net-short (dati COT settimanali, campione intrinsecamente più
piccolo — lasciato con nota esplicita).

USO:
  pip install requests yfinance pandas
  python3 compute_historical_stats.py

  Sovrascrive historical_cases.json con i valori calcolati (preservando le
  voci non coperte da backtest reale). Da rilanciare periodicamente (consigliato:
  settimanale) per tenere le statistiche aggiornate con i dati più recenti.
"""

from __future__ import annotations

import json
import os
import statistics
import math
import datetime as dt

import requests
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE = os.path.join(BASE_DIR, "historical_cases.json")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "INSERISCI_LA_TUA_CHIAVE_GRATUITA")


# ---------------------------------------------------------------------------
# FETCH STORICO — versioni "lunghe" per il backtest (non le cache giornaliere
# usate dal monitoraggio live, qui serve la serie completa)
# ---------------------------------------------------------------------------

def fetch_fred_full_series(series_id: str, anni: int = 10) -> pd.Series:
    url = "https://api.stlouisfed.org/fred/series/observations"
    data_inizio = (dt.date.today() - dt.timedelta(days=365 * anni)).isoformat()
    params = {
        "series_id": series_id,
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "observation_start": data_inizio,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    oss = r.json()["observations"]
    date = [dt.date.fromisoformat(o["date"]) for o in oss if o["value"] != "."]
    valori = [float(o["value"]) for o in oss if o["value"] != "."]
    return pd.Series(valori, index=pd.to_datetime(date)).sort_index()


def fetch_price_series(ticker: str, periodo: str) -> pd.Series:
    if yf is None:
        raise ImportError("Esegui: pip install yfinance")
    data = yf.download(ticker, period=periodo, interval="1d", progress=False)
    close = data["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    return close


def fetch_fear_greed_history(giorni: int = 1825) -> pd.Series:
    r = requests.get(f"https://api.alternative.me/fng/?limit={giorni}", timeout=30)
    r.raise_for_status()
    dati = r.json()["data"]
    date = [dt.date.fromtimestamp(int(d["timestamp"])) for d in dati]
    valori = [int(d["value"]) for d in dati]
    return pd.Series(valori, index=pd.to_datetime(date)).sort_index()


# ---------------------------------------------------------------------------
# EVENT STUDY — la logica centrale del backtest
# ---------------------------------------------------------------------------

def _trova_inizio_episodi(condizione: pd.Series) -> list:
    """Restituisce solo il PRIMO giorno di ogni episodio (transizione False->True)."""
    inizi = []
    precedente = False
    for data, valore in condizione.items():
        if bool(valore) and not precedente:
            inizi.append(data)
        precedente = bool(valore)
    return inizi


def test_significativita(hit_rate: float, n: int) -> dict:
    """
    Verifica se l'hit-rate osservato è statisticamente diverso dal 50%
    (il caso puro — una moneta lanciata), tenendo conto della dimensione
    del campione. Sostituisce una soglia fissa arbitraria (es. "65%"):
    un 58% su 200 casi può essere più affidabile di un 75% su 8 casi.

    Usa un test z one-tailed (approssimazione normale alla binomiale,
    valida per n>=20): chiediamo "quanto è probabile vedere un hit-rate
    così alto per puro caso, se in realtà il segnale non valesse nulla?"
    Un p-value basso (<0.05 = soglia standard al 95% di confidenza)
    significa: molto improbabile che sia solo fortuna.
    """
    if n < 20:
        return {
            "z_score": None, "p_value": None, "significativo_95": None,
            "nota": f"campione troppo piccolo per il test (n={n}, servono almeno 20)",
        }
    p0 = 0.5
    errore_standard = math.sqrt(p0 * (1 - p0) / n)
    z = (hit_rate - p0) / errore_standard
    p_value_one_tailed = 1 - 0.5 * (1 + math.erf(z / math.sqrt(2)))

    return {
        "z_score": round(z, 2),
        "p_value": round(p_value_one_tailed, 4),
        "significativo_95": bool(p_value_one_tailed < 0.05),
    }


def event_study(prezzo: pd.Series, date_episodi: list, orizzonte_giorni: int = 10,
                 direzione_attesa: int = -1) -> dict | None:
    """
    Per ogni episodio, misura il rendimento del prezzo nei giorni successivi.
    direzione_attesa: -1 se ci si aspetta un calo, +1 se un rialzo.

    Oltre a hit-rate e movimento mediano, calcola:
    - expectancy: hit_rate * |movimento mediano| — un segnale che vince
      raramente ma con movimenti ampi può valere più di uno che vince spesso
      con movimenti minuscoli. L'hit-rate da solo non basta a giudicare
      la qualità di un segnale.
    - stabilità split-half: divide il campione a metà cronologicamente e
      confronta l'hit-rate delle due metà. Se il numero crolla nella
      seconda metà, è un forte indizio che il pattern misurato sul
      campione intero sia rumore statistico, non un vero edge — un
      controllo che nessun sistema "veloce" fa, ma che fa la differenza
      tra un numero affidabile e uno illusorio.
    """
    movimenti, giorni_picco, giorni_esaurimento = [], [], []
    date_episodi_ordinate = sorted(date_episodi)

    for data_inizio in date_episodi_ordinate:
        date_future = prezzo.index[prezzo.index >= pd.Timestamp(data_inizio)]
        if len(date_future) == 0:
            continue
        pos = prezzo.index.get_loc(date_future[0])
        if pos + orizzonte_giorni >= len(prezzo):
            continue  # non abbastanza dati futuri per questo episodio, scarto

        base = prezzo.iloc[pos]
        finestra = prezzo.iloc[pos:pos + orizzonte_giorni + 1]
        rendimenti = (finestra / base - 1) * 100

        movimenti.append(float(rendimenti.iloc[-1]))

        idx_picco = rendimenti.abs().idxmax()
        pos_picco = list(rendimenti.index).index(idx_picco)
        giorni_picco.append(pos_picco)

        valore_picco = rendimenti.loc[idx_picco]
        soglia_esaurimento = abs(valore_picco) * 0.15
        post_picco = rendimenti.iloc[pos_picco:]
        esaurito = post_picco[post_picco.abs() <= soglia_esaurimento]
        pos_esaurimento = list(rendimenti.index).index(esaurito.index[0]) if len(esaurito) else orizzonte_giorni
        giorni_esaurimento.append(pos_esaurimento)

    if not movimenti:
        return None

    def _hit_rate(lista_movimenti: list) -> float:
        if not lista_movimenti:
            return 0.0
        hits = sum(1 for m in lista_movimenti if (m < 0) == (direzione_attesa < 0))
        return hits / len(lista_movimenti)

    hit_rate_totale = _hit_rate(movimenti)
    mediana_move = statistics.median(movimenti)
    expectancy = round(hit_rate_totale * abs(mediana_move), 3)

    meta = len(movimenti) // 2
    hit_rate_prima_meta = _hit_rate(movimenti[:meta]) if meta > 0 else None
    hit_rate_seconda_meta = _hit_rate(movimenti[meta:]) if meta > 0 else None
    stabile = None
    if hit_rate_prima_meta is not None and hit_rate_seconda_meta is not None:
        stabile = abs(hit_rate_prima_meta - hit_rate_seconda_meta) <= 0.20  # tolleranza 20pp

    significativita = test_significativita(hit_rate_totale, len(movimenti))

    return {
        "n_casi_indicativi": len(movimenti),
        "hit_rate_calcolato": round(hit_rate_totale, 3),
        "mediana_move_pct": round(mediana_move, 2),
        "picco_giorni_calcolato": round(statistics.median(giorni_picco), 1),
        "esaurimento_giorni_calcolato": round(statistics.median(giorni_esaurimento), 1),
        "expectancy": expectancy,
        "hit_rate_prima_meta": round(hit_rate_prima_meta, 3) if hit_rate_prima_meta is not None else None,
        "hit_rate_seconda_meta": round(hit_rate_seconda_meta, 3) if hit_rate_seconda_meta is not None else None,
        "stabile_nel_tempo": stabile,
        "p_value": significativita["p_value"],
        "z_score": significativita["z_score"],
        "significativo_95": significativita["significativo_95"],
        "calcolato_il": dt.date.today().isoformat(),
    }


# ---------------------------------------------------------------------------
# ORCHESTRAZIONE — un backtest per ciascuno dei 3 segnali coperti
# ---------------------------------------------------------------------------

def backtest_vix() -> dict | None:
    print("Scarico storico VIX e S&P500 (10 anni)...")
    vix = fetch_price_series("^VIX", "10y")
    spy = fetch_price_series("SPY", "10y")
    condizione = vix > 25
    episodi = _trova_inizio_episodi(condizione)
    print(f"  {len(episodi)} episodi trovati (VIX > 25)")
    # direzione_attesa=+1: i dati reali mostrano mean-reversion (rimbalzo)
    # dopo un picco di VIX, non prosecuzione del calo come inizialmente ipotizzato
    return event_study(spy, episodi, orizzonte_giorni=10, direzione_attesa=1)


def backtest_spread_hy() -> dict | None:
    if not FRED_API_KEY or "INSERISCI" in FRED_API_KEY:
        print("FRED_API_KEY non configurata, salto il backtest dello spread HY")
        return None
    print("Scarico storico spread High Yield (FRED) e S&P500 (10 anni)...")
    hy = fetch_fred_full_series("BAMLH0A0HYM2", anni=10)
    spy = fetch_price_series("SPY", "10y")

    # un singolo giorno di rialzo è troppo rumoroso (scatta ~1 giorno su 8,
    # non è un vero segnale di stress) — cerchiamo invece un allargamento
    # anomalo: variazione su 5 giorni superiore all'85° percentile storico,
    # un vero "shock", non normale rumore di mercato
    variazione_5gg = hy.diff(5)
    soglia_shock = variazione_5gg.quantile(0.85)
    condizione = variazione_5gg > soglia_shock
    episodi = _trova_inizio_episodi(condizione)
    print(f"  {len(episodi)} episodi trovati (shock spread HY, soglia {soglia_shock:.3f})")
    return event_study(spy, episodi, orizzonte_giorni=10, direzione_attesa=-1)


def backtest_crypto_fear_greed() -> dict | None:
    print("Scarico storico Fear&Greed crypto e Bitcoin (5 anni)...")
    fg = fetch_fear_greed_history(giorni=1825)
    btc = fetch_price_series("BTC-USD", "5y")
    condizione = fg < 25
    episodi = _trova_inizio_episodi(condizione)
    print(f"  {len(episodi)} episodi trovati (Fear&Greed < 25)")
    return event_study(btc, episodi, orizzonte_giorni=14, direzione_attesa=1)


def main():
    with open(OUTPUT_FILE) as f:
        playbook = json.load(f)

    risultati = {
        "VIX elevato": backtest_vix(),
        "spread credito in allargamento": backtest_spread_hy(),
        "crypto in paura estrema": backtest_crypto_fear_greed(),
    }

    for nome, risultato in risultati.items():
        if risultato is None:
            print(f"[{nome}] backtest non disponibile, mantengo il valore precedente")
            continue

        voce = playbook.get(nome, {})
        # aggiorno con i valori calcolati, mantenendo la struttura esistente
        voce["n_casi_indicativi"] = risultato["n_casi_indicativi"]
        voce["orizzonte_anni"] = 10 if nome != "crypto in paura estrema" else 5
        voce["ci_95"] = "calcolato su dati reali (vedi n_casi per l'ampiezza campionaria)"
        voce["calcolato_il"] = risultato["calcolato_il"]

        # aggiorno la chiave hit_rate esistente, qualunque fosse il suo nome originale
        hit_rate_key = next((k for k in voce if k.startswith("hit_rate")), "hit_rate_calcolato")
        voce[hit_rate_key] = risultato["hit_rate_calcolato"]

        move_key = next((k for k in voce if k.startswith("mediana_move")), "mediana_move_5gg")
        voce[move_key] = f"{risultato['mediana_move_pct']:+.2f}%"

        voce["picco_giorni"] = risultato["picco_giorni_calcolato"]
        voce["esaurimento_giorni"] = risultato["esaurimento_giorni_calcolato"]
        voce["expectancy"] = risultato["expectancy"]
        voce["stabile_nel_tempo"] = risultato["stabile_nel_tempo"]
        voce["hit_rate_prima_meta_campione"] = risultato["hit_rate_prima_meta"]
        voce["hit_rate_seconda_meta_campione"] = risultato["hit_rate_seconda_meta"]
        voce["p_value"] = risultato["p_value"]
        voce["significativo_95"] = risultato["significativo_95"]
        voce.pop("picco_ore", None)  # normalizzo tutto a giorni per coerenza

        playbook[nome] = voce
        stabilita_str = "stabile" if risultato["stabile_nel_tempo"] else "INSTABILE (attenzione)" if risultato["stabile_nel_tempo"] is False else "n/d"
        sig_str = f"p={risultato['p_value']}, {'SIGNIFICATIVO' if risultato['significativo_95'] else 'non significativo'}" if risultato["p_value"] is not None else "n/d (campione piccolo)"
        print(f"[{nome}] aggiornato: hit-rate {risultato['hit_rate_calcolato']:.0%}, "
              f"mediana {risultato['mediana_move_pct']:+.2f}%, n={risultato['n_casi_indicativi']}, "
              f"expectancy {risultato['expectancy']}, {stabilita_str} "
              f"(1ª metà: {risultato['hit_rate_prima_meta']:.0%}, 2ª metà: {risultato['hit_rate_seconda_meta']:.0%})")
        print(f"           test statistico: {sig_str}")

    playbook["_nota"] = ("Valori per VIX elevato, spread credito in allargamento e crypto in paura "
                          "estrema sono calcolati con backtest reale (event study su dati storici "
                          "yfinance/FRED/alternative.me). Curva invertita e positioning net-short "
                          "restano stime illustrative — orizzonte troppo lungo o campione troppo "
                          "piccolo per un backtest giornaliero affidabile.")

    with open(OUTPUT_FILE, "w") as f:
        json.dump(playbook, f, indent=2, ensure_ascii=False)

    print(f"\nSalvato in {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
