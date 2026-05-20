
import MetaTrader5 as mt5
import threading
import time
from datetime import datetime, timedelta, timezone

# ------------------------------------------------
#  MT5 UTILITIES — ARBEXA v4.8.5 (stable)
# ------------------------------------------------
# 
# DESIGN: ogni chiamata fa init+shutdown INDIPENDENTE.
# Niente lock globali tra thread, niente sessione "calda".
# - Più lento (~50ms in più per chiamata)
# - MOLTO più stabile: niente blocchi se MT5 PROP è lento
# - 2 thread possono chiamare MT5 in parallelo senza interferire
#   (MT5 Python supporta connessioni concorrenti se i path sono diversi)
# 
# Eccezione: usiamo un mini-lock SOLO per la sezione init/shutdown,
# perché quel passaggio sui path diversi può confondere MT5 se chiamato
# da 2 thread esattamente nello stesso istante. Il lock dura millisecondi.

_init_lock = threading.Lock()

def _safe_init(path):
    """Init thread-safe. Ritorna True se l'init è riuscito."""
    if not path:
        return False
    try:
        with _init_lock:
            return bool(mt5.initialize(path=path))
    except:
        return False


def _safe_shutdown():
    """Shutdown — senza lock: mt5.shutdown() può bloccarsi e non deve
    tenere il lock mentre aspetta, altrimenti blocca tutti gli altri thread."""
    try:
        mt5.shutdown()
    except:
        pass


def trova_simbolo(simbolo_input):
    """Trova il nome corretto di un simbolo nel terminale corrente."""
    tutti = mt5.symbols_get()
    if not tutti:
        return simbolo_input
    for s in tutti:
        if simbolo_input.upper() in s.name.upper():
            return s.name
    return simbolo_input


def calcola_lotto(path, simbolo, soldi, dp):
    """Calcola il lotto necessario per perdere/guadagnare 'soldi' su 'dp' punti."""
    if not _safe_init(path):
        return 0.01, "no_init"
    try:
        s = trova_simbolo(simbolo)
        mt5.symbol_select(s, True)
        info = mt5.symbol_info(s)
        tick = mt5.symbol_info_tick(s)
        if not info or not tick:
            return 0.01, "no_info"
        prezzo = tick.ask
        pt     = info.point
        profit_1pt = mt5.order_calc_profit(
            mt5.ORDER_TYPE_BUY, s, 1.0, prezzo, prezzo + pt
        )
        if profit_1pt is None or profit_1pt <= 0:
            tv = info.trade_tick_value * (info.point / info.trade_tick_size)
            profit_1pt = tv if tv > 0 else 1.0
        lotto_raw = soldi / (dp * profit_1pt) if (dp > 0 and profit_1pt > 0) else 0.01
        v_min  = float(info.volume_min  or 0.01)
        v_max  = float(info.volume_max  or 100.0)
        v_step = float(info.volume_step or 0.01)
        lotto = round(lotto_raw / v_step) * v_step
        lotto = max(v_min, min(lotto, v_max))
        from math import log10
        decimals = max(0, -int(round(log10(v_step)))) if v_step > 0 else 2
        lotto = round(lotto, decimals)
        debug = "1pt={:.4f} step={} min={} lotto={}".format(profit_1pt, v_step, v_min, lotto)
        return lotto, debug
    except Exception as e:
        return 0.01, str(e)
    finally:
        _safe_shutdown()


def get_account_info(path):
    """Ritorna saldo, equity, P&L, posizioni del conto."""
    if not _safe_init(path):
        return None
    try:
        acc = mt5.account_info()
        if not acc:
            return None
        positions = mt5.positions_get() or []
        total_volume = round(sum(float(p.volume or 0) for p in positions), 2)
        return {
            "balance":      round(acc.balance, 2),
            "equity":       round(acc.equity, 2),
            "pnl":          round(acc.equity - acc.balance, 2),
            "positions":    len(positions),
            "total_volume": total_volume,
            "account":      str(acc.login),
            "broker":       acc.company,
            "server":       acc.server,
        }
    except:
        return None
    finally:
        _safe_shutdown()


def get_spread(path, simbolo):
    """Ritorna lo spread corrente in punti per il simbolo dato. None se non disponibile."""
    if not _safe_init(path):
        return None
    try:
        s = trova_simbolo(simbolo)
        mt5.symbol_select(s, True)
        info = mt5.symbol_info(s)
        if not info:
            return None
        return int(info.spread)
    except:
        return None
    finally:
        _safe_shutdown()


def invia_ordine(path, simbolo, tipo, volume, punti, is_prop=True, sl_ratio=1, tp_ratio=3):
    """Invia un ordine BUY o SELL su MT5 con SL e TP calcolati.
    Lo spread viene AGGIUNTO al TP per garantire il target effettivo."""
    if not _safe_init(path):
        return False, "Connessione fallita"
    try:
        s = trova_simbolo(simbolo)
        mt5.symbol_select(s, True)
        tick = mt5.symbol_info_tick(s)
        info = mt5.symbol_info(s)
        if not tick or not info:
            return False, "Simbolo non trovato: " + simbolo
        prezzo  = tick.ask if tipo == mt5.ORDER_TYPE_BUY else tick.bid
        pt      = info.point
        spread  = info.spread
        sl_dist = punti * sl_ratio * pt
        tp_dist = (punti * tp_ratio + spread) * pt
        sl = (prezzo - sl_dist) if tipo == mt5.ORDER_TYPE_BUY else (prezzo + sl_dist)
        tp = (prezzo + tp_dist) if tipo == mt5.ORDER_TYPE_BUY else (prezzo - tp_dist)
        last_error = "Fill error"
        for fill in [mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC]:
            req = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": s,
                "volume": float(volume), "type": tipo, "price": prezzo,
                "sl": sl, "tp": tp, "magic": 0, "comment": "",
                "deviation": 20, "type_filling": fill
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                return True, "OK @ " + str(round(prezzo, 2))
            if res:
                last_error = "retcode={} {}".format(res.retcode, res.comment)
        return False, last_error
    except Exception as e:
        return False, str(e)
    finally:
        _safe_shutdown()


def chiudi_posizioni(path):
    """Chiude TUTTE le posizioni aperte sul conto specificato."""
    if not _safe_init(path):
        return 0
    chiuse = 0
    try:
        posizioni = mt5.positions_get()
        if posizioni:
            for p in posizioni:
                tipo_c = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                tick   = mt5.symbol_info_tick(p.symbol)
                prezzo = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask
                for fill in [mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC]:
                    req = {
                        "action": mt5.TRADE_ACTION_DEAL, "position": p.ticket,
                        "symbol": p.symbol, "volume": p.volume, "type": tipo_c,
                        "price": prezzo, "magic": 0, "comment": "",
                        "deviation": 20, "type_filling": fill
                    }
                    r = mt5.order_send(req)
                    if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                        chiuse += 1
                        break
    except: pass
    finally:
        _safe_shutdown()
    return chiuse


def get_closed_trades(path, from_timestamp):
    """Restituisce le deal chiuse del terminal a partire da un Unix timestamp."""
    if not _safe_init(path):
        return []
    trades = []
    try:
        try:
            from_dt_select = datetime.utcfromtimestamp(from_timestamp) - timedelta(days=2)
            mt5.history_select(from_dt_select, datetime.utcnow() + timedelta(hours=1))
        except: pass
        from_dt = datetime.utcfromtimestamp(from_timestamp).replace(tzinfo=timezone.utc)
        local_tz = datetime.now().astimezone().tzinfo
        from_dt_local = from_dt.astimezone(local_tz).replace(tzinfo=None)
        from_dt_safe = from_dt_local - timedelta(days=1)
        to_dt_safe = datetime.now() + timedelta(days=1)
        deals = mt5.history_deals_get(from_dt_safe, to_dt_safe)
        try:
            import os as _os
            log_dir = _os.path.expandvars(r"%APPDATA%\ARBEXA\data\logs")
            _os.makedirs(log_dir, exist_ok=True)
            with open(_os.path.join(log_dir, "closed_trades_diag.txt"), "a", encoding="utf-8") as _lf:
                _lf.write("\n[{}] get_closed_trades from_ts={} deals={}\n".format(
                    datetime.now().isoformat(), from_timestamp, len(deals) if deals else 0))
        except: pass
        if deals:
            open_times = {}
            for d in deals:
                if d.time < from_timestamp:
                    continue
                if d.entry == mt5.DEAL_ENTRY_IN:
                    open_times[d.position_id] = datetime.fromtimestamp(d.time).strftime("%d/%m %H:%M")
            for d in deals:
                if d.time < from_timestamp:
                    continue
                if d.entry == mt5.DEAL_ENTRY_OUT:
                    open_time  = open_times.get(d.position_id, "—")
                    close_time = datetime.fromtimestamp(d.time)
                    trades.append({
                        "time":        close_time.strftime("%d/%m %H:%M"),
                        "open_time":   open_time,
                        "date":        close_time.strftime("%d/%m/%Y"),
                        "symbol":      d.symbol,
                        "type":        "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
                        "profit":      round(d.profit, 2),
                        "volume":      round(d.volume, 2),
                        "position_id": d.position_id,
                    })
    except: pass
    finally:
        _safe_shutdown()
    return trades


def get_daily_pnl(path):
    """Ritorna il P&L del giorno corrente. Reset a 22:00 UTC."""
    if not _safe_init(path):
        return None
    try:
        utc_now = datetime.utcnow()
        if utc_now.hour >= 22:
            day_start_utc = utc_now.replace(hour=22, minute=0, second=0, microsecond=0)
        else:
            day_start_utc = (utc_now - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        
        # Converte l'UTC in local time naive per MT5
        local_tz = datetime.now().astimezone().tzinfo
        day_start_local = day_start_utc.replace(tzinfo=timezone.utc).astimezone(local_tz).replace(tzinfo=None)
        to_dt_safe = datetime.now() + timedelta(days=1)
        
        closed_pnl = 0.0
        deals = mt5.history_deals_get(day_start_local, to_dt_safe)
        if deals:
            for d in deals:
                if d.entry == mt5.DEAL_ENTRY_OUT and d.type in (
                        mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                    closed_pnl += float(d.profit or 0) + float(d.swap or 0) + float(d.commission or 0)
        floating_pnl = 0.0
        positions = mt5.positions_get() or []
        for p in positions:
            floating_pnl += float(p.profit or 0) + float(p.swap or 0)
        return round(closed_pnl + floating_pnl, 2)
    except:
        return None
    finally:
        _safe_shutdown()


def get_daily_closed_pnl(path):
    """Ritorna SOLO il P&L delle operazioni CHIUSE oggi (da 22:00 UTC).
    Usato per ricostruire il balance di inizio giornata anche se l'app
    viene avviata a metà giornata."""
    if not _safe_init(path):
        return None
    try:
        utc_now = datetime.utcnow()
        if utc_now.hour >= 22:
            day_start_utc = utc_now.replace(hour=22, minute=0, second=0, microsecond=0)
        else:
            day_start_utc = (utc_now - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        
        # Converte l'UTC in local time naive per MT5
        local_tz = datetime.now().astimezone().tzinfo
        day_start_local = day_start_utc.replace(tzinfo=timezone.utc).astimezone(local_tz).replace(tzinfo=None)
        to_dt_safe = datetime.now() + timedelta(days=1)
        
        closed_pnl = 0.0
        # Garantisco di avere la history disponibile
        try:
            mt5.history_select(day_start_local - timedelta(days=1), to_dt_safe)
        except: pass
        deals = mt5.history_deals_get(day_start_local, to_dt_safe)
        if deals:
            for d in deals:
                if d.entry == mt5.DEAL_ENTRY_OUT and d.type in (
                        mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                    closed_pnl += float(d.profit or 0) + float(d.swap or 0) + float(d.commission or 0)
        return round(closed_pnl, 2)
    except:
        return None
    finally:
        _safe_shutdown()
