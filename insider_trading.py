"""
INSIDER TRADING MONITOR — SEC EDGAR, gratuito, nessuna chiave richiesta
==========================================================================
Monitora le transazioni (Form 4) di dirigenti e board sui titoli seguiti.
Quando un dirigente acquista azioni della propria azienda con soldi propri
(non stock option esercitate) è un segnale che pochissimi investitori
retail guardano, ma che i professionisti seguono sempre — è informazione
resa pubblica per legge entro 2 giorni lavorativi dalla transazione.

FONTE: SEC EDGAR (data.sec.gov), completamente pubblica e gratuita.
Richiede un User-Agent identificativo nelle richieste (policy SEC, non
serve registrazione — basta un'email valida nell'header).
"""

import requests
import xml.etree.ElementTree as ET
import datetime as dt
import os
import json
import time

SEC_HEADERS = {
    # La SEC richiede un User-Agent con un contatto valido — sostituisci
    # con la tua email reale, è una policy tecnica, non serve registrarsi.
    "User-Agent": "MarketEngine luca.pavoloni@example.com"
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TICKER_CACHE_FILE = os.path.join(BASE_DIR, "sec_ticker_cache.json")

# Titoli monitorati — modifica/aggiungi qui i ticker che ti interessano
TITOLI_MONITORATI = {
    # Mega-cap tecnologici
    "AAPL": {"nome_tr": "Apple Inc.", "isin": "US0378331005"},
    "MSFT": {"nome_tr": "Microsoft Corp.", "isin": "US5949181045"},
    "NVDA": {"nome_tr": "NVIDIA Corp.", "isin": "US67066G1040"},
    "GOOGL": {"nome_tr": "Alphabet Inc. Class A", "isin": "US02079K3059"},
    "AMZN": {"nome_tr": "Amazon.com Inc.", "isin": "US0231351067"},
    "META": {"nome_tr": "Meta Platforms Inc.", "isin": "US30303M1027"},
    "TSLA": {"nome_tr": "Tesla Inc.", "isin": "US88160R1014"},
    "AMD": {"nome_tr": "Advanced Micro Devices", "isin": "US0079031078"},
    "NFLX": {"nome_tr": "Netflix Inc.", "isin": "US64110L1061"},
    # Finanza
    "JPM": {"nome_tr": "JPMorgan Chase & Co.", "isin": "US46625H1005"},
    # Proxy azionari del settore crypto — non esiste un "insider" diretto per
    # Bitcoin/Ethereum (non hanno dirigenti legali), ma queste società quotate
    # danno un segnale indiretto genuino di fiducia nel settore
    "COIN": {"nome_tr": "Coinbase Global Inc.", "isin": "US19260Q1076"},
    "MSTR": {"nome_tr": "Strategy Inc. (ex MicroStrategy)", "isin": "US5949724083"},
    "MARA": {"nome_tr": "MARA Holdings Inc.", "isin": "US5658491064"},
    "RIOT": {"nome_tr": "Riot Platforms Inc.", "isin": "US76766A2091"},
}


def _load_ticker_map() -> dict:
    """Mappa ticker -> CIK (identificativo SEC), scaricata una volta e riusata da cache."""
    if os.path.exists(TICKER_CACHE_FILE):
        with open(TICKER_CACHE_FILE) as f:
            return json.load(f)

    r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_HEADERS, timeout=20)
    r.raise_for_status()
    dati = r.json()
    mappa = {v["ticker"]: str(v["cik_str"]).zfill(10) for v in dati.values()}

    with open(TICKER_CACHE_FILE, "w") as f:
        json.dump(mappa, f)
    return mappa


def fetch_recent_form4(ticker: str, giorni: int = 30) -> list[dict]:
    """Recupera l'elenco dei Form 4 (transazioni insider) recenti per un ticker."""
    mappa = _load_ticker_map()
    cik = mappa.get(ticker.upper())
    if not cik:
        print(f"[insider] ticker {ticker} non trovato in SEC EDGAR")
        return []

    r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=SEC_HEADERS, timeout=20)
    r.raise_for_status()
    dati = r.json()

    recenti = dati["filings"]["recent"]
    soglia_data = dt.date.today() - dt.timedelta(days=giorni)

    filings = []
    for i, tipo in enumerate(recenti["form"]):
        if tipo != "4":
            continue
        data_deposito = dt.date.fromisoformat(recenti["filingDate"][i])
        if data_deposito < soglia_data:
            continue
        filings.append({
            "cik": cik,
            "accession": recenti["accessionNumber"][i].replace("-", ""),
            "primary_doc": recenti["primaryDocument"][i],
            "data": data_deposito.isoformat(),
        })
    return filings


def parse_form4(filing: dict) -> list[dict]:
    """Scarica e interpreta l'XML di un singolo Form 4, estraendo le transazioni."""
    url = f"https://www.sec.gov/Archives/edgar/data/{int(filing['cik'])}/{filing['accession']}/{filing['primary_doc']}"
    try:
        r = requests.get(url, headers=SEC_HEADERS, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"[insider] errore lettura {url}: {e}")
        return []

    transazioni = []
    for t in root.findall(".//nonDerivativeTransaction"):
        try:
            codice = t.find(".//transactionCode").text  # P=acquisto, S=vendita
            azioni = float(t.find(".//transactionShares/value").text)
            prezzo_el = t.find(".//transactionPricePerShare/value")
            prezzo = float(prezzo_el.text) if prezzo_el is not None else None
            transazioni.append({"codice": codice, "azioni": azioni, "prezzo": prezzo})
        except (AttributeError, ValueError, TypeError):
            continue
    return transazioni


def summarize_insider_activity(ticker: str, giorni: int = 30) -> dict:
    """
    Aggrega tutte le transazioni insider recenti su un titolo:
    numero di acquisti, vendite, e saldo netto in azioni.
    """
    filings = fetch_recent_form4(ticker, giorni)
    transazioni_totali = []
    for filing in filings:
        transazioni_totali.append(parse_form4(filing))
        time.sleep(0.2)  # piccola pausa, buona pratica verso un servizio pubblico gratuito

    n_acquisti, n_vendite, azioni_nette = 0, 0, 0.0
    for transazioni in transazioni_totali:
        for t in transazioni:
            if t["codice"] == "P":
                n_acquisti += 1
                azioni_nette += t["azioni"]
            elif t["codice"] == "S":
                n_vendite += 1
                azioni_nette -= t["azioni"]

    return {
        "ticker": ticker,
        "n_filing": len(filings),
        "n_acquisti": n_acquisti,
        "n_vendite": n_vendite,
        "azioni_nette": azioni_nette,
        "segnale": "acquisto netto" if azioni_nette > 0 and n_acquisti >= 2 else
                   "vendita netta" if azioni_nette < 0 and n_vendite >= 2 else "misto/nessun segnale chiaro",
    }


if __name__ == "__main__":
    for ticker in TITOLI_MONITORATI:
        print(f"\n--- {ticker} ---")
        riepilogo = summarize_insider_activity(ticker, giorni=30)
        print(riepilogo)
