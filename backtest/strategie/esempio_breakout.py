"""
Strategia di esempio: breakout del massimo/minimo delle ultime N barre.
Serve come modello per scrivere nuove strategie e per testare il motore.

Ogni file di strategia deve avere UNA funzione segnali(A, **parametri) che
ritorna una lista di tuple (lungo, livello, a, b):
  lungo   = True per comprare, False per vendere
  livello = prezzo d'ingresso; float('inf') per il long e -float('inf') per
            lo short significano "a mercato"
  a, b    = intervallo di minuti (indici dell'array A) in cui l'ingresso e'
            ammesso
"""

from motore import HIGH, LOW


def segnali(A, finestra=20, validita_minuti=15):
    """Se il prezzo chiude oltre il massimo (o il minimo) delle ultime
    `finestra` barre, prova a entrare a mercato sulla barra successiva,
    valido per `validita_minuti` minuti."""
    n = A.shape[0]
    out = []
    for i in range(finestra, n - 1):
        massimo = A[i - finestra:i, HIGH].max()
        minimo = A[i - finestra:i, LOW].min()
        chiusura = A[i, 5]  # CLOSE
        a = i + 1
        b = min(i + validita_minuti, n - 1)
        if chiusura > massimo:
            out.append((True, float("inf"), a, b))
        elif chiusura < minimo:
            out.append((False, float("-inf"), a, b))
    return out
