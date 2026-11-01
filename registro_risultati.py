"""
REGISTRO DEI RISULTATI
======================
Fotografa ogni segnale quando scatta (anche quelli "solo log", non inviati),
con la previsione storica di QUEL momento, e dopo N giorni di borsa misura
cosa è successo davvero. È il test fuori campione del sistema.

File (scritti dal bot su GitHub Actions):
  registro_risultati.jsonl  una riga per episodio di segnale
  registro_stato.json       episodi ancora aperti (anti-doppioni)
  report_risultati.md       report completo, rigenerato a ogni esecuzione

Regole fissate PRIMA di vedere i dati:
  - un episodio si registra una sola volta finché il segnale resta attivo;
  - se l'episodio era già in corso quando è stato registrato (es. drawdown
    iniziato prima dell'attivazione del registro), si salvano la data reale di
    inizio e "gia_in_corso_alla_registrazione": l'esito viene misurato ma NON
    confrontato con lo storico, che parte sempre dal giorno 0 dell'episodio;
  - esito = chiusura N giorni di borsa dopo la data iniziale, sullo stesso
    asset e orizzonte usati dal backtest (close-to-close, come il backtest);
  - calibrazione: circa il 50% degli esiti dovrebbe cadere nell'intervallo
    storico 25°-75°;
  - un segnale verificato che dopo almeno MIN_N esiti non batte la frequenza
    normale (p < ALFA, stessa direzione) va riportato a "solo log";
  - nessun segnale diventa verificato grazie al registro sotto MIN_N esiti.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None

from statistiche import ALFA, MIN_N, p_bilaterale

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRO_FILE = os.path.join(BASE_DIR, "registro_risultati.jsonl")
STATO_FILE = os.path.join(BASE_DIR, "registro_stato.json")
REPORT_FILE = os.path.join(BASE_DIR, "report_risultati.md")
URL_REPORT = "https://github.com/Brucius95/market-engine/blob/main/report_risultati.md"
TZ_ROMA = ZoneInfo("Europe/Rome")
GIORNI_EXTRA_PRIMA_DI_ARRENDERSI = 30   # oltre: esito "non misurabile"
LIMITE_RIEPILOGO_BYTE = 3800
# giorno della prima esecuzione del registro: i segnali macro registrati quel
# giorno erano con ogni probabilità già attivi da prima, ma la loro data di
# inizio non è ricostruibile -> "già in corso, data non nota"
DATA_ATTIVAZIONE_REGISTRO = "2026-11-01"            # ntfy accetta ~4 KB per messaggio

ETICHETTA_STATO = {"push_consegnata": "push", "push_fallita": "push fallita", "solo_log": "solo log"}


# ---------------------------------------------------------------- file
def inizializza() -> None:
    """Crea i file vuoti se mancano (il workflow fa git add su file che devono esistere)."""
    if not os.path.exists(REGISTRO_FILE):
        open(REGISTRO_FILE, "w").close()
    if not os.path.exists(STATO_FILE):
        _salva_stato({"aperti": {}})
    if not os.path.exists(REPORT_FILE):
        scrivi_report()


def _carica_registro() -> list[dict]:
    if not os.path.exists(REGISTRO_FILE):
        return []
    with open(REGISTRO_FILE) as f:
        return [json.loads(r) for r in f if r.strip()]


def _salva_registro(righe: list[dict]) -> None:
    with open(REGISTRO_FILE, "w") as f:
        for r in righe:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _carica_stato() -> dict:
    try:
        with open(STATO_FILE) as f:
            return json.load(f)
    except Exception:
        return {"aperti": {}}


def _salva_stato(stato: dict) -> None:
    with open(STATO_FILE, "w") as f:
        json.dump(stato, f, indent=1, ensure_ascii=False)


# ---------------------------------------------------------------- prezzi
def _storico(ticker: str, inizio: dt.date | None = None) -> pd.Series | None:
    """Chiusure giornaliere aggiustate (come i backtest), indice = date senza fuso."""
    if yf is None:
        return None
    try:
        if inizio is None:
            h = yf.Ticker(ticker).history(period="1mo", auto_adjust=True)
        else:
            h = yf.Ticker(ticker).history(start=(inizio - dt.timedelta(days=7)).isoformat(), auto_adjust=True)
        s = h["Close"].dropna()
        if s.empty:
            return None
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        return s[~s.index.duplicated(keep="last")]
    except Exception as e:
        print(f"[registro] prezzi non disponibili per {ticker}: {e}")
        return None


# ---------------------------------------------------------------- registrazione
def _snapshot_previsione(previsione: dict | None) -> dict | None:
    """Copia del verdetto valido al momento del segnale (solo se calcolato con il test corretto)."""
    if not previsione or "pct_rialzo_normale" not in previsione:
        return None
    return {k: v for k, v in previsione.items() if k != "metodo_test"}


def segnale_attivo(chiave: str, modulo: str, segnale: str, posizione: str, isin: str,
                   ticker_misura: str, orizzonte: int, previsione: dict | None,
                   stato_invio: str, valore: str = "",
                   inizio_reale: tuple[str, int] | None = None) -> None:
    """
    inizio_reale: (data del primo giorno dell'episodio, giorni di borsa già trascorsi
    alla registrazione), se il modulo sa calcolarla (oggi: drawdown).
    Registra l'inizio di un episodio (una sola volta finché resta attivo).
    Se l'episodio è già aperto, aggiorna solo lo stato d'invio quando una push
    prima fallita viene poi consegnata.
    """
    stato = _carica_stato()
    aperti = stato.setdefault("aperti", {})
    righe = _carica_registro()

    if chiave in aperti:
        if stato_invio == "push_consegnata":
            for r in righe:
                if r["id"] == aperti[chiave] and r["stato_invio"] != "push_consegnata":
                    r["stato_invio"] = "push_consegnata"
                    _salva_registro(righe)
                    print(f"  [registro] {chiave}: push consegnata (prima fallita)")
                    break
        return

    serie = _storico(ticker_misura)
    if serie is None:
        print(f"  [registro] {chiave}: prezzo di {ticker_misura} non disponibile, riprovo alla prossima esecuzione")
        return

    snap = _snapshot_previsione(previsione)
    if snap:
        orizzonte = int(snap.get("orizzonte_giorni", orizzonte))
    ora = dt.datetime.now(TZ_ROMA)
    data_iniziale = serie.index[-1].date()
    rec = {
        "id": f"{ora.strftime('%Y%m%d%H%M')}_{chiave}",
        "chiave": chiave,
        "quando_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "quando_it": ora.strftime("%Y-%m-%d %H:%M %Z"),
        "modulo": modulo,
        "segnale": segnale,
        "valore": valore,
        "posizione": posizione,
        "isin": isin,
        "ticker_misura": ticker_misura,
        "data_iniziale": data_iniziale.isoformat(),
        "prezzo_iniziale": round(float(serie.iloc[-1]), 6),
        "orizzonte_giorni": orizzonte,
        "stato_invio": stato_invio,
        "previsione": snap,
        "esito": None,
    }
    if inizio_reale:
        _imposta_inizio_reale(rec, inizio_reale)
    righe.append(rec)
    _salva_registro(righe)
    aperti[chiave] = rec["id"]
    _salva_stato(stato)
    print(f"  [registro] nuovo episodio: {segnale} ({ETICHETTA_STATO.get(stato_invio, stato_invio)})")


def episodio_aperto(chiave: str) -> bool:
    return chiave in _carica_stato().get("aperti", {})


def _imposta_inizio_reale(rec: dict, inizio_reale: tuple[str, int]) -> None:
    data, giorni = inizio_reale
    rec["inizio_reale"] = data
    rec["giorni_gia_trascorsi"] = int(giorni)
    rec["gia_in_corso_alla_registrazione"] = bool(data < rec["data_iniziale"])


def completa_inizi_reali(calcola) -> int:
    """
    Per i segnali macro registrati il giorno di attivazione del registro: già in
    corso, data di inizio non nota.
    Per i drawdown registrati prima che esistesse questo campo, calcola la data
    reale di inizio con calcola(ticker, data_iniziale) -> (data, giorni) | None.
    Idempotente: le righe che hanno già il campo non vengono toccate.
    """
    righe = _carica_registro()
    cambiate = 0
    for r in righe:
        if r["modulo"] != "drawdown" or "inizio_reale" in r:
            continue
        risultato = calcola(r["ticker_misura"], r["data_iniziale"])
        if not risultato:
            continue
        _imposta_inizio_reale(r, risultato)
        if r["esito"] and r["gia_in_corso_alla_registrazione"]:
            r["esito"]["dentro_intervallo_25_75"] = None
            r["esito"]["direzione_corretta"] = None
            r["esito"]["confronto_con_storico"] = False
        cambiate += 1
        print(f"  [registro] {r['segnale']}: inizio reale {r['inizio_reale']} "
              f"({r['giorni_gia_trascorsi']} giorni di borsa prima della registrazione)")
    for r in righe:
        if (r["modulo"] == "macro" and "gia_in_corso_alla_registrazione" not in r
                and r["quando_it"][:10] == DATA_ATTIVAZIONE_REGISTRO):
            r["inizio_reale"] = None
            r["giorni_gia_trascorsi"] = None
            r["gia_in_corso_alla_registrazione"] = True
            if r["esito"]:
                r["esito"]["dentro_intervallo_25_75"] = None
                r["esito"]["direzione_corretta"] = None
                r["esito"]["confronto_con_storico"] = False
            cambiate += 1
            print(f"  [registro] {r['segnale']}: già attivo all'attivazione del registro, data di inizio non nota")
    if cambiate:
        _salva_registro(righe)
    return cambiate


def segnale_inattivo(chiave: str) -> None:
    """Chiude l'episodio: la prossima attivazione sarà un nuovo episodio."""
    stato = _carica_stato()
    if stato.get("aperti", {}).pop(chiave, None) is not None:
        _salva_stato(stato)


# ---------------------------------------------------------------- esiti
def _calcola_esito(rec: dict, serie: pd.Series, oggi: dt.date) -> dict | None:
    n = rec["orizzonte_giorni"]
    d0 = pd.Timestamp(rec["data_iniziale"])
    pos = int(serie.index.searchsorted(d0, side="right")) - 1
    if pos < 0 or serie.index[pos] != d0:
        pos = -1  # data iniziale assente nello storico
    if pos >= 0 and pos + n < len(serie):
        p0, p1 = float(serie.iloc[pos]), float(serie.iloc[pos + n])
        rend = p1 / p0 - 1
        prev = rec.get("previsione")
        dentro = verso = None
        confronto = not rec.get("gia_in_corso_alla_registrazione", False)
        if prev and confronto:
            suf = f"_{n}g"
            if ("p25" + suf) in prev and ("p75" + suf) in prev:
                dentro = bool(prev["p25" + suf] <= rend <= prev["p75" + suf])
            if prev.get("significativo"):
                verso = bool((rend >= 0) == (prev.get("direzione") == "RIALZO"))
        return {"stato": "misurato", "misurato_il": oggi.isoformat(),
                "data_finale": serie.index[pos + n].date().isoformat(),
                "prezzo_iniziale_chiusura": round(p0, 6), "prezzo_finale": round(p1, 6),
                "rendimento": round(rend, 5), "in_rialzo": bool(rend >= 0),
                "dentro_intervallo_25_75": dentro, "direzione_corretta": verso,
                "confronto_con_storico": confronto}
    giorni_passati = (oggi - d0.date()).days
    if giorni_passati > n * 2 + GIORNI_EXTRA_PRIMA_DI_ARRENDERSI:
        return {"stato": "non_misurabile", "misurato_il": oggi.isoformat(),
                "motivo": "dati di prezzo insufficienti dopo la data iniziale"}
    return None


def aggiorna_esiti() -> int:
    """Compila l'esito delle righe con orizzonte scaduto. Restituisce quante ne ha chiuse."""
    righe = _carica_registro()
    oggi = dt.date.today()
    da_fare: dict[str, list[dict]] = {}
    for r in righe:
        # filtro economico: prima di N giorni di calendario non può essere scaduto
        if r["esito"] is None and (oggi - dt.date.fromisoformat(r["data_iniziale"])).days >= r["orizzonte_giorni"]:
            da_fare.setdefault(r["ticker_misura"], []).append(r)
    chiusi = 0
    for ticker, gruppo in da_fare.items():
        serie = _storico(ticker, inizio=min(dt.date.fromisoformat(r["data_iniziale"]) for r in gruppo))
        if serie is None:
            # nessun dato: dopo il tempo massimo l'esito diventa "non misurabile"
            serie = pd.Series(dtype=float, index=pd.DatetimeIndex([]))
        for r in gruppo:
            esito = _calcola_esito(r, serie, oggi)
            if esito:
                r["esito"] = esito
                chiusi += 1
    if chiusi:
        _salva_registro(righe)
        print(f"[registro] {chiusi} esiti compilati")
    return chiusi


# ---------------------------------------------------------------- statistiche
def _pct(x: float | None, segno: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x:+.1%}" if segno else f"{x:.0%}"


def _p_txt(p: float) -> str:
    return "< 0.01" if p < 0.01 else f"{p:.2f}"


def _statistiche_per_segnale(righe: list[dict]) -> list[dict]:
    gruppi: dict[str, list[dict]] = {}
    for r in righe:
        gruppi.setdefault(r["segnale"], []).append(r)
    out = []
    for segnale, rr in gruppi.items():
        tutti_misurati = [r for r in rr if r["esito"] and r["esito"]["stato"] == "misurato"]
        # gli episodi già in corso alla registrazione non partono dal giorno 0:
        # misurati, ma esclusi dal confronto con lo storico
        misurati = [r for r in tutti_misurati if not r.get("gia_in_corso_alla_registrazione")]
        rend = [r["esito"]["rendimento"] for r in misurati]
        con_int = [r for r in misurati if r["esito"]["dentro_intervallo_25_75"] is not None]
        ultima_prev = next((r["previsione"] for r in reversed(rr) if r["previsione"]), None)
        verificato = bool(ultima_prev and ultima_prev.get("significativo"))
        basi = [r["previsione"]["pct_rialzo_normale"] for r in misurati if r["previsione"]]
        k_up = sum(1 for x in rend if x >= 0)
        giudizio = ""
        if verificato:
            if len(misurati) < MIN_N:
                giudizio = f"verificato, in osservazione ({len(misurati)}/{MIN_N} esiti)"
            else:
                base = sum(basi) / len(basi) if basi else 0.5
                p = p_bilaterale(k_up, len(rend), base)
                stesso_verso = (k_up / len(rend) > base) == (ultima_prev.get("direzione") == "RIALZO")
                giudizio = ("confermato fuori campione" if (p < ALFA and stesso_verso)
                            else "DA RIPORTARE A SOLO LOG")
        out.append({
            "segnale": segnale, "episodi": len(rr), "misurati": len(misurati),
            "gia_in_corso": len(tutti_misurati) - len(misurati),
            "in_attesa": sum(1 for r in rr if r["esito"] is None),
            "pct_rialzo": (k_up / len(rend)) if rend else None,
            "storico": ultima_prev.get("pct_rialzo_dopo") if ultima_prev else None,
            "normale": ultima_prev.get("pct_rialzo_normale") if ultima_prev else None,
            "mediana": float(pd.Series(rend).median()) if rend else None,
            "dentro": (sum(1 for r in con_int if r["esito"]["dentro_intervallo_25_75"]) / len(con_int)) if con_int else None,
            "n_dentro": len(con_int),
            "verificato": verificato, "giudizio": giudizio,
            "push": sum(1 for r in rr if r["stato_invio"] == "push_consegnata"),
        })
    return sorted(out, key=lambda x: (-x["misurati"], x["segnale"]))


def _calibrazione(righe: list[dict]) -> tuple[int, int, float | None]:
    vals = [r["esito"]["dentro_intervallo_25_75"] for r in righe
            if r["esito"] and r["esito"]["stato"] == "misurato" and r["esito"]["dentro_intervallo_25_75"] is not None]
    k, n = sum(vals), len(vals)
    return k, n, (p_bilaterale(k, n, 0.5) if n else None)


def _riga_calibrazione(righe: list[dict]) -> str:
    k, n, p = _calibrazione(righe)
    if not n:
        return "Calibrazione: ancora nessun esito con intervallo storico"
    return (f"Calibrazione: {k} esiti su {n} ({k / n:.0%}) dentro l'intervallo 25°-75° "
            f"(atteso circa 50%, p {_p_txt(p)})")


# ---------------------------------------------------------------- report
def _gia_in_corso_txt(r: dict) -> str:
    if "gia_in_corso_alla_registrazione" not in r:
        return "—"
    if not r["gia_in_corso_alla_registrazione"]:
        return "no"
    if not r.get("inizio_reale"):
        return "sì, data di inizio non nota: non confrontato con lo storico"
    return f"sì, dal {r['inizio_reale']} (giorno {r['giorni_gia_trascorsi']}): non confrontato con lo storico"


def scrivi_report() -> str:
    """Rigenera report_risultati.md. Nessun orario di generazione: il file cambia solo se cambiano i dati."""
    righe = _carica_registro()
    stats = _statistiche_per_segnale(righe)
    ultimo = max((r["quando_it"] for r in righe), default="nessuno")
    L = [
        "# Registro dei risultati",
        "",
        f"Episodi registrati: **{len(righe)}** · esiti misurati: "
        f"**{sum(1 for r in righe if r['esito'] and r['esito']['stato'] == 'misurato')}** · "
        f"in attesa: **{sum(1 for r in righe if r['esito'] is None)}** · ultimo evento: {ultimo}",
        "",
        _riga_calibrazione(righe),
        "",
        "Regole: esito = chiusura dopo N giorni di borsa sullo stesso asset del backtest. "
        f"Un segnale verificato con almeno {MIN_N} esiti che non batte la frequenza normale "
        f"(p < {ALFA}) va riportato a \"solo log\". Nessun segnale diventa verificato con meno di {MIN_N} esiti.",
        "",
        "## Per segnale",
        "",
        "Gli episodi già in corso al momento della registrazione (es. drawdown iniziati prima "
        "dell'attivazione del registro) sono misurati ma esclusi dal confronto con lo storico: "
        "nella colonna Esiti compaiono come \"+N già in corso\".",
        "",
        "| Segnale | Episodi (push) | Esiti | In rialzo | Storico | Normale | Mediana | Dentro 25°-75° | Stato |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in stats:
        esiti_txt = f"{s['misurati']}" + (f" (+{s['gia_in_corso']} già in corso)" if s["gia_in_corso"] else "")
        L.append(f"| {s['segnale']} | {s['episodi']} ({s['push']}) | {esiti_txt} | "
                 f"{_pct(s['pct_rialzo'], False)} | {_pct(s['storico'], False)} | {_pct(s['normale'], False)} | "
                 f"{_pct(s['mediana'])} | {_pct(s['dentro'], False)} | "
                 f"{s['giudizio'] or 'non verificato'} |")
    if not stats:
        L.append("| (nessun episodio ancora) | | | | | | | | |")

    attesa = sorted((r for r in righe if r["esito"] is None), key=lambda r: r["data_iniziale"])
    L += ["", "## In attesa di esito", ""]
    if attesa:
        L += ["| Dal | Segnale | Posizione | Asset misurato | Orizzonte | Invio | Già in corso alla registrazione |",
              "|---|---|---|---|---|---|---|"]
        for r in attesa:
            L.append(f"| {r['data_iniziale']} | {r['segnale']} | {r['posizione']} | {r['ticker_misura']} | "
                     f"{r['orizzonte_giorni']} giorni di borsa | {ETICHETTA_STATO.get(r['stato_invio'], r['stato_invio'])} | "
                     f"{_gia_in_corso_txt(r)} |")
    else:
        L.append("Nessuno.")

    chiusi = sorted((r for r in righe if r["esito"] and r["esito"]["stato"] == "misurato"),
                    key=lambda r: r["esito"]["data_finale"], reverse=True)[:30]
    L += ["", "## Ultimi esiti (max 30)", ""]
    if chiusi:
        L += ["| Dal → al | Segnale | Rendimento | Dentro 25°-75° | Direzione corretta | Già in corso alla registrazione |",
              "|---|---|---|---|---|---|"]
        for r in chiusi:
            e = r["esito"]
            si_no = lambda v: "—" if v is None else ("sì" if v else "no")
            L.append(f"| {r['data_iniziale']} → {e['data_finale']} | {r['segnale']} | {_pct(e['rendimento'])} | "
                     f"{si_no(e['dentro_intervallo_25_75'])} | {si_no(e['direzione_corretta'])} | {_gia_in_corso_txt(r)} |")
    else:
        L.append("Nessuno.")
    testo = "\n".join(L) + "\n"
    with open(REPORT_FILE, "w") as f:
        f.write(testo)
    return testo


# ---------------------------------------------------------------- riepilogo della domenica
# Testo per un non esperto: niente sigle di ticker, percentili, p-value o gergo
# del sistema. Il dettaglio tecnico resta in report_risultati.md.
CALENDAR_FILE = os.path.join(BASE_DIR, "calendar_events.json")
MAX_RIGHE_RIEPILOGO = 15
# date fisse non presenti in calendar_events.json (lì farebbero scattare anche
# i promemoria automatici a 7 e 1 giorno)
EVENTI_EXTRA = [{"data": "2026-11-03", "tipo": "MIDTERM"}]
DESCRIZIONE_EVENTO = {
    "CPI": "dati sull'inflazione USA",
    "FOMC": "riunione della Fed sui tassi",
    "MIDTERM": "elezioni di metà mandato USA (midterm)",
}
NOME_SEMPLICE = {"SPY": "l'S&P 500", "BTC-USD": "il Bitcoin", "CL=F": "il petrolio", "GC=F": "l'oro",
                 "SI=F": "l'argento", "ETH-USD": "l'Ethereum", "SOL-USD": "Solana"}
TEMA_SEMPLICE = {"dazi_commercio": "dazi e commercio", "sanzioni": "sanzioni",
                 "fed_policy": "Fed e tassi", "geopolitica_energia": "geopolitica ed energia"}
DESCRIZIONE_MACRO = {
    "VIX elevato": "la paura sui mercati USA è alta",
    "spread credito in allargamento": "le aziende più indebitate stanno pagando interessi più alti",
    "positioning net-short": "i grandi investitori scommettono più sul ribasso che sul rialzo della borsa USA",
    "curva invertita": "i tassi USA a breve sono più alti di quelli a lungo",
    "crypto in paura estrema": "il clima sulle criptovalute è di paura estrema",
}
GIORNI_SETTIMANA = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]


def _gg_mm(data_iso: str) -> str:
    return f"{data_iso[8:10]}/{data_iso[5:7]}"


def _apostrofo(numero: int) -> bool:
    """In italiano 'l'/dell'' davanti a 1, 8, 11 e ai numeri che iniziano per 8 (otto, ottanta...)."""
    return numero in (1, 11) or str(numero).startswith("8")


def _il_giorno(d: dt.date) -> str:
    """'il 10/11' oppure 'l'11/11', 'l'8/11', 'l'1/12'."""
    return f"l'{d.day}/{d.month:02d}" if _apostrofo(d.day) else f"il {d.strftime('%d/%m')}"


def _data_con_prep(d: dt.date, prep: str) -> str:
    """prep = 'dal' / 'del': 'dal 29/10', 'dall'1/11', 'del 10/12', 'dell'11/11'."""
    return f"{prep[:-1]}ll'{d.day}/{d.month:02d}" if _apostrofo(d.day) else f"{prep} {d.strftime('%d/%m')}"


def _del_pct(frazione: float) -> str:
    """'del 25%' oppure 'dell'11%', 'dell'8%'."""
    n = round(abs(frazione) * 100)
    return f"dell'{n}%" if _apostrofo(n) else f"del {n}%"


def _nome_posizione(r: dict) -> str:
    """Nome della posizione senza note tecniche tra parentesi."""
    return r["posizione"].split(" (")[0].strip()


def _calo_dal_massimo_mese(ticker: str) -> float | None:
    """Variazione attuale dal massimo degli ultimi 30 giorni di borsa (stessa regola del drawdown)."""
    if yf is None:
        return None
    try:
        c = yf.Ticker(ticker).history(period="3mo", auto_adjust=True)["Close"].dropna()
        return float(c.iloc[-1] / c.iloc[-30:].max() - 1)
    except Exception:
        return None


def _descrizione_segnale(r: dict) -> str:
    """Cosa sta succedendo, in parole semplici."""
    mod, val = r["modulo"], r.get("valore", "")
    if mod == "drawdown":
        calo = _calo_dal_massimo_mese(r["ticker_misura"])
        return (f"giù {_del_pct(calo)} dal massimo dell'ultimo mese" if calo is not None
                else "in forte calo rispetto al massimo dell'ultimo mese")
    if mod == "macro":
        return DESCRIZIONE_MACRO.get(r["segnale"], r["segnale"])
    if mod == "notizie":
        tema = r["segnale"].replace("notizie ", "")
        volte = val.split("x")[0] if "x" in val else ""
        return (f"notizie su {TEMA_SEMPLICE.get(tema, tema)} {volte} volte più del solito" if volte
                else f"notizie su {TEMA_SEMPLICE.get(tema, tema)} molto più del solito")
    if mod == "insider":
        return "i dirigenti hanno comprato azioni della propria azienda"
    if mod == "azioni":
        try:
            var, data = val.split(" il ")
            return f"movimento insolito di {var.replace('.', ',')} in un giorno ({_gg_mm(data)})"
        except ValueError:
            return "movimento insolito in un giorno"
    return r["segnale"]


def _frase_storico(r: dict) -> str:
    """'Dopo cali simili ... X volte su 100, di norma Y su 100' + affidabilità in mezza frase."""
    prev = r.get("previsione")
    if not prev:
        return "nessun dato storico affidabile su cosa succede dopo"
    n = prev.get("orizzonte_giorni", r["orizzonte_giorni"])
    x, y = round(prev["pct_rialzo_dopo"] * 100), round(prev["pct_rialzo_normale"] * 100)
    if r["modulo"] == "drawdown":
        attacco, verbo = "dopo cali simili", "è risalito"
    elif r["modulo"] == "azioni":
        attacco = "dopo cali simili" if "calo" in r["segnale"] else "dopo rialzi simili"
        verbo = "è salito"
    else:
        attacco, verbo = "in situazioni simili", f"{NOME_SEMPLICE.get(r['ticker_misura'], 'il prezzo')} è salito"
    if r.get("gia_in_corso_alla_registrazione") and r["modulo"] == "drawdown":
        attacco += f", nei {n} giorni dall'inizio del calo,"
    else:
        attacco += f", nei {n} giorni successivi,"
    if prev.get("significativo"):
        giudizio = "differenza confermata dai dati"
    elif prev.get("n", 0) < MIN_N:
        giudizio = "troppo pochi casi per fidarsene"
    else:
        giudizio = "differenza troppo piccola per fidarsene"
    return f"{attacco} {verbo} {x} volte su 100, di norma {y} ({giudizio})"


def _prossimi_eventi(oggi: dt.date, quanti: int = 3) -> list[str]:
    eventi = list(EVENTI_EXTRA)
    try:
        with open(CALENDAR_FILE) as f:
            # salto il blocco di note e gli eventi ancora "di esempio"
            eventi += [e for e in json.load(f) if "id" in e and "_ATTENZIONE" not in e]
    except Exception:
        pass
    futuri = sorted((e for e in eventi if e.get("data", "") >= oggi.isoformat()), key=lambda e: e["data"])
    out = []
    for e in futuri[:quanti]:
        d = dt.date.fromisoformat(e["data"])
        desc = DESCRIZIONE_EVENTO.get(e.get("tipo", ""), e.get("nome", "evento"))
        out.append(f"- {GIORNI_SETTIMANA[d.weekday()]} {d.strftime('%d/%m')}: {desc}")
    return out


def _data_primo_esito(righe: list[dict]) -> dt.date | None:
    """Data stimata del primo esito ancora in attesa (giorni di borsa; crypto anche nel weekend)."""
    date = []
    for r in righe:
        if r["esito"] is None:
            d0 = dt.date.fromisoformat(r["data_iniziale"])
            n = r["orizzonte_giorni"]
            if r["ticker_misura"].endswith("-USD"):
                date.append(d0 + dt.timedelta(days=n))
            else:
                date.append(pd.Timestamp(d0) + pd.offsets.BDay(n))
    return min(pd.Timestamp(d).date() for d in date) if date else None


def testo_riepilogo_domenica(oggi: dt.date | None = None) -> str:
    """
    Riepilogo per un non esperto, massimo MAX_RIGHE_RIEPILOGO righe e sotto
    LIMITE_RIEPILOGO_BYTE: In breve / DA TENERE D'OCCHIO / DA GUARDARE /
    COME VA IL SISTEMA / link al report.
    """
    oggi = oggi or dt.datetime.now(TZ_ROMA).date()
    righe = _carica_registro()
    aperti = set(_carica_stato().get("aperti", {}).values())
    attivi = [r for r in righe if r["id"] in aperti]

    # un punto per posizione: prima i movimenti di prezzo della posizione stessa
    ordine = {"drawdown": 0, "azioni": 1, "insider": 2, "notizie": 3, "macro": 4}
    per_pos: dict[str, list[dict]] = {}
    for r in sorted(attivi, key=lambda r: ordine.get(r["modulo"], 9)):
        per_pos.setdefault(_nome_posizione(r), []).append(r)

    verificati = sum(1 for r in attivi if (r.get("previsione") or {}).get("significativo"))
    eventi = _prossimi_eventi(oggi)

    misurati = [r for r in righe if r["esito"] and r["esito"]["stato"] == "misurato"]
    settimana = [r for r in misurati if dt.date.fromisoformat(r["esito"]["misurato_il"]) > oggi - dt.timedelta(days=7)]
    primo = _data_primo_esito(righe)
    sistema = f"Segnali registrati finora: {len(righe)}. Risultati già verificati: {len(misurati)}"
    sistema += f" (questa settimana {len(settimana)})." if misurati else "."
    if primo and not misurati:
        sistema += f" Il primo risultato arriverà verso {_il_giorno(primo)}."
    da_togliere = [s["segnale"] for s in _statistiche_per_segnale(righe) if s["giudizio"] == "DA RIPORTARE A SOLO LOG"]
    if da_togliere:
        sistema += f" Segnali che non stanno funzionando: {len(da_togliere)} (dettagli nel report)."

    if per_pos:
        breve = (f"{len(per_pos)} posizioni da tenere d'occhio, "
                 + (f"{verificati} con una tendenza storica confermata." if verificati
                    else "nessuna con una tendenza storica affidabile."))
    else:
        breve = "Nessun segnale attivo questa settimana."

    fisse = 1 + 3 + max(len(eventi), 1) + 1 + 1   # in breve, 3 intestazioni, eventi, sistema, link (la data è nel titolo ntfy)
    posto = MAX_RIGHE_RIEPILOGO - fisse
    punti = []
    pos_list = list(per_pos.items())
    mostrabili = pos_list if len(pos_list) <= posto else pos_list[:posto - 1]
    for nome, rr in mostrabili:
        parti = []
        for r in rr:
            p = _descrizione_segnale(r)
            if r.get("gia_in_corso_alla_registrazione"):
                if r.get("inizio_reale"):
                    p += ", in corso " + _data_con_prep(dt.date.fromisoformat(r["inizio_reale"]), "dal")
                else:
                    p += ", in corso da prima " + _data_con_prep(dt.date.fromisoformat(DATA_ATTIVAZIONE_REGISTRO), "del")
            parti.append(f"{p}. {_frase_storico(r)[0].upper()}{_frase_storico(r)[1:]}.")
        punti.append(f"- {nome}: " + " ".join(parti))
    if len(mostrabili) < len(pos_list):
        punti.append(f"- e altre {len(pos_list) - len(mostrabili)} posizioni (vedi il report)")
    if not punti:
        punti = ["- nessuna"]

    def componi(pp: list[str]) -> str:
        return "\n".join([f"In breve: {breve}", "DA TENERE D'OCCHIO"] + pp +
                         ["DA GUARDARE"] + (eventi or ["- nessuna data in calendario"]) +
                         ["COME VA IL SISTEMA", sistema, f"Dettagli: {URL_REPORT}"])

    testo = componi(punti)
    # limite ntfy (~4 KB): tolgo i punti dal fondo e lo dico
    tolti = 0
    while len(testo.encode("utf-8")) > LIMITE_RIEPILOGO_BYTE and len(punti) > 1:
        punti.pop(-1)
        tolti += 1
        testo = componi(punti + [f"- e altre {tolti} posizioni (vedi il report)"])
    return testo


def titolo_riepilogo_domenica(oggi: dt.date | None = None) -> str:
    oggi = oggi or dt.datetime.now(TZ_ROMA).date()
    return f"Riepilogo della domenica - {oggi.strftime('%d/%m/%Y')}"
