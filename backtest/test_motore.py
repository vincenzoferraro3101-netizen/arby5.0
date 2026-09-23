"""Test di sanita' del motore, con dati sintetici (nessun dato di mercato
richiesto). Esegui con: python test_motore.py"""

import csv
import os
import tempfile

import numpy as np

import motore


def _scrivi_csv_sintetico(percorso):
    righe = [
        ("2024.01.01 00:00", 1.1000, 1.1005, 1.0995, 1.1000, 100, 10),
        ("2024.01.01 00:01", 1.1000, 1.1010, 1.1000, 1.1010, 100, 10),
        ("2024.01.01 00:02", 1.1010, 1.1030, 1.1010, 1.1030, 100, 10),
        ("2024.01.01 00:03", 1.1030, 1.1032, 1.0970, 1.0980, 100, 10),
        ("2024.01.01 00:04", 1.0980, 1.0990, 1.0975, 1.0985, 100, 10),
    ]
    with open(percorso, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "open", "high", "low", "close", "volume", "spread"])
        for r in righe:
            w.writerow(r)


def test_carica_csv():
    with tempfile.TemporaryDirectory() as d:
        percorso = os.path.join(d, "TEST.csv")
        _scrivi_csv_sintetico(percorso)
        A, ts = motore.carica_csv(percorso, punto=0.00001)
        assert A.shape == (5, 7)
        assert A[0, motore.GIORNO] == 0
        assert A[0, motore.MINUTO] == 0
        assert A[1, motore.MINUTO] == 1
        assert abs(A[0, motore.SPREAD] - 10 * 0.00001) < 1e-9
        print("test_carica_csv OK")


def test_long_tp():
    with tempfile.TemporaryDirectory() as d:
        percorso = os.path.join(d, "TEST.csv")
        _scrivi_csv_sintetico(percorso)
        A, ts = motore.carica_csv(percorso, punto=0.00001)
        # long a mercato sulla barra 0, TP 20 pips, SL 50 pips
        # entry ask = open(0) + spread(0) = 1.1000 + 0.0001 = 1.1001
        # TP a 1.1001 + 0.0020 = 1.1021 -> colpito alla barra 2 (high 1.1030, bid)
        segnali = [(True, float("inf"), 0, 4)]
        ops = motore.esegui_backtest(A, ts, segnali, tp_pips=20, sl_pips=50, pip_size=0.0001)
        assert len(ops) == 1
        assert ops[0]["esito"] == "TP"
        assert ops[0]["direzione"] == "LONG"
        print("test_long_tp OK:", ops[0])


def test_long_sl():
    with tempfile.TemporaryDirectory() as d:
        percorso = os.path.join(d, "TEST.csv")
        _scrivi_csv_sintetico(percorso)
        A, ts = motore.carica_csv(percorso, punto=0.00001)
        # SL stretto: entry ask ~1.1001, SL a 5 pips sotto = 1.0951 -> mai toccato
        # invece con SL 5 pips sopra soglia realistica testiamo TP piccolo
        segnali = [(True, float("inf"), 0, 4)]
        ops = motore.esegui_backtest(A, ts, segnali, tp_pips=1000, sl_pips=5, pip_size=0.0001)
        assert len(ops) == 1
        # bid low scende a 1.0970 (barra 3): entry 1.1001, SL a 1.1001-0.0005=1.0996
        assert ops[0]["esito"] == "SL"
        print("test_long_sl OK:", ops[0])


def test_short_tp():
    with tempfile.TemporaryDirectory() as d:
        percorso = os.path.join(d, "TEST.csv")
        _scrivi_csv_sintetico(percorso)
        A, ts = motore.carica_csv(percorso, punto=0.00001)
        # short a mercato sulla barra 2 (bid open 1.1010), TP piccolo
        segnali = [(False, float("-inf"), 2, 4)]
        ops = motore.esegui_backtest(A, ts, segnali, tp_pips=15, sl_pips=100, pip_size=0.0001)
        assert len(ops) == 1
        assert ops[0]["direzione"] == "SHORT"
        assert ops[0]["esito"] == "TP"
        print("test_short_tp OK:", ops[0])


def test_statistiche_e_drawdown():
    ops = [
        {"pips": 10, "uscita_ts": 0, "anno": 2024},
        {"pips": -5, "uscita_ts": 1, "anno": 2024},
        {"pips": -8, "uscita_ts": 2, "anno": 2024},
        {"pips": 20, "uscita_ts": 3, "anno": 2025},
    ]
    stat = motore.calcola_statistiche(ops, tp_pips=20, sl_pips=10)
    assert stat["globale"]["n_operazioni"] == 4
    assert abs(stat["globale"]["pips_netti"] - 17) < 1e-9
    # picco a 10 dopo op1, poi scende a 10-5-8=-3 -> drawdown 13
    assert abs(stat["globale"]["drawdown_max"] - 13) < 1e-9
    assert 2024 in stat["per_anno"] and 2025 in stat["per_anno"]
    print("test_statistiche_e_drawdown OK")


if __name__ == "__main__":
    test_carica_csv()
    test_long_tp()
    test_long_sl()
    test_short_tp()
    test_statistiche_e_drawdown()
    print("Tutti i test passati.")
