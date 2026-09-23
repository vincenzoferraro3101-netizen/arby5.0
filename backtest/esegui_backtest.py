"""
Esegue un backtest completo: carica i dati, applica una strategia, calcola
le statistiche, salva l'elenco delle operazioni e stampa il giudizio.

Uso:
    python esegui_backtest.py --csv DATI/EURUSD.csv --strategia strategie.esempio_breakout \
        --punto 0.00001 --pip_size 0.0001 --tp 20 --sl 10

Se non indichi --spread_extra, il programma te lo chiede a schermo.
"""

import argparse
import importlib
import sys

import motore


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True, help="percorso del CSV in DATI/")
    p.add_argument("--strategia", required=True, help="modulo Python, es. strategie.esempio_breakout")
    p.add_argument("--punto", type=float, required=True, help="dimensione di un punto in prezzo (per convertire lo spread)")
    p.add_argument("--pip_size", type=float, required=True, help="dimensione di un pip in prezzo (per TP/SL)")
    p.add_argument("--tp", type=float, required=True, help="take profit in pips")
    p.add_argument("--sl", type=float, required=True, help="stop loss in pips")
    p.add_argument("--spread_extra", type=float, default=None, help="pips di spread aggiuntivi oltre a quello storico dei dati (0 = nessuno)")
    p.add_argument("--parametri", default="", help="parametri della strategia, es. finestra=20,validita_minuti=15")
    p.add_argument("--out", default="risultati/operazioni.csv", help="dove salvare il CSV delle operazioni")
    args = p.parse_args()

    if args.spread_extra is None:
        risposta = input("Quanti pips di spread AGGIUNTIVI vuoi considerare oltre a quello storico dei dati? [0]: ").strip()
        args.spread_extra = float(risposta) if risposta else 0.0

    parametri = {}
    if args.parametri:
        for coppia in args.parametri.split(","):
            k, v = coppia.split("=")
            try:
                v = float(v) if "." in v else int(v)
            except ValueError:
                pass
            parametri[k.strip()] = v

    print("Carico dati da {} ...".format(args.csv))
    A, timestamps = motore.carica_csv(args.csv, args.punto)
    n_giorni_dati = A[-1, motore.GIORNO] - A[0, motore.GIORNO] + 1
    anni_coperti = (timestamps[-1] - timestamps[0]).days / 365.25
    print("Dati: da {} a {}  ({} righe, {:.1f} anni coperti)".format(
        timestamps[0], timestamps[-1], len(timestamps), anni_coperti))

    modulo = importlib.import_module(args.strategia)
    segnali = modulo.segnali(A, **parametri)
    print("Segnali generati dalla strategia: {}".format(len(segnali)))

    operazioni = motore.esegui_backtest(
        A, timestamps, segnali,
        tp_pips=args.tp, sl_pips=args.sl,
        pip_size=args.pip_size, spread_extra_pips=args.spread_extra,
    )
    print("Operazioni eseguite: {}".format(len(operazioni)))

    motore.salva_operazioni_csv(operazioni, args.out)
    print("Elenco operazioni salvato in: {}".format(args.out))

    statistiche = motore.calcola_statistiche(operazioni, args.tp, args.sl)
    g = statistiche["globale"]
    print()
    print("=== RISULTATO GLOBALE ===")
    print("Operazioni:       {}".format(g["n_operazioni"]))
    print("Win rate:         {:.1%}".format(g["win_rate"]))
    print("Win rate pareggio:{:.1%}".format(statistiche["winrate_pareggio"]))
    print("Pips netti:       {:.1f}".format(g["pips_netti"]))
    print("Drawdown massimo: {:.1f} pips".format(g["drawdown_max"]))
    print()
    print("=== ANNO PER ANNO ===")
    for anno, s in sorted(statistiche["per_anno"].items()):
        print("{}: op={:4d}  win_rate={:.1%}  pips_netti={:8.1f}  dd_max={:7.1f}".format(
            anno, s["n_operazioni"], s["win_rate"], s["pips_netti"], s["drawdown_max"]))
    print()
    print("=== GIUDIZIO ===")
    for riga in motore.giudica(statistiche, anni_coperti):
        print(riga)


if __name__ == "__main__":
    main()
