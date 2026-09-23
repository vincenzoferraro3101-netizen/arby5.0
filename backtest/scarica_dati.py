"""
Scarica candele da 1 minuto da MetaTrader5 e le salva in DATI/<SIMBOLO>.csv
nel formato richiesto dal motore: time,open,high,low,close,volume,spread
(time in UTC, formato AAAA.MM.GG HH:MM).

Da eseguire SUL COMPUTER dove e' installato e collegato il terminale MT5
(pip install MetaTrader5), non su un server remoto.

Uso:
    python scarica_dati.py EURUSD 2019 2024

Trappole gestite:
 - il terminale MT5 restituisce gli orari in ora SERVER (di solito EET/EEST,
   cioe' UTC+2 in inverno e UTC+3 in estate, con il cambio l'ultima domenica
   di marzo e l'ultima di ottobre): li convertiamo in UTC prima di salvare.
 - il terminale puo' smettere di rispondere se si chiede troppa storia sulla
   stessa connessione: scarichiamo UN ANNO alla volta e ci ricolleghiamo
   (shutdown() + initialize()) a ogni anno.
"""

import sys
import csv
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5


def _ultima_domenica(anno, mese):
    d = datetime(anno, mese, 1) + timedelta(days=31)
    d = d.replace(day=1) - timedelta(days=1)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


def offset_eet_utc(dt_naive):
    """Ritorna l'offset (in ore) del broker rispetto a UTC per la data data,
    assumendo il classico EET/EEST (UTC+2 / UTC+3, cambio ultima domenica di
    marzo/ottobre alle 01:00 UTC). Se il tuo broker usa un altro fuso,
    modifica questa funzione."""
    anno = dt_naive.year
    inizio_dst = _ultima_domenica(anno, 3).replace(hour=1)
    fine_dst = _ultima_domenica(anno, 10).replace(hour=1)
    if inizio_dst <= dt_naive < fine_dst:
        return 3
    return 2


def server_to_utc(dt_naive):
    offset = offset_eet_utc(dt_naive)
    return dt_naive - timedelta(hours=offset)


def scarica_anno(simbolo, anno, path_terminale=None):
    ok = mt5.initialize(path=path_terminale) if path_terminale else mt5.initialize()
    if not ok:
        raise RuntimeError("initialize() fallito: " + str(mt5.last_error()))
    try:
        inizio = datetime(anno, 1, 1)
        fine = datetime(anno + 1, 1, 1)
        rates = mt5.copy_rates_range(simbolo, mt5.TIMEFRAME_M1, inizio, fine)
    finally:
        mt5.shutdown()

    if rates is None or len(rates) == 0:
        return []

    righe = []
    for r in rates:
        t_server = datetime.utcfromtimestamp(int(r["time"]))
        t_utc = server_to_utc(t_server)
        righe.append((
            t_utc,
            float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
            int(r["tick_volume"]), int(r["spread"]),
        ))
    return righe


def scarica(simbolo, anno_inizio, anno_fine, cartella_dati="DATI", path_terminale=None):
    tutte = []
    for anno in range(anno_inizio, anno_fine + 1):
        print("Scarico {} {} ...".format(simbolo, anno))
        righe = scarica_anno(simbolo, anno, path_terminale)
        print("  {} candele".format(len(righe)))
        tutte.extend(righe)

    tutte.sort(key=lambda r: r[0])

    percorso = "{}/{}.csv".format(cartella_dati.rstrip("/"), simbolo)
    with open(percorso, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume", "spread"])
        for (t, o, h, l, c, v, sp) in tutte:
            w.writerow([t.strftime("%Y.%m.%d %H:%M"), o, h, l, c, v, sp])

    print("Salvato: {} ({} righe totali)".format(percorso, len(tutte)))
    return percorso


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Uso: python scarica_dati.py SIMBOLO ANNO_INIZIO ANNO_FINE [percorso_terminale]")
        sys.exit(1)
    simbolo = sys.argv[1]
    anno_inizio = int(sys.argv[2])
    anno_fine = int(sys.argv[3])
    path_terminale = sys.argv[4] if len(sys.argv) > 4 else None
    scarica(simbolo, anno_inizio, anno_fine, path_terminale=path_terminale)
