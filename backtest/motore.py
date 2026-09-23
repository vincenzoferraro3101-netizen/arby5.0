"""
Motore di backtest su candele da 1 minuto. Solo numpy, nessuna libreria
di backtest pronta. Regole applicate (vedi anche README.md):

 - I prezzi nel CSV sono BID.
 - Chi compra (long) entra ed esce all'ASK = bid + spread del minuto.
 - Chi vende (short) entra ed esce al BID.
 - Lo spread e' quindi sempre pagato, sia in entrata che in uscita.
 - Un'operazione per segnale, con TP e SL fissi in pips per l'intero test.
 - Se in una stessa barra vengono toccati sia TP che SL, vince lo SL
   (ipotesi peggiore possibile, per non gonfiare i risultati).
"""

import csv
from datetime import datetime

import numpy as np

# Colonne dell'array numpy prodotto da carica_csv
GIORNO, MINUTO, OPEN, HIGH, LOW, CLOSE, SPREAD = range(7)

# Esiti di un'operazione
ESITO_TP = 1
ESITO_SL = -1
ESITO_FINE_DATI = 0


def carica_csv(percorso, punto):
    """Legge un CSV time,open,high,low,close,volume,spread (formato
    AAAA.MM.GG HH:MM, ora UTC) e ritorna:
      A          -> array numpy (N,7): giorno, minuto, open, high, low, close, spread(prezzo)
      timestamps -> lista di datetime, uno per riga, stesso ordine di A

    punto = dimensione di un punto/point del simbolo in prezzo
            (es. 0.00001 per EURUSD a 5 cifre, 0.01 per XAUUSD a 2 cifre).
            Serve solo per convertire la colonna spread (in punti interi)
            in prezzo.
    """
    righe = []
    with open(percorso, "r", encoding="utf-8", newline="") as f:
        lettore = csv.reader(f)
        intestazione = next(lettore)
        intestazione = [c.strip().lower() for c in intestazione]
        idx = {nome: i for i, nome in enumerate(intestazione)}
        for r in ["time", "open", "high", "low", "close", "spread"]:
            if r not in idx:
                raise ValueError("Colonna mancante nel CSV: " + r)
        for riga in lettore:
            if not riga:
                continue
            t = datetime.strptime(riga[idx["time"]].strip(), "%Y.%m.%d %H:%M")
            righe.append((
                t,
                float(riga[idx["open"]]),
                float(riga[idx["high"]]),
                float(riga[idx["low"]]),
                float(riga[idx["close"]]),
                int(riga[idx["spread"]]),
            ))

    if not righe:
        raise ValueError("CSV vuoto: " + percorso)

    righe.sort(key=lambda r: r[0])

    n = len(righe)
    A = np.zeros((n, 7), dtype=np.float64)
    timestamps = []

    giorno_corrente = -1
    data_corrente = None
    for i, (t, o, h, l, c, sp) in enumerate(righe):
        if t.date() != data_corrente:
            data_corrente = t.date()
            giorno_corrente += 1
        A[i, GIORNO] = giorno_corrente
        A[i, MINUTO] = t.hour * 60 + t.minute
        A[i, OPEN] = o
        A[i, HIGH] = h
        A[i, LOW] = l
        A[i, CLOSE] = c
        A[i, SPREAD] = sp * punto
        timestamps.append(t)

    return A, timestamps


def _trova_ingresso_long(A, livello, a, b):
    """Cerca l'indice della barra in cui un long puo' entrare tra [a, b].
    A mercato (livello = +inf): entra subito sulla barra a.
    A livello: entra quando l'ASK (low+spread) tocca il livello (limite in giu')
               oppure quando l'ASK (high+spread) lo supera gia' in apertura.
    """
    n = A.shape[0]
    b = min(b, n - 1)
    if livello == float("inf"):
        if a > b or a >= n:
            return None, None
        prezzo = A[a, OPEN] + A[a, SPREAD]
        return a, prezzo
    for i in range(a, b + 1):
        ask_low = A[i, LOW] + A[i, SPREAD]
        if ask_low <= livello:
            return i, livello
    return None, None


def _trova_ingresso_short(A, livello, a, b):
    """Come sopra ma per gli short: il livello di un ordine di vendita si
    confronta con il BID (si entra vendendo al bid)."""
    n = A.shape[0]
    b = min(b, n - 1)
    if livello == float("-inf"):
        if a > b or a >= n:
            return None, None
        prezzo = A[a, OPEN]
        return a, prezzo
    for i in range(a, b + 1):
        bid_high = A[i, HIGH]
        if bid_high >= livello:
            return i, livello
    return None, None


def _segui_long(A, i_ingresso, prezzo_ingresso, tp_prezzo, sl_prezzo):
    """Segue un long dalla barra di ingresso in poi. Per chiudere un long si
    vende, quindi l'uscita (sia TP che SL) avviene al BID."""
    n = A.shape[0]
    for i in range(i_ingresso, n):
        bid_high = A[i, HIGH]
        bid_low = A[i, LOW]
        tp_colpito = bid_high >= tp_prezzo
        sl_colpito = bid_low <= sl_prezzo
        if sl_colpito:
            return i, sl_prezzo, ESITO_SL
        if tp_colpito:
            return i, tp_prezzo, ESITO_TP
    return n - 1, A[n - 1, CLOSE], ESITO_FINE_DATI


def _segui_short(A, i_ingresso, prezzo_ingresso, tp_prezzo, sl_prezzo):
    """Segue uno short: per chiudere si compra, quindi l'uscita e' all'ASK
    (bid + spread della barra)."""
    n = A.shape[0]
    for i in range(i_ingresso, n):
        ask_high = A[i, HIGH] + A[i, SPREAD]
        ask_low = A[i, LOW] + A[i, SPREAD]
        sl_colpito = ask_high >= sl_prezzo
        tp_colpito = ask_low <= tp_prezzo
        if sl_colpito:
            return i, sl_prezzo, ESITO_SL
        if tp_colpito:
            return i, tp_prezzo, ESITO_TP
    return n - 1, A[n - 1, CLOSE] + A[n - 1, SPREAD], ESITO_FINE_DATI


def esegui_backtest(A, timestamps, segnali, tp_pips, sl_pips, pip_size, spread_extra_pips=0.0):
    """Esegue una lista di segnali (lungo, livello, a, b) sull'array A.

    tp_pips, sl_pips : take profit / stop loss fissi, in pips, uguali per
                        tutte le operazioni del test.
    pip_size         : quanti "prezzo" vale un pip per questo simbolo
                        (es. 0.0001 EURUSD, 0.01 USDJPY/XAUUSD).
    spread_extra_pips: pip di spread aggiuntivi da sommare, oltre a quello
                        gia' presente nei dati, per simulare condizioni
                        peggiori (0 = usa solo lo spread storico).

    Ritorna la lista delle operazioni eseguite, ognuna un dizionario.
    """
    extra = spread_extra_pips * pip_size
    tp_dist = tp_pips * pip_size
    sl_dist = sl_pips * pip_size
    n = A.shape[0]
    operazioni = []

    for (lungo, livello, a, b) in segnali:
        a = max(0, a)
        b = min(b, n - 1)
        if a > b:
            continue

        if lungo:
            i_ing, prezzo_grezzo = _trova_ingresso_long(A, livello, a, b)
            if i_ing is None:
                continue
            prezzo_ingresso = prezzo_grezzo + extra
            tp_prezzo = prezzo_ingresso + tp_dist
            sl_prezzo = prezzo_ingresso - sl_dist
            i_usc, prezzo_uscita, esito = _segui_long(A, i_ing, prezzo_ingresso, tp_prezzo, sl_prezzo)
            pips = (prezzo_uscita - prezzo_ingresso) / pip_size
        else:
            i_ing, prezzo_grezzo = _trova_ingresso_short(A, livello, a, b)
            if i_ing is None:
                continue
            prezzo_ingresso = prezzo_grezzo - extra
            tp_prezzo = prezzo_ingresso - tp_dist
            sl_prezzo = prezzo_ingresso + sl_dist
            i_usc, prezzo_uscita, esito = _segui_short(A, i_ing, prezzo_ingresso, tp_prezzo, sl_prezzo)
            pips = (prezzo_ingresso - prezzo_uscita) / pip_size

        operazioni.append({
            "ingresso_ts": timestamps[i_ing],
            "uscita_ts": timestamps[i_usc],
            "direzione": "LONG" if lungo else "SHORT",
            "prezzo_ingresso": prezzo_ingresso,
            "prezzo_uscita": prezzo_uscita,
            "esito": {ESITO_TP: "TP", ESITO_SL: "SL", ESITO_FINE_DATI: "FINE_DATI"}[esito],
            "pips": pips,
            "anno": timestamps[i_ing].year,
        })

    return operazioni


def _drawdown_max(pips_sequenza):
    """Drawdown massimo (in pips) sulla curva equity cumulata, in ordine
    di uscita delle operazioni."""
    picco = 0.0
    equity = 0.0
    dd_max = 0.0
    for p in pips_sequenza:
        equity += p
        if equity > picco:
            picco = equity
        dd = picco - equity
        if dd > dd_max:
            dd_max = dd
    return dd_max


def calcola_statistiche(operazioni, tp_pips, sl_pips):
    """Calcola le statistiche richieste su un elenco di operazioni,
    sia complessive sia anno per anno."""
    operazioni = sorted(operazioni, key=lambda o: o["uscita_ts"])
    winrate_pareggio = sl_pips / (sl_pips + tp_pips) if (sl_pips + tp_pips) > 0 else float("nan")

    def blocco(ops):
        n = len(ops)
        if n == 0:
            return {
                "n_operazioni": 0, "win_rate": float("nan"),
                "pips_netti": 0.0, "drawdown_max": 0.0,
            }
        vinte = sum(1 for o in ops if o["pips"] > 0)
        pips_tot = sum(o["pips"] for o in ops)
        dd = _drawdown_max([o["pips"] for o in ops])
        return {
            "n_operazioni": n,
            "win_rate": vinte / n,
            "pips_netti": pips_tot,
            "drawdown_max": dd,
        }

    globale = blocco(operazioni)
    globale["winrate_pareggio"] = winrate_pareggio

    anni = sorted(set(o["anno"] for o in operazioni))
    per_anno = {}
    for anno in anni:
        ops_anno = [o for o in operazioni if o["anno"] == anno]
        per_anno[anno] = blocco(ops_anno)

    return {"globale": globale, "per_anno": per_anno, "winrate_pareggio": winrate_pareggio}


def salva_operazioni_csv(operazioni, percorso):
    """Salva l'elenco completo delle operazioni in un CSV, cosi' si puo'
    controllare a campione."""
    operazioni = sorted(operazioni, key=lambda o: o["ingresso_ts"])
    with open(percorso, "w", encoding="utf-8", newline="") as f:
        scrittore = csv.writer(f)
        scrittore.writerow(["ingresso", "uscita", "direzione", "prezzo_ingresso", "prezzo_uscita", "esito", "pips"])
        for o in operazioni:
            scrittore.writerow([
                o["ingresso_ts"].strftime("%Y.%m.%d %H:%M"),
                o["uscita_ts"].strftime("%Y.%m.%d %H:%M"),
                o["direzione"],
                round(o["prezzo_ingresso"], 6),
                round(o["prezzo_uscita"], 6),
                o["esito"],
                round(o["pips"], 2),
            ])


def giudica(statistiche, anni_coperti):
    """Ritorna una lista di righe testuali con il giudizio sul risultato,
    seguendo le regole: copertura minima, margine sul pareggio, allarme
    win rate troppo alto quando TP > SL."""
    righe = []
    g = statistiche["globale"]
    wp = statistiche["winrate_pareggio"]

    if anni_coperti < 2:
        righe.append("ATTENZIONE: il test copre meno di 2 anni di dati ({:.1f}). Il risultato non e' affidabile.".format(anni_coperti))
    elif anni_coperti < 5:
        righe.append("Il test copre {:.1f} anni: accettabile, ma meglio averne 5 o piu'.".format(anni_coperti))
    else:
        righe.append("Copertura dati: {:.1f} anni. OK.".format(anni_coperti))

    if g["n_operazioni"] == 0:
        righe.append("Nessuna operazione eseguita: nessun giudizio possibile.")
        return righe

    margine = g["win_rate"] - wp
    righe.append("Win rate: {:.1%}  |  win rate di pareggio: {:.1%}  |  margine: {:+.1%}".format(g["win_rate"], wp, margine))
    if margine < 0.02:
        righe.append("Il margine sul pareggio e' inferiore al 2%: la strategia NON supera il controllo.")
    else:
        righe.append("Il margine sul pareggio supera il 2-3%: controllo superato.")

    return righe
