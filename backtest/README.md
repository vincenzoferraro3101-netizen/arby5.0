# Backtest motore (1 minuto, numpy)

Motore di backtest scritto a mano (nessuna libreria di backtest pronta),
con regole precise su spread e prezzi. Vedi `motore.py` per i dettagli.

## Regole applicate

- I dati sono candele da 1 minuto, prezzi **BID**.
- Chi compra entra ed esce all'**ASK = bid + spread** del minuto.
- Chi vende lavora sempre sul **BID**.
- Lo spread si paga sempre, sia in entrata che in uscita.
- TP e SL sono fissi in pips per tutto il test.
- Se nella stessa barra vengono toccati TP e SL, vince lo SL (ipotesi
  peggiore, per non gonfiare i risultati).

## 1. Scaricare i dati (sul tuo PC, con MT5 installato e collegato)

```
pip install MetaTrader5
cd backtest
python scarica_dati.py EURUSD 2019 2024
```

Salva `DATI/EURUSD.csv` con gli orari gia' convertiti da ora server a UTC.
**Verifica sempre** un campione di orari/prezzi con una seconda fonte
gratuita (es. Dukascopy): se coincidono a meno di mezzo pip, la conversione
e' corretta. Se il tuo broker non usa il classico EET/EEST, modifica la
funzione `offset_eet_utc` in `scarica_dati.py`.

## 2. Scrivere/scegliere una strategia

Vedi `strategie/esempio_breakout.py` come modello. Ogni strategia e' un
file con una funzione `segnali(A, **parametri)`.

## 3. Lanciare il backtest

```
cd backtest
python esegui_backtest.py \
  --csv DATI/EURUSD.csv \
  --strategia strategie.esempio_breakout \
  --punto 0.00001 \
  --pip_size 0.0001 \
  --tp 20 --sl 10 \
  --parametri finestra=20,validita_minuti=15
```

Il programma chiede quanti pips di spread AGGIUNTIVI vuoi considerare oltre
a quello storico gia' nei dati (0 = usa solo quello storico).

`--punto` e `--pip_size` dipendono dal simbolo e dal broker (quante cifre
decimali usa). Valori tipici:

| Simbolo tipo        | punto      | pip_size  |
|---------------------|------------|-----------|
| EURUSD (5 cifre)    | 0.00001    | 0.0001    |
| USDJPY (3 cifre)    | 0.001      | 0.01      |
| XAUUSD (2 cifre)    | 0.01       | 0.01      |

Controlla i decimali reali del tuo simbolo prima di lanciare il test.

## 4. Risultati

- A schermo: numero operazioni, win rate, win rate di pareggio
  (SL/(SL+TP)), pips netti, drawdown massimo, stessi numeri anno per anno,
  e un giudizio finale.
- `risultati/operazioni.csv`: elenco completo di ogni operazione
  (ingresso, uscita, direzione, prezzo, esito, pips) per controllo a
  campione.

## 5. Test del motore (dati finti, per verificare che il codice sia corretto)

```
cd backtest
python test_motore.py
```

## Attenzione

- Il risultato e' affidabile solo con almeno 2 anni di dati, meglio 5+.
- Un win rate sopra il 90% con TP piu' grande dello SL e' quasi sempre un
  errore del test, non una strategia buona.
- Se provi tante combinazioni di parametri, qualcuna uscira' buona per
  puro caso: non fermarti alla prima che "funziona".
