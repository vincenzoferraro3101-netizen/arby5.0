from flask import Flask, request, jsonify, render_template_string, send_from_directory, make_response, session, redirect
from flask_socketio import SocketIO
import json, os, hashlib, base64, time, secrets, html as _html, threading
from datetime import datetime, date, timedelta
from database import load_clients, save_clients, save_client, delete_client as db_delete_client

# ── BASE_DIR DINAMICO ────────────────────────────────────────────────
# Su VPS usa /root/sniper, in locale usa la directory dello script
_VPS_DIR = "/root/sniper"
BASE_DIR = _VPS_DIR if os.path.isdir(_VPS_DIR) else os.path.dirname(os.path.abspath(__file__))

# ── SEGRETI ──────────────────────────────────────────────────────────
# Nessun segreto è scritto nel codice. Vengono letti dalle variabili
# d'ambiente oppure dal file BASE_DIR/.env (formato KEY=VALUE, una per riga).
# Vedi .env.example per l'elenco completo.
def _load_dotenv(path):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass

_load_dotenv(os.path.join(BASE_DIR, ".env"))

def _require_env(name):
    v = os.environ.get(name, "").strip()
    if not v:
        import sys
        sys.stderr.write(
            "ERRORE: variabile d'ambiente {} mancante.\n"
            "Impostala nell'ambiente oppure nel file {}\n"
            "(vedi .env.example).\n".format(name, os.path.join(BASE_DIR, ".env")))
        sys.exit(1)
    return v

app = Flask(__name__)
_secret = os.environ.get("ARBEXA_SECRET", "").strip()
if not _secret:
    _secret = secrets.token_hex(32)
app.secret_key = _secret
socketio = SocketIO(app, cors_allowed_origins="*")

ADMIN_PASSWORD   = _require_env("ARBEXA_ADMIN_PW")
LICENSE_PASSWORD = _require_env("ARBEXA_LICENSE_PW")
GESTIONALE_PW    = _require_env("ARBEXA_GESTIONALE_PW")

DATA_FILE        = os.path.join(BASE_DIR, "clients.json")
EXE_DIR          = os.path.join(BASE_DIR, "downloads")
_DOWNLOAD_COUNT_FILE = os.path.join(BASE_DIR, "download_count.json")

def _load_download_count():
    try:
        with open(_DOWNLOAD_COUNT_FILE) as f:
            return json.load(f).get("count", 0)
    except:
        return 0

def _save_download_count(n):
    try:
        with open(_DOWNLOAD_COUNT_FILE, "w") as f:
            json.dump({"count": n}, f)
    except:
        pass

_download_count = _load_download_count()
DISTRIBUTORS_FILE        = os.path.join(BASE_DIR, "distributors.json")
TRIAL_FINGERPRINTS_FILE  = os.path.join(BASE_DIR, "trial_fingerprints.json")
BROKER_HISTORY_FILE      = os.path.join(BASE_DIR, "broker_history.json")
TELEGRAM_LINKS_FILE      = os.path.join(BASE_DIR, "telegram_links.json")

# ── TELEGRAM BOT ────────────────────────────────────────────────────
# Token del bot: variabile d'ambiente TELEGRAM_BOT_TOKEN (obbligatoria).
TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_API       = "https://api.telegram.org/bot{}/".format(TELEGRAM_BOT_TOKEN)
TRIAL_DAYS               = 2  # giorni di trial gratuito al primo avvio
# Chiave di firma licenze: cambiarla invalida tutte le licenze già emesse.
_LKEY            = _require_env("ARBEXA_LKEY")

def _hash_pw(pw: str) -> str:
    salt = "arbexa_pw_salt_2026"
    return hashlib.sha256((salt + pw.strip().lower()).encode()).hexdigest()

def _check_pw(plain: str, stored: str) -> bool:
    if not plain or not stored:
        return False
    plain = plain.strip().lower()
    stored_s = stored.strip().lower()
    if len(stored_s) == 64 and all(c in "0123456789abcdef" for c in stored_s):
        return _hash_pw(plain) == stored_s
    return plain == stored_s

def _migrate_pw(client_id: str, plain: str):
    if client_id in clients:
        clients[client_id]["client_password"] = _hash_pw(plain)
        save_client(client_id, clients[client_id])

def esc(s) -> str:
    return _html.escape(str(s or ""))

_rate_cache = {}

def _calc_license_days(client_obj):
    expiry = client_obj.get("license_expiry", "")
    if not expiry:
        return client_obj.get("license_days", 0)
    try:
        exp_date = date.fromisoformat(expiry)
        return max((exp_date - date.today()).days, 0)
    except:
        return client_obj.get("license_days", 0)

def _rate_limit(ip, max_calls=5, window=60):
    now = time.time()
    calls = [t for t in _rate_cache.get(ip, []) if now - t < window]
    if len(calls) >= max_calls:
        return False
    calls.append(now)
    _rate_cache[ip] = calls
    return True

os.makedirs(EXE_DIR, exist_ok=True)

CMD_TTL = 30

def _get_valid_command(client_obj):
    cmd = client_obj.get("pending_command")
    if not cmd:
        return None
    ts = client_obj.get("pending_command_ts")
    if ts and (time.time() - ts) > CMD_TTL:
        client_obj["pending_command"]      = None
        client_obj["pending_trader_index"] = None
        client_obj["pending_command_ts"]   = None
        return None
    return cmd

def load_distributors():
    if os.path.exists(DISTRIBUTORS_FILE):
        try:
            with open(DISTRIBUTORS_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def save_distributors():
    with open(DISTRIBUTORS_FILE, "w") as f:
        json.dump(distributors, f, indent=2)

distributors = load_distributors()

# ── TRIAL FINGERPRINTS: tracking dei PC che hanno gia' usato il trial ─
# Struttura: { "fingerprint_hash": {"first_seen": "2026-04-30",
#                                    "expiry": "2026-05-02",
#                                    "client_id": "ABC123"} }
def load_trial_fingerprints():
    if os.path.exists(TRIAL_FINGERPRINTS_FILE):
        try:
            with open(TRIAL_FINGERPRINTS_FILE) as f:
                return json.load(f)
        except: pass
    return {}

def save_trial_fingerprints():
    try:
        with open(TRIAL_FINGERPRINTS_FILE, "w") as f:
            json.dump(trial_fingerprints, f, indent=2)
    except: pass

trial_fingerprints = load_trial_fingerprints()

# ── BROKER HISTORY: archivio cicli quando cambia il costo_prop ──────
# Struttura: { "client_id": [ { "ts":..., "prop_firm":..., "costo_prop":...,
#                               "hedge_pnl_cum":..., "lotti":..., "profitto_netto":... }, ... ] }
def load_broker_history():
    if os.path.exists(BROKER_HISTORY_FILE):
        try:
            with open(BROKER_HISTORY_FILE) as f:
                return json.load(f)
        except: pass
    return {}

def save_broker_history():
    try:
        with open(BROKER_HISTORY_FILE, "w") as f:
            json.dump(broker_history, f, indent=2)
    except: pass

broker_history = load_broker_history()

# ── TELEGRAM LINKS: mapping {client_id|dist_id : {"chat_id": ..., "username": ...}} ──
# Struttura: {
#   "clients":     { "ABC123": {"chat_id": 12345, "username": "mario"} },
#   "distributors":{ "DIST1":  {"chat_id": 67890, "username": "giulia"} },
#   "pending":     { "1234":   {"role":"client","id":"ABC123","ts":...} }   # codici di linking
# }
def load_telegram_links():
    if os.path.exists(TELEGRAM_LINKS_FILE):
        try:
            with open(TELEGRAM_LINKS_FILE) as f:
                data = json.load(f)
                # Sanity defaults
                if "clients" not in data: data["clients"] = {}
                if "distributors" not in data: data["distributors"] = {}
                if "pending" not in data: data["pending"] = {}
                return data
        except: pass
    return {"clients": {}, "distributors": {}, "pending": {}}

def save_telegram_links():
    try:
        with open(TELEGRAM_LINKS_FILE, "w") as f:
            json.dump(telegram_links, f, indent=2)
    except: pass

telegram_links = load_telegram_links()

def send_telegram(chat_id, text, parse_mode="HTML"):
    """Invia un messaggio Telegram a un chat_id specifico. Non blocca se errore."""
    if not chat_id or not TELEGRAM_BOT_TOKEN:
        return False
    try:
        import urllib.request as _ur
        import urllib.parse  as _up
        data = _up.urlencode({
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": parse_mode,
        }).encode()
        req = _ur.Request(TELEGRAM_API + "sendMessage", data=data, method="POST")
        with _ur.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception as e:
        print("send_telegram error:", e)
        return False

def gen_link_code():
    """Genera un codice di linking di 6 cifre univoco."""
    import random
    for _ in range(20):
        code = str(random.randint(100000, 999999))
        if code not in telegram_links.get("pending", {}):
            return code
    return str(random.randint(100000, 999999))

_dirty_clients = set()
_dirty_lock    = threading.Lock()

def mark_dirty(client_id):
    with _dirty_lock:
        _dirty_clients.add(client_id)

def _batch_save_loop():
    while True:
        time.sleep(10)  # Salvataggio batch ogni 10s per dati più freschi
        with _dirty_lock:
            to_save = list(_dirty_clients)
            _dirty_clients.clear()
        for cid in to_save:
            try:
                if cid in clients:
                    save_client(cid, clients[cid])
            except:
                pass

_batch_thread = threading.Thread(target=_batch_save_loop, daemon=True)
_batch_thread.start()

# ── SCHEDULER RESOCONTO GIORNALIERO 22:00 UTC ─────────────────────────
def _daily_recap_loop():
    """Thread daemon che alle 22:00 UTC manda il resoconto a tutti i clienti
    con Telegram collegato, usando i dati dell'ultimo heartbeat nel DB.
    Non richiede che l'app desktop sia aperta."""
    _last_sent_date = None
    while True:
        try:
            from datetime import datetime as _dt
            _now = _dt.utcnow()
            # Controlla se siamo alle 20:00 UTC (finestra di 60 secondi)
            if _now.hour == 20 and _now.minute == 0 and _now.date() != _last_sent_date:
                _last_sent_date = _now.date()
                _date_str = _now.strftime("%d/%m/%Y")
                _sent = 0
                for _cid, _cli in list(clients.items()):
                    try:
                        # Verifica che il cliente abbia Telegram collegato
                        _cli_tg = telegram_links.get("clients", {}).get(_cid)
                        if not _cli_tg or not _cli_tg.get("chat_id"):
                            continue
                        # Leggi dati trader dall'ultimo heartbeat
                        _traders = _cli.get("data", {}).get("traders", [])
                        if not _traders:
                            continue
                        for _t in _traders:
                            _prop_firm  = _t.get("prop_firm", "?")
                            _prop_bal   = float(_t.get("prop_bal", 0) or 0)
                            _hedge_bal  = float(_t.get("hedge_bal", 0) or 0)
                            _prop_pnl   = float(_t.get("prop_pnl_day", 0) or 0)
                            _hedge_pnl  = float(_t.get("hedge_pnl_day", 0) or 0)
                            _net        = _prop_pnl + _hedge_pnl
                            _icon       = "✅" if _net >= 0 else "🔴"
                            _net_sign   = "+" if _net >= 0 else ""

                            _trades_list = []
                            _trades_log = _t.get("trades_log", [])
                            if _trades_log:
                                _trades_list.append("")
                                _trades_list.append("📝 <b>Operazioni chiuse oggi</b>")
                                for tr in _trades_log:
                                    prof = float(tr.get("profit", 0) or 0)
                                    icon = "🟢 TP" if prof >= 0 else "🔴 SL"
                                    _trades_list.append("   {} {:.2f} $".format(icon, prof))

                            _lines = [
                                "{} <b>RESOCONTO GIORNATA</b>".format(_icon),
                                "─────────────",
                                "Scheda: <b>{}</b>".format(_prop_firm),
                                "Data: {}".format(_date_str),
                                "",
                                "📊 <b>P&amp;L giornaliero</b>",
                                "   PROP:  <b>{:+.2f} $</b>".format(_prop_pnl),
                                "   HEDGE: <b>{:+.2f} $</b>".format(_hedge_pnl),
                                "   ─────────",
                                "   <b>Netto: {}{:.2f} $</b>".format(_net_sign, _net),
                                "",
                                "💰 <b>Saldi a fine giornata</b>",
                                "   PROP:  ${:,.2f}".format(_prop_bal),
                                "   HEDGE: ${:,.2f}".format(_hedge_bal),
                            ]
                            _lines.extend(_trades_list)
                            _msg = "\n".join(_lines)
                            # Invia al cliente
                            if send_telegram(_cli_tg["chat_id"], _msg):
                                _sent += 1
                    except Exception as _e:
                        pass
        except Exception:
            pass
        time.sleep(30)  # controlla ogni 30 secondi

_recap_thread = threading.Thread(target=_daily_recap_loop, daemon=True)
_recap_thread.start()

def _xor(s, key):
    return "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(s))

def _enc(s):
    return base64.b64encode(_xor(s, _LKEY).encode("latin1")).decode()

def generate_license(client_id, days):
    expiry = (date.today() + timedelta(days=int(days))).isoformat()
    raw    = "{}|{}".format(client_id, expiry)
    sig    = hashlib.sha256((_LKEY + raw).encode()).hexdigest()[:16]
    return _enc("{}|{}".format(raw, sig))

clients = load_clients()

def notify_admins(text):
    """Invia notifica Telegram a tutti i clienti con is_admin=True."""
    try:
        for cid, cli in clients.items():
            if not cli.get("is_admin"): continue
            tg = telegram_links.get("clients", {}).get(cid)
            if tg and tg.get("chat_id"):
                send_telegram(tg["chat_id"], text)
    except: pass

def _cum_key(trade):
    ticket = str(trade.get("ticket", ""))
    if ticket and ticket != "0":
        return "t:" + ticket
    return "fp:{}|{}|{}|{}".format(
        trade.get("date",""), trade.get("close_time",""),
        trade.get("profit",""), trade.get("conto",""))

def merge_client_cumulative(client_obj, new_trades):
    prop_cum  = client_obj.setdefault("prop_cumulative",  [])
    hedge_cum = client_obj.setdefault("hedge_cumulative", [])
    prop_keys  = {_cum_key(t) for t in prop_cum}
    hedge_keys = {_cum_key(t) for t in hedge_cum}
    for trade in new_trades:
        k = _cum_key(trade)
        if trade.get("conto") == "PROP":
            if k not in prop_keys:
                prop_cum.append(trade)
                prop_keys.add(k)
        else:
            if k not in hedge_keys:
                hedge_cum.append(trade)
                hedge_keys.add(k)

def merge_cumulative(trader_obj, new_trades):
    prop_cum  = trader_obj.setdefault("prop_cumulative",  [])
    hedge_cum = trader_obj.setdefault("hedge_cumulative", [])
    prop_keys  = {_cum_key(t) for t in prop_cum}
    hedge_keys = {_cum_key(t) for t in hedge_cum}
    for trade in new_trades:
        k = _cum_key(trade)
        if trade.get("conto") == "PROP":
            if k not in prop_keys:
                prop_cum.append(trade)
                prop_keys.add(k)
        else:
            if k not in hedge_keys:
                hedge_cum.append(trade)
                hedge_keys.add(k)

_deleted_clients = set()

@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    d   = request.get_json(force=True, silent=True) or {}
    cid = d.get("client_id", "").strip()
    if not cid:
        return jsonify({"ok": False})
    if cid in _deleted_clients:
        return jsonify({"ok": False, "deleted": True, "error": "Cliente eliminato"}), 404
    if cid not in clients:
        clients[cid] = {"client_id": cid, "first_seen": datetime.utcnow().isoformat() + "Z"}
        # Notifica admin — nuovo cliente
        notify_admins("🆕 <b>Nuovo cliente registrato</b>\n\nID: <code>{}</code>\nData: {}".format(
            cid, datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")))
    if clients[cid].get("blocked"):
        return jsonify({"ok": False, "blocked": True, "blocked_reason": "admin_block",
                        "license_days": 0, "license_expiry": "2000-01-01",
                        "command": None, "params": None, "messages": [], "new_messages": 0})
    stored_expiry = clients[cid].get("license_expiry")
    if stored_expiry:
        try:
            exp = datetime.strptime(stored_expiry, "%Y-%m-%d").date()
            server_days = max((exp - date.today()).days, 0)
            clients[cid]["license_days"] = server_days
        except:
            server_days = clients[cid].get("license_days", 0)
    else:
        server_days = clients[cid].get("license_days", d.get("license_days", 0))
    incoming_traders = d.get("traders", [])
    if incoming_traders:
        existing = clients[cid].get("traders", [])
        for i, t_in in enumerate(incoming_traders):
            if i < len(existing):
                # ── RILEVAMENTO CAMBIO costo_prop ──────────────────
                # Se costo_prop cambia (e il valore vecchio era > 0), archivia
                # il ciclo precedente e resetta hedge_pnl_cum.
                # NB: ignora il primo invio (quando old_costo è 0/vuoto: scheda nuova)
                try:
                    new_costo = float(t_in.get("costo_prop", 0) or 0)
                    old_costo = float(existing[i].get("costo_prop", 0) or 0)
                    # Solo se entrambi > 0 e diversi → vero cambio
                    if old_costo > 0 and new_costo > 0 and abs(new_costo - old_costo) > 0.01:
                        # Snapshot prima del merge
                        old_hedge_cum = float(existing[i].get("hedge_pnl_cum", 0) or 0)
                        old_lots      = float(existing[i].get("hedge_lots_cum", 0) or 0)
                        prop_firm     = existing[i].get("prop_firm", "?")
                        # Archivia il ciclo
                        if cid not in broker_history:
                            broker_history[cid] = []
                        broker_history[cid].append({
                            "ts":              datetime.utcnow().isoformat() + "Z",
                            "prop_firm":       prop_firm,
                            "costo_prop":      old_costo,
                            "hedge_pnl_cum":   round(old_hedge_cum, 2),
                            "hedge_lots_cum":  round(old_lots, 2),
                            "profitto_netto":  round(old_hedge_cum - old_costo, 2),
                            "new_costo_prop": new_costo,
                            "trader_index":    i,
                        })
                        save_broker_history()
                        # Reset hedge_pnl_cum lato server (sarà sincronizzato col client)
                        existing[i]["hedge_pnl_cum"] = 0.0
                        # Pending command per app desktop: resetta hedge cumulativo lato client
                        clients[cid]["pending_command"]      = "reset_hedge_pnl_cum"
                        clients[cid]["pending_trader_index"] = i
                        clients[cid]["pending_command_ts"]   = time.time()
                except: pass
                # Merge campi normale (escludendo hedge_pnl_cum se appena resettato)
                _just_archived = (clients[cid].get("pending_command") == "reset_hedge_pnl_cum"
                                  and clients[cid].get("pending_trader_index") == i
                                  and clients[cid].get("pending_command_ts", 0) > time.time() - 30)
                
                # Calcola l'accumulo indipendente per l'admin/distributore
                if not _just_archived:
                    try:
                        old_h_cum  = float(existing[i].get("hedge_pnl_cum", 0) or 0)
                        new_h_cum  = float(t_in.get("hedge_pnl_cum", 0) or 0)
                        old_h_lots = float(existing[i].get("hedge_lots_cum", 0) or 0)
                        new_h_lots = float(t_in.get("hedge_lots_cum", 0) or 0)
                        
                        if "admin_cumulative_hedge" not in existing[i]:
                            existing[i]["admin_cumulative_hedge"] = old_h_cum
                        if "admin_cumulative_lots" not in existing[i]:
                            existing[i]["admin_cumulative_lots"] = old_h_lots
                            
                        if new_h_cum > old_h_cum:
                            delta_h = new_h_cum - old_h_cum
                            existing[i]["admin_cumulative_hedge"] = round(float(existing[i]["admin_cumulative_hedge"] or 0) + delta_h, 2)
                        if new_h_lots > old_h_lots:
                            delta_l = new_h_lots - old_h_lots
                            existing[i]["admin_cumulative_lots"] = round(float(existing[i]["admin_cumulative_lots"] or 0) + delta_l, 2)
                    except:
                        pass

                for key in ["prop_bal","prop_pnl","hedge_bal","hedge_pnl",
                            "hedge_pnl_cum","hedge_lots_cum","positions","prop_firm","size",
                            "fase","dd_giornaliero_pct","dd_totale_pct",
                            "target_pct","simbolo_prop","simbolo_hedge",
                            "prop_account","prop_broker","hedge_account","hedge_broker",
                            "prop_bal_day_start","costo_prop","prop_pnl_day","hedge_pnl_day",
                            # Per preview ordini da web
                            "stop_prop","target_hedge","ratio_prop","ratio_hedge",
                            "lotto_prop","lotto_hedge","auto_ratio","prop_size_real",
                            # Status ultimo messaggio app cliente
                            "last_status","last_status_color","last_status_ts"]:
                    # Skip hedge_pnl_cum se appena archiviato (il client deve prima ricevere il reset)
                    if key == "hedge_pnl_cum" and _just_archived:
                        continue
                    if key in t_in:
                        existing[i][key] = t_in[key]
            else:
                # Per nuove schede, inizializza i valori cumulativi admin/distributor
                t_in["admin_cumulative_hedge"] = float(t_in.get("hedge_pnl_cum", 0) or 0)
                t_in["admin_cumulative_lots"]  = float(t_in.get("hedge_lots_cum", 0) or 0)
                existing.append(t_in)
        clients[cid]["traders"] = existing
    clients[cid]["client_id"]    = cid
    clients[cid]["last_seen"]    = datetime.utcnow().isoformat() + "Z"
    clients[cid]["license_days"] = server_days
    # ── FINGERPRINT CHECK ─────────────────────────────────────────
    incoming_fp = d.get("fingerprint", "").strip()
    if incoming_fp:
        stored_fp = clients[cid].get("fingerprint", "")
        if not stored_fp:
            # Prima volta — registra il fingerprint
            clients[cid]["fingerprint"] = incoming_fp
        elif stored_fp != incoming_fp:
            # Fingerprint diverso — PC diverso, blocca
            return jsonify({"ok": False, "blocked": True, "blocked_reason": "fingerprint",
                            "license_days": 0, "license_expiry": "2000-01-01",
                            "command": None, "params": None, "messages": [], "new_messages": 0})

    # ── TRIAL 2 GIORNI (SOLO PRIMO USO PER FINGERPRINT) ──────────────
    # Se il client non ha license_expiry e il suo fingerprint non ha mai
    # usato il trial: gli concedo 2 giorni e marchio il fingerprint.
    # Se il fingerprint ha gia' usato il trial (anche con un altro client_id):
    #   - se ancora valido: restituisco i giorni residui di quel trial
    #   - se scaduto:       license_days resta 0 (deve attivare a pagamento)
    if not stored_expiry and incoming_fp:
        if incoming_fp in trial_fingerprints:
            # Fingerprint gia' visto: usa l'expiry originale del trial
            tr_data    = trial_fingerprints[incoming_fp]
            tr_expiry  = tr_data.get("expiry", "")
            try:
                tr_exp_d   = datetime.strptime(tr_expiry, "%Y-%m-%d").date()
                trial_days = max((tr_exp_d - date.today()).days, 0)
            except:
                trial_days = 0
            if trial_days > 0:
                clients[cid]["license_expiry"] = tr_expiry
                clients[cid]["license_days"]   = trial_days
                clients[cid]["trial"]          = True
                server_days   = trial_days
                stored_expiry = tr_expiry
        else:
            # Fingerprint nuovo: concedo trial
            trial_exp_d = date.today() + timedelta(days=TRIAL_DAYS)
            trial_exp_s = trial_exp_d.strftime("%Y-%m-%d")
            trial_fingerprints[incoming_fp] = {
                "first_seen": date.today().strftime("%Y-%m-%d"),
                "expiry":     trial_exp_s,
                "client_id":  cid,
            }
            save_trial_fingerprints()
            clients[cid]["license_expiry"] = trial_exp_s
            clients[cid]["license_days"]   = TRIAL_DAYS
            clients[cid]["trial"]          = True
            server_days   = TRIAL_DAYS
            stored_expiry = trial_exp_s

    if d.get("params_received"):
        clients[cid]["params_pending"] = False
    if "pending_command" not in clients[cid]:
        clients[cid]["pending_command"] = None
    mark_dirty(cid)

    # ── MESSAGGI AUTOMATICI ───────────────────────────────────────
    # 1. Primo heartbeat — messaggio di benvenuto
    if not clients[cid].get("welcome_sent"):
        clients[cid]["welcome_sent"] = True
        if _auto_msgs_config.get("welcome", {}).get("enabled", True):
            txt = _auto_msgs_config["welcome"]["text"].replace("{id}", cid)
            _auto_msg(cid, txt)

    # 2. DD giornaliero vicino (rimane meno del 1% della size)
    for t in clients[cid].get("traders", []):
        sz       = float(t.get("size", 0) or 0)
        prop_bal = float(t.get("prop_bal", 0) or 0)
        prop_pnl = float(t.get("prop_pnl", 0) or 0)
        dd_pct   = float(t.get("dd_giornaliero_pct", 5) or 5)
        if sz > 0 and _auto_msgs_config.get("dd_warning", {}).get("enabled", True):
            dd_limit   = sz * dd_pct / 100
            dd_used    = max(sz - (prop_bal + prop_pnl), 0)
            dd_rem     = max(dd_limit - dd_used, 0)
            dd_rem_pct = dd_rem / sz * 100
            dd_key     = "dd_warn_sent_{}".format(date.today().isoformat())
            if dd_rem_pct <= 1.0 and not clients[cid].get(dd_key):
                clients[cid][dd_key] = True
                txt = _auto_msgs_config["dd_warning"]["text"]
                _auto_msg(cid, txt)

    # 3. Licenza in scadenza — 3 giorni prima
    lic_days = clients[cid].get("license_days", 0)
    if 0 < lic_days <= 3 and _auto_msgs_config.get("license", {}).get("enabled", True):
        lic_warn_key = "lic_warn_sent_{}".format(clients[cid].get("license_expiry", ""))
        if not clients[cid].get(lic_warn_key):
            clients[cid][lic_warn_key] = True
            txt = _auto_msgs_config["license"]["text"].replace("{days}", str(lic_days))
            _auto_msg(cid, txt)

    socketio.emit("update", clients)
    cmd    = _get_valid_command(clients[cid])
    tidx   = clients[cid].get("pending_trader_index")
    params = clients[cid].get("server_params", None) if clients[cid].get("params_pending") else None
    if cmd:
        clients[cid]["pending_command"]      = None
        clients[cid]["pending_trader_index"] = None
        save_client(cid, clients[cid])
    messages = clients[cid].get("messages", [])
    new_msgs = [m for m in messages if not m.get("read")]
    all_clients_snap = {k: {
        "license_days":   v.get("license_days", 0),
        "license_expiry": v.get("license_expiry", ""),
        "last_seen":      v.get("last_seen", ""),
    } for k, v in clients.items()}
    return jsonify({"ok": True, "command": cmd, "trader_index": tidx, "params": params,
                    "license_days": clients[cid].get("license_days", 0),
                    "license_expiry": clients[cid].get("license_expiry", ""),
                    "messages": messages[-3:] if messages else [],
                    "new_messages": len(new_msgs),
                    "all_clients": all_clients_snap})

@app.route("/api/ea_update/<client_id>", methods=["POST"])
def ea_update(client_id):
    data = request.get_json(force=True, silent=True) or {}
    if not data:
        try:
            data = json.loads(request.data.decode("utf-8")) or {}
        except:
            data = {}
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    c    = clients[client_id]
    tidx = int(data.get("trader_index", 0))
    while len(c.setdefault("traders", [])) <= tidx:
        c["traders"].append({})
    t = c["traders"][tidx]
    account_type = data.get("account_type", "PROP")
    if account_type == "HEDGE":
        for k in ["hedge_bal", "hedge_pnl", "hedge_positions"]:
            if k in data:
                t[k] = data[k]
    else:
        for k in ["prop_firm", "size", "prop_bal", "prop_pnl",
                  "positions", "trade_count", "dd_giornaliero_pct",
                  "dd_totale_pct", "target_pct", "fase"]:
            if k in data:
                t[k] = data[k]
    if "trades" in data:
        t["ea_trades"] = data["trades"][-30:]
    if account_type == "PROP" and not t.get("archived"):
        size       = float(t.get("size", 0) or 0)
        prop_bal   = float(t.get("prop_bal", 0) or 0)
        target_pct = float(t.get("target_pct", 10) or 10)
        dd_max_pct = float(t.get("dd_totale_pct", 10) or 10)
        costo_prop = float(t.get("costo_prop", 0) or 0)
        target_bal = size * (1 + target_pct / 100)
        dd_bal     = size * (1 - dd_max_pct / 100)
        esito = None
        if size > 0 and prop_bal >= target_bal:
            esito = "PASSATA"
        elif size > 0 and prop_bal <= dd_bal:
            esito = "BRUCIATA"
        if esito:
            prop_cum_list  = c.get("prop_cumulative",  [])
            hedge_cum_list = c.get("hedge_cumulative", [])
            hedge_cum = sum(float(tr.get("profit", 0)) for tr in hedge_cum_list)
            prop_cum  = sum(float(tr.get("profit", 0)) for tr in prop_cum_list)
            guadagno_hedge = round(hedge_cum - costo_prop, 2)
            archived_entry = {
                "esito":          esito,
                "archived_at":    datetime.utcnow().isoformat() + "Z",
                "prop_firm":      t.get("prop_firm", ""),
                "size":           size,
                "fase":           t.get("fase", ""),
                "costo_prop":     costo_prop,
                "prop_bal_final": prop_bal,
                "prop_cum":       round(prop_cum, 2),
                "hedge_bal_final":float(t.get("hedge_bal", 0) or 0),
                "hedge_cum":      round(hedge_cum, 2),
                "guadagno_hedge": guadagno_hedge,
                "target_pct":     target_pct,
                "dd_totale_pct":  dd_max_pct,
                "trades_prop":    prop_cum_list[:],
                "trades_hedge":   hedge_cum_list[:],
                "trader_index":   tidx,
            }
            c.setdefault("traders_archive", []).append(archived_entry)
            c["prop_cumulative"]  = []
            c["hedge_cumulative"] = []
            c["traders"][tidx] = {
                "prop_firm":         t.get("prop_firm", ""),
                "size":              size,
                "costo_prop":        costo_prop,
                "dd_giornaliero_pct":t.get("dd_giornaliero_pct", 5),
                "dd_totale_pct":     dd_max_pct,
                "target_pct":        target_pct,
                "fase":              t.get("fase", ""),
                "ea_trades":         [],
            }
            save_client(client_id, c)
            # Messaggio automatico PASSATA
            if esito == "PASSATA" and _auto_msgs_config.get("target", {}).get("enabled", True):
                txt = _auto_msgs_config["target"]["text"]
                _auto_msg(client_id, txt)
            # Notifica distributore (se assegnato)
            _icon = "🎯" if esito == "PASSATA" else "🔴"
            dist_msg = "{} <b>Scheda {} — Cliente {}</b>\n\nProp firm: {}\nSize: ${:,.0f}\nFase: {}\nBilancio finale PROP: ${:,.2f}".format(
                _icon, esito, client_id,
                archived_entry.get("prop_firm","?"),
                archived_entry.get("size",0),
                archived_entry.get("fase","?"),
                archived_entry.get("prop_bal_final",0)
            )
            for dist_id, dist in distributors.items():
                if client_id in dist.get("clients", []):
                    d_info = telegram_links.get("distributors", {}).get(dist_id)
                    if d_info and d_info.get("chat_id"):
                        send_telegram(d_info["chat_id"], dist_msg)
                    break
            notify_admins(dist_msg)
            socketio.emit("update", clients)
            return jsonify({"ok": True, "command": None, "archived": True, "esito": esito})
    c["last_seen"] = datetime.utcnow().isoformat() + "Z"
    mark_dirty(client_id)
    cmd    = _get_valid_command(c)
    tidx_c = c.get("pending_trader_index")
    if cmd and (tidx_c is None or int(tidx_c) == tidx):
        c["pending_command"]      = None
        c["pending_trader_index"] = None
        save_client(client_id, c)
        return jsonify({"ok": True, "command": cmd})
    return jsonify({"ok": True, "command": None})

@app.route("/api/ea_trades/<client_id>/<int:trader_idx>")
def ea_get_trades(client_id, trader_idx):
    cpw = request.headers.get("X-Client-Password", "").strip().lower()
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    stored = clients[client_id].get("client_password", "").strip().lower()
    if not stored or not _check_pw(cpw, stored):
        return jsonify({"ok": False}), 403
    traders = clients[client_id].get("traders", [])
    trades  = []
    if trader_idx < len(traders):
        trades = traders[trader_idx].get("ea_trades",
                 traders[trader_idx].get("trades_history", []))
    return jsonify({"ok": True, "trades": trades})

@app.route("/api/archive/<client_id>")
def get_archive(client_id):
    admin_pw  = request.headers.get("X-Admin-Password", "")
    client_pw = request.headers.get("X-Client-Password", "").strip().lower()
    dist_id   = request.headers.get("X-Distributor-Id", "")
    dist_pw   = request.headers.get("X-Distributor-Password", "")
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    is_admin  = admin_pw == ADMIN_PASSWORD
    is_client = (client_pw and client_pw == clients[client_id].get("client_password","").strip().lower())
    is_dist   = (dist_id and dist_id in distributors and
                 distributors[dist_id].get("password") == dist_pw and
                 client_id in distributors[dist_id].get("clients", []))
    if not is_admin and not is_client and not is_dist:
        return jsonify({"ok": False}), 403
    archive = clients[client_id].get("traders_archive", [])
    return jsonify({"ok": True, "archive": archive})

@app.route("/api/broker_history/<client_id>")
def get_broker_history(client_id):
    """Storico cicli (snapshot al cambio costo_prop). Visibile ad admin e distributore."""
    admin_pw  = request.headers.get("X-Admin-Password", "")
    dist_id   = request.headers.get("X-Distributor-Id", "")
    dist_pw   = request.headers.get("X-Distributor-Password", "")
    is_admin  = admin_pw == ADMIN_PASSWORD
    is_dist   = (dist_id and dist_id in distributors and
                 distributors[dist_id].get("password") == dist_pw and
                 client_id in distributors[dist_id].get("clients", []))
    if not is_admin and not is_dist:
        return jsonify({"ok": False}), 403
    history = broker_history.get(client_id, [])
    return jsonify({"ok": True, "history": history})

@app.route("/api/broker_history_delete/<client_id>/<int:idx>", methods=["DELETE"])
def delete_broker_history(client_id, idx):
    """Elimina una voce specifica dello storico (solo admin)."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    hist = broker_history.get(client_id, [])
    if 0 <= idx < len(hist):
        hist.pop(idx)
        broker_history[client_id] = hist
        save_broker_history()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Indice non valido"}), 404

@app.route("/api/archive_delete/<client_id>/<int:idx>", methods=["DELETE"])
def delete_archive_entry(client_id, idx):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    archive = clients[client_id].get("traders_archive", [])
    if idx < 0 or idx >= len(archive):
        return jsonify({"ok": False, "error": "Indice non valido"}), 400
    archive.pop(idx)
    clients[client_id]["traders_archive"] = archive
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/archive_delete_all/<client_id>", methods=["DELETE"])
def delete_archive_all(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["traders_archive"] = []
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/delete_trader/<client_id>/<int:idx>", methods=["POST"])
def delete_trader(client_id, idx):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    traders = clients[client_id].get("traders", [])
    if idx < 0 or idx >= len(traders):
        return jsonify({"ok": False, "error": "Indice scheda non valido"}), 400
    traders.pop(idx)
    clients[client_id]["traders"] = traders
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/reset_totals/<client_id>", methods=["POST"])
def reset_totals(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["prop_cumulative"]  = []
    clients[client_id]["hedge_cumulative"] = []
    clients[client_id]["totals_reset_ts"]  = datetime.utcnow().isoformat() + "Z"
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    return jsonify({"ok": True})

@app.route("/api/reset_hedge_lots/<client_id>", methods=["POST"])
def reset_hedge_lots(client_id):
    """Reset cumulativo lotti hedge di un singolo cliente.
    Imposta un pending_command che il client esegue al prossimo polling.
    Azzera anche subito i valori lato server, così l'admin vede effetto immediato.
    """
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    # Azzero subito lato server per feedback immediato sul pannello
    for t in clients[client_id].get("traders", []):
        t["hedge_lots_cum"] = 0.0
    # Imposto comando remoto: il client azzera anche lato locale al prossimo polling
    clients[client_id]["pending_command"]      = "reset_hedge_lots"
    clients[client_id]["pending_trader_index"] = None
    clients[client_id]["pending_command_ts"]   = time.time()
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    return jsonify({"ok": True})

@app.route("/api/reset_admin_hedge/<client_id>/<int:idx>", methods=["POST"])
def reset_admin_hedge(client_id, idx):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    traders = clients[client_id].get("traders", [])
    if idx < 0 or idx >= len(traders):
        return jsonify({"ok": False, "error": "Indice scheda non valido"}), 400
    traders[idx]["admin_cumulative_hedge"] = 0.0
    traders[idx]["admin_cumulative_lots"]  = 0.0
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    return jsonify({"ok": True})

@app.route("/api/dist_reset_admin_hedge/<client_id>/<int:idx>", methods=["POST"])
def dist_reset_admin_hedge(client_id, idx):
    dist_id = request.headers.get("X-Distributor-Id", "").strip()
    dist_pw = request.headers.get("X-Distributor-Password", "").strip()
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if client_id not in distributors[dist_id].get("clients", []):
        return jsonify({"ok": False, "error": "Cliente non assegnato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    traders = clients[client_id].get("traders", [])
    if idx < 0 or idx >= len(traders):
        return jsonify({"ok": False, "error": "Indice scheda non valido"}), 400
    traders[idx]["admin_cumulative_hedge"] = 0.0
    traders[idx]["admin_cumulative_lots"]  = 0.0
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    return jsonify({"ok": True})

@app.route("/api/toggle_telegram_alerts/<client_id>/<target>", methods=["POST"])
def toggle_telegram_alerts(client_id, target):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled", False))
    if target == "admin":
        clients[client_id]["notify_admin_telegram"] = enabled
    elif target == "distributor":
        clients[client_id]["notify_distributor_telegram"] = enabled
    else:
        return jsonify({"ok": False, "error": "Target non valido"}), 400
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    return jsonify({"ok": True})

@app.route("/api/dist_toggle_telegram_alerts/<client_id>", methods=["POST"])
def dist_toggle_telegram_alerts(client_id):
    dist_id = request.headers.get("X-Distributor-Id", "").strip()
    dist_pw = request.headers.get("X-Distributor-Password", "").strip()
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if client_id not in distributors[dist_id].get("clients", []):
        return jsonify({"ok": False, "error": "Cliente non assegnato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled", False))
    clients[client_id]["notify_distributor_telegram"] = enabled
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    return jsonify({"ok": True})

# ── BILLING DISTRIBUTORI ──────────────────────────────────────────
def _calc_distributor_amount(dist):
    """Calcola l'importo dovuto al distributore in base al suo billing_mode.
    Ritorna dict con: amount, fixed, per_lot, total_lots, mode, paid (bool)."""
    mode  = dist.get("billing_mode", "fixed")
    fixed = float(dist.get("fixed_amount", 0) or 0)
    rate  = float(dist.get("per_lot_rate", 0) or 0)
    # Somma lotti cumulativi di TUTTI i clienti del distributore (usando admin_cumulative_lots)
    total_lots = 0.0
    for cid in dist.get("clients", []):
        if cid not in clients:
            continue
        for t in clients[cid].get("traders", []):
            try:
                total_lots += float(t.get("admin_cumulative_lots", t.get("hedge_lots_cum", 0)) or 0)
            except: pass
    total_lots = round(total_lots, 2)
    per_lot_amount = round(rate * total_lots, 2)
    if   mode == "fixed":
        amount = fixed
    elif mode == "per_lot":
        amount = per_lot_amount
    elif mode == "hybrid_sum":
        amount = round(fixed + per_lot_amount, 2)
    elif mode == "hybrid_max":
        amount = max(fixed, per_lot_amount)
    else:
        amount = 0.0
    # Stato pagamento mese corrente
    cur_month = datetime.utcnow().strftime("%Y-%m")
    last_paid = dist.get("last_paid_month", "")
    paid_this_month = (last_paid == cur_month)
    # Calcolo mesi saltati: differenza in mesi tra mese corrente e ultimo pagato.
    # Se mai pagato (last_paid vuoto), missed_months = 0 (non avvisare al primo uso).
    missed_months = 0
    if last_paid:
        try:
            ly, lm = [int(x) for x in last_paid.split("-")]
            cy, cm = [int(x) for x in cur_month.split("-")]
            diff = (cy - ly) * 12 + (cm - lm)
            # diff = 0 → mese corrente già pagato
            # diff = 1 → mese precedente, normale
            # diff >= 2 → ho saltato uno o più mesi
            missed_months = max(0, diff - 1)
        except: pass
    return {
        "amount":        round(amount, 2),
        "fixed":         fixed,
        "per_lot":       per_lot_amount,
        "rate":          rate,
        "total_lots":    total_lots,
        "mode":          mode,
        "currency":      dist.get("currency", "USD"),
        "paid":          paid_this_month,
        "month":         cur_month,
        "last_paid":     last_paid,
        "missed_months": missed_months,
    }

@app.route("/api/distributor_billing/<dist_id>", methods=["GET"])
def distributor_billing(dist_id):
    """Ritorna importo dovuto al distributore + stato pagamento.
    Accessibile sia ad admin sia al distributore stesso."""
    admin_pw = request.headers.get("X-Admin-Password", "")
    dist_pw  = request.headers.get("X-Distributor-Password", "")
    is_admin = admin_pw == ADMIN_PASSWORD
    is_dist  = (dist_id in distributors and
                distributors[dist_id].get("password") == dist_pw)
    if not (is_admin or is_dist):
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False, "error": "Distributore non trovato"}), 404
    return jsonify({"ok": True, **_calc_distributor_amount(distributors[dist_id])})

@app.route("/api/distributor_billing_settings/<dist_id>", methods=["POST"])
def distributor_billing_settings(dist_id):
    """Modifica le impostazioni di pagamento di un distributore (solo admin)."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False, "error": "Distributore non trovato"}), 404
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("billing_mode", "").strip()
    if mode and mode in ("fixed", "per_lot", "hybrid_sum", "hybrid_max"):
        distributors[dist_id]["billing_mode"] = mode
    if "fixed_amount" in data:
        try: distributors[dist_id]["fixed_amount"] = float(data["fixed_amount"])
        except: pass
    if "per_lot_rate" in data:
        try: distributors[dist_id]["per_lot_rate"] = float(data["per_lot_rate"])
        except: pass
    if "currency" in data:
        cur = str(data["currency"]).strip().upper()[:5]
        if cur:
            distributors[dist_id]["currency"] = cur
    save_distributors()
    return jsonify({"ok": True, **_calc_distributor_amount(distributors[dist_id])})

@app.route("/api/distributor_mark_paid/<dist_id>", methods=["POST"])
def distributor_mark_paid(dist_id):
    """Segna come pagato/non pagato il mese corrente per il distributore (solo admin).
    Quando paid=True, azzera automaticamente i lotti hedge cumulativi di tutti
    i clienti del distributore (chiusura ciclo di fatturazione)."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False, "error": "Distributore non trovato"}), 404
    data = request.get_json(force=True, silent=True) or {}
    paid = bool(data.get("paid", True))
    cur_month = datetime.utcnow().strftime("%Y-%m")
    if paid:
        distributors[dist_id]["last_paid_month"] = cur_month
        # AUTO-RESET LOTTI: chiusura del ciclo di fatturazione
        target_cids = distributors[dist_id].get("clients", [])
        for cid in target_cids:
            if cid not in clients:
                continue
            for t in clients[cid].get("traders", []):
                t["hedge_lots_cum"] = 0.0
            clients[cid]["pending_command"]      = "reset_hedge_lots"
            clients[cid]["pending_trader_index"] = None
            clients[cid]["pending_command_ts"]   = time.time()
            save_client(cid, clients[cid])
        try: socketio.emit("update", clients)
        except: pass
    else:
        distributors[dist_id]["last_paid_month"] = ""
    save_distributors()
    return jsonify({"ok": True, **_calc_distributor_amount(distributors[dist_id])})

@app.route("/api/dist_reset_hedge_lots/<dist_id>", methods=["POST"])
def dist_reset_hedge_lots(dist_id):
    """Reset cumulativo lotti hedge di TUTTI i clienti di un distributore.
    Operazione admin-only e potenzialmente massiva: applica reset_hedge_lots
    a ciascun cliente del distributore.
    """
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False, "error": "Distributore non trovato"}), 404
    target_cids = distributors[dist_id].get("clients", [])
    n_done = 0
    for cid in target_cids:
        if cid not in clients:
            continue
        for t in clients[cid].get("traders", []):
            t["hedge_lots_cum"] = 0.0
        clients[cid]["pending_command"]      = "reset_hedge_lots"
        clients[cid]["pending_trader_index"] = None
        clients[cid]["pending_command_ts"]   = time.time()
        save_client(cid, clients[cid])
        n_done += 1
    socketio.emit("update", clients)
    return jsonify({"ok": True, "clients_reset": n_done})

@app.route("/api/set_client_name/<client_id>", methods=["POST"])
def set_client_name(client_id):
    admin_pw = request.headers.get("X-Admin-Password", "")
    dist_id  = request.headers.get("X-Distributor-Id", "")
    dist_pw  = request.headers.get("X-Distributor-Password", "")
    is_admin = admin_pw == ADMIN_PASSWORD
    is_dist  = (dist_id and dist_id in distributors and
                distributors[dist_id].get("password") == dist_pw and
                client_id in distributors[dist_id].get("clients", []))
    if not is_admin and not is_dist:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    name = (request.get_json(force=True, silent=True) or {}).get("name", "").strip()
    clients[client_id]["client_name"] = name
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

_COLORS_FILE = os.path.join(BASE_DIR, "card_colors.json")

# ── MESSAGGI RAPIDI ───────────────────────────────────────────────
_QUICK_MSGS_FILE = os.path.join(BASE_DIR, "quick_messages.json")
_DEFAULT_QUICK_MSGS = [
    {"id": 1, "label": "⚠️ Attenzione DD",   "text": "⚠️ Attenzione! Sei vicino al limite di drawdown giornaliero, opera con massima cautela."},
    {"id": 2, "label": "🛑 Stop operazioni", "text": "🛑 Ferma tutto e chiudi le posizioni, ti contatto a breve."},
    {"id": 3, "label": "✅ Via libera",       "text": "✅ Puoi operare normalmente, tutto ok."},
    {"id": 4, "label": "📞 Contattami",       "text": "📞 Contattami su Telegram appena puoi."},
    {"id": 5, "label": "🎯 Complimenti",      "text": "🎯 Ottimo lavoro! Fase completata con successo."},
    {"id": 6, "label": "🔄 Riavvia",          "text": "🔄 Riavvia Arbexa e riconnetti MetaTrader."},
]

def _load_quick_msgs():
    try:
        with open(_QUICK_MSGS_FILE) as f:
            return json.load(f)
    except:
        return _DEFAULT_QUICK_MSGS[:]

def _save_quick_msgs(msgs):
    try:
        with open(_QUICK_MSGS_FILE, "w") as f:
            json.dump(msgs, f, ensure_ascii=False, indent=2)
    except:
        pass

_quick_messages = _load_quick_msgs()

@app.route("/api/quick_messages")
def get_quick_messages():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "messages": _quick_messages})

@app.route("/api/quick_messages", methods=["POST"])
def save_quick_messages():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True) or {}
    msgs = data.get("messages", [])
    if not msgs:
        return jsonify({"ok": False, "error": "Dati mancanti"}), 400
    global _quick_messages
    _quick_messages = msgs
    _save_quick_msgs(msgs)
    return jsonify({"ok": True})

_AUTO_MSGS_FILE = os.path.join(BASE_DIR, "auto_messages.json")
_DEFAULT_AUTO_MSGS = {
    "welcome":    {"enabled": True, "label": "Benvenuto (primo avvio)",        "text": "📱 Sapevi che puoi controllare ArbeXa da remoto?\nAccedi al tuo pannello personale da qualsiasi dispositivo:\n👉 https://sniperpro.online/mypanel\nIl tuo ID: {id}\nPassword: ultime 4 cifre del tuo ID"},
    "dd_warning": {"enabled": True, "label": "DD giornaliero vicino (-1%)",    "text": "⚠️ Attenzione! Sei molto vicino al limite di drawdown giornaliero, opera con massima cautela."},
    "target":     {"enabled": True, "label": "Target raggiunto (PASSATA)",     "text": "🎯 Complimenti! Hai raggiunto il target, fase archiviata con successo!\nOttimo lavoro, continua così!"},
    "license":    {"enabled": True, "label": "Licenza in scadenza (3 giorni)", "text": "📅 La tua licenza scade tra {days} giorno/i! Contattami per rinnovarla."},
}

def _load_auto_msgs():
    try:
        with open(_AUTO_MSGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            for k, v in _DEFAULT_AUTO_MSGS.items():
                if k not in data:
                    data[k] = v
            return data
    except:
        return dict(_DEFAULT_AUTO_MSGS)

def _save_auto_msgs(data):
    with open(_AUTO_MSGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

_auto_msgs_config = _load_auto_msgs()

@app.route("/api/auto_messages")
def get_auto_messages():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "config": _auto_msgs_config})

@app.route("/api/auto_messages", methods=["POST"])
def save_auto_messages():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True) or {}
    config = data.get("config", {})
    if not config:
        return jsonify({"ok": False, "error": "Dati mancanti"}), 400
    global _auto_msgs_config
    _auto_msgs_config = config
    _save_auto_msgs(config)
    return jsonify({"ok": True})

def _auto_msg(cid, text):
    """Invia un messaggio automatico al cliente se non già inviato."""
    msg = {"text": text, "ts": datetime.now().strftime("%d/%m %H:%M"), "read": False, "auto": True}
    clients[cid].setdefault("messages", []).append(msg)
    clients[cid]["messages"] = clients[cid]["messages"][-20:]
    save_client(cid, clients[cid])

def _load_colors():
    try:
        with open(_COLORS_FILE) as f:
            d = json.load(f)
        return d.get("colors", {}), d.get("legend", {})
    except:
        return {}, {}

def _save_colors():
    try:
        with open(_COLORS_FILE, "w") as f:
            json.dump({"colors": _card_colors, "legend": _color_legend}, f)
    except:
        pass

_card_colors, _color_legend = _load_colors()

@app.route("/api/card_colors")
def get_card_colors():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "colors": _card_colors, "legend": _color_legend})

@app.route("/api/set_card_color/<client_id>", methods=["POST"])
def set_card_color(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    data  = request.get_json(force=True, silent=True) or {}
    color = data.get("color", "")
    if color:
        _card_colors[client_id] = color
    else:
        _card_colors.pop(client_id, None)
    _save_colors()
    return jsonify({"ok": True})

@app.route("/api/set_color_legend", methods=["POST"])
def set_color_legend():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    data  = request.get_json(force=True, silent=True) or {}
    color = data.get("color", "")
    label = data.get("label", "")
    if color:
        _color_legend[color] = label
    _save_colors()
    return jsonify({"ok": True})

@app.route("/api/send_message/<client_id>", methods=["POST"])
def send_message(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    text = (request.get_json(force=True, silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Messaggio vuoto"}), 400
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    msg = {"id": secrets.token_hex(6), "text": text, "ts": datetime.utcnow().strftime("%d/%m %H:%M"), "read": False}
    clients[client_id].setdefault("messages", []).append(msg)
    clients[client_id]["messages"] = clients[client_id]["messages"][-50:]
    save_clients(clients)
    socketio.emit("update", clients)
    # Notifica Telegram al cliente
    cli_tg = telegram_links.get("clients", {}).get(client_id)
    if cli_tg and cli_tg.get("chat_id"):
        send_telegram(cli_tg["chat_id"], "📩 <b>Nuovo messaggio da Admin</b>\n\n{}".format(text))
    return jsonify({"ok": True})

@app.route("/api/set_admin/<client_id>", methods=["POST"])
def set_admin(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    is_admin = bool((request.get_json(force=True, silent=True) or {}).get("is_admin", False))
    clients[client_id]["is_admin"] = is_admin
    save_client(client_id, clients[client_id])
    # Messaggio Telegram di conferma attivazione admin
    if is_admin:
        tg = telegram_links.get("clients", {}).get(client_id)
        if tg and tg.get("chat_id"):
            send_telegram(tg["chat_id"],
                "⭐ <b>Da ora sei Amministratore ARBEXA</b>\n\n"
                "Riceverai le seguenti notifiche da tutti i clienti:\n\n"
                "🆕 Nuovo cliente registrato\n"
                "🎯 Scheda PASSATA\n"
                "🔴 Scheda BRUCIATA\n"
                "💬 Risposta a un messaggio"
            )
    return jsonify({"ok": True, "is_admin": is_admin})

@app.route("/api/delete_chat/<client_id>", methods=["DELETE"])
def delete_chat(client_id):
    """Cancella tutta la chat di un cliente."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["messages"] = []
    clients[client_id]["unread_replies"] = 0
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    return jsonify({"ok": True})

@app.route("/api/delete_message/<client_id>/<msg_id>", methods=["DELETE"])
def delete_message(client_id, msg_id):
    """Cancella un singolo messaggio."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["messages"] = [m for m in clients[client_id].get("messages", []) if m.get("id") != msg_id]
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    return jsonify({"ok": True})

@app.route("/api/mark_read/<client_id>", methods=["POST"])
def mark_read(client_id):
    """Segna messaggi come letti dal web (mypanel)."""
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    for m in clients[client_id].get("messages", []):
        m["read"] = True
    save_clients(clients)
    return jsonify({"ok": True})

@app.route("/api/mark_read_exe/<client_id>", methods=["POST"])
def mark_read_exe(client_id):
    """Segna messaggi come letti dall'exe."""
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    for m in clients[client_id].get("messages", []):
        m["read_exe"] = True
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/reply_message/<client_id>", methods=["POST"])
def reply_message(client_id):
    cpw = request.headers.get("X-Client-Password", "").strip().lower()
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    stored = clients[client_id].get("client_password", "").strip().lower()
    if not stored or not _check_pw(cpw, stored):
        return jsonify({"ok": False}), 403
    text = (request.get_json(force=True, silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Testo vuoto"}), 400
    reply = {"id": secrets.token_hex(6), "text": text, "ts": datetime.now().strftime("%d/%m %H:%M"), "from": "client", "read": False}
    clients[client_id].setdefault("messages", []).append(reply)
    clients[client_id]["unread_replies"] = clients[client_id].get("unread_replies", 0) + 1
    save_client(client_id, clients[client_id])
    # Notifica Telegram ad admin
    notify_admins("💬 <b>Risposta da cliente {}</b>\n\n{}".format(client_id, text))
    # Notifica al distributore se il cliente è assegnato
    for did, dist in distributors.items():
        if client_id in dist.get("clients", []):
            d_tg = telegram_links.get("distributors", {}).get(did)
            if d_tg and d_tg.get("chat_id"):
                send_telegram(d_tg["chat_id"], "💬 <b>Risposta da cliente {}</b>\n\n{}".format(client_id, text))
            break
    return jsonify({"ok": True})

@app.route("/api/mark_replies_read/<client_id>", methods=["POST"])
def mark_replies_read(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["unread_replies"] = 0
    for m in clients[client_id].get("messages", []):
        if m.get("from") == "client":
            m["read"] = True
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/renew_license/<client_id>", methods=["POST"])
def renew_license(client_id):
    pw  = request.headers.get("X-Admin-Password")
    lpw = request.headers.get("X-License-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    if lpw != LICENSE_PASSWORD:
        return jsonify({"ok": False, "error": "Password licenze errata"}), 403
    days = int((request.get_json(force=True, silent=True) or {}).get("days", 30))
    if client_id not in clients:
        clients[client_id] = {}
    expiry = (date.today() + timedelta(days=days)).isoformat()
    clients[client_id]["license_expiry"] = expiry
    clients[client_id]["license_days"]   = days
    save_clients(clients)
    socketio.emit("update", clients)
    return jsonify({"ok": True, "expiry": expiry, "days": days})

@app.route("/api/generate_license", methods=["POST"])
def api_generate_license():
    pw = request.headers.get("X-License-Password")
    if pw != LICENSE_PASSWORD:
        return jsonify({"ok": False, "error": "Password licenza errata"}), 403
    ip = request.remote_addr
    if not _rate_limit(ip, max_calls=20, window=60):
        return jsonify({"ok": False, "error": "Troppe richieste"}), 429
    data = request.get_json(force=True, silent=True) or {}
    cid  = data.get("client_id", "").strip()
    days = data.get("days", 30)
    if not cid:
        return jsonify({"ok": False, "error": "ID mancante"}), 400
    code = generate_license(cid, days)
    if cid not in clients:
        clients[cid] = {}
    clients[cid].setdefault("used_codes", [])
    save_clients(clients)
    return jsonify({"ok": True, "code": code})

@app.route("/api/activate_license", methods=["POST"])
def activate_license():
    ip = request.remote_addr
    if not _rate_limit(ip, max_calls=5, window=60):
        return jsonify({"ok": False, "error": "Troppe richieste"}), 429
    data = request.get_json(force=True, silent=True) or {}
    cid  = data.get("client_id", "").strip()
    code = data.get("code", "").strip()
    if not cid or not code:
        return jsonify({"ok": False, "error": "Dati mancanti"}), 400
    try:
        import base64 as _b64
        def _xor_srv(s, key):
            return "".join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(s))
        decoded = _xor_srv(_b64.b64decode(code).decode("latin1"), _LKEY)
        parts   = decoded.split("|")
        if len(parts) != 3:
            return jsonify({"ok": False, "error": "Codice non valido"}), 400
        code_cid, expiry_str, sig = parts
        if code_cid != cid:
            return jsonify({"ok": False, "error": "Codice non valido per questo ID"}), 400
        raw      = "{}|{}".format(code_cid, expiry_str)
        expected = hashlib.sha256((_LKEY + raw).encode()).hexdigest()[:16]
        if sig != expected:
            return jsonify({"ok": False, "error": "Firma non valida"}), 400
        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        if expiry_date < date.today():
            return jsonify({"ok": False, "error": "Codice scaduto"}), 400
        if cid not in clients:
            clients[cid] = {}
        used_codes = clients[cid].get("used_codes", [])
        if code in used_codes:
            return jsonify({"ok": False, "error": "Codice gia utilizzato"}), 400
        days_left = (expiry_date - date.today()).days
        used_codes.append(code)
        clients[cid]["used_codes"]     = used_codes
        clients[cid]["license_expiry"] = expiry_str
        clients[cid]["license_days"]   = days_left
        save_clients(clients)
        return jsonify({"ok": True, "days_left": days_left, "expiry": expiry_str,
                        "message": "Licenza attivata! {} giorni rimasti.".format(days_left)})
    except Exception:
        return jsonify({"ok": False, "error": "Codice non valido"}), 400

@app.route("/api/command/<client_id>", methods=["POST"])
def send_command(client_id):
    admin_pw = request.headers.get("X-Admin-Password", "")
    dist_id  = request.headers.get("X-Distributor-Id", "")
    dist_pw  = request.headers.get("X-Distributor-Password", "")
    if admin_pw == ADMIN_PASSWORD:
        pass
    elif dist_id and dist_id in distributors and distributors[dist_id]["password"] == dist_pw:
        if client_id not in distributors[dist_id].get("clients", []):
            return jsonify({"ok": False, "error": "Cliente non assegnato"}), 403
    else:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True) or {}
    cmd  = data.get("command")
    tidx = data.get("trader_index")
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["pending_command"]      = cmd
    clients[client_id]["pending_trader_index"] = tidx
    clients[client_id]["pending_command_ts"]   = time.time()
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})


@app.route("/api/order_preview/<client_id>/<int:trader_idx>/<side>", methods=["GET"])
def order_preview(client_id, trader_idx, side):
    """Restituisce parametri preview ordine (lotti, SL, TP) per BUY o SELL.
    Accessibile da admin, distributor del cliente, o cliente stesso."""
    admin_pw = request.headers.get("X-Admin-Password", "")
    dist_id  = request.headers.get("X-Distributor-Id", "")
    dist_pw  = request.headers.get("X-Distributor-Password", "")
    cpw      = (request.headers.get("X-Client-Password", "") or session.get("client_pw_" + client_id, "")).strip().lower()

    # Auth: admin, distributor assegnato, oppure cliente con password
    auth_ok = False
    if admin_pw == ADMIN_PASSWORD:
        auth_ok = True
    elif dist_id and dist_id in distributors and distributors[dist_id]["password"] == dist_pw:
        if client_id in distributors[dist_id].get("clients", []):
            auth_ok = True
    elif cpw and client_id in clients:
        stored = clients[client_id].get("client_password", "").strip().lower()
        if stored and _check_pw(cpw, stored):
            auth_ok = True
        elif not stored and cpw == client_id[-4:].lower():
            auth_ok = True
    if not auth_ok:
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403

    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    traders = clients[client_id].get("traders", [])
    if trader_idx < 0 or trader_idx >= len(traders):
        return jsonify({"ok": False, "error": "Trader non valido"}), 404
    if side not in ("buy", "sell"):
        return jsonify({"ok": False, "error": "Side non valido"}), 400

    t = traders[trader_idx]
    # Parametri base dal trader
    stop_prop    = float(t.get("stop_prop", 1000) or 1000)
    target_hedge = float(t.get("target_hedge", 100) or 100)
    lotto_prop   = float(t.get("lotto_prop", 0.01) or 0.01)
    lotto_hedge  = float(t.get("lotto_hedge", 0.01) or 0.01)

    # Parse ratio (formato "1:4" oppure "3:1")
    def parse_ratio(s, default_a=1, default_b=4):
        try:
            parts = str(s).split(":")
            return int(parts[0]), int(parts[1])
        except:
            return default_a, default_b
    sl_p, tp_p = parse_ratio(t.get("ratio_prop", "1:4"), 1, 4)
    sl_h, tp_h = parse_ratio(t.get("ratio_hedge", "4:1"), 4, 1)

    # Calcoli SL/TP in dollari
    sl_p_usd = round(stop_prop, 0)
    tp_p_usd = round(stop_prop * tp_p / sl_p, 0) if sl_p else stop_prop
    tp_h_usd = round(target_hedge, 0)
    sl_h_usd = round(target_hedge * sl_h / tp_h, 0) if tp_h else target_hedge

    return jsonify({
        "ok":          True,
        "side":        side.upper(),
        "prop_firm":   t.get("prop_firm", "Scheda"),
        "size":        t.get("size", 0),
        "symbol_prop": t.get("simbolo_prop", "XAUUSD"),
        "symbol_hedge":t.get("simbolo_hedge", "XAUUSD"),
        "ratio":       f"{sl_p}:{tp_p}",
        "lotto_prop":  round(lotto_prop, 2),
        "lotto_hedge": round(lotto_hedge, 2),
        "sl_prop_usd": sl_p_usd,
        "tp_prop_usd": tp_p_usd,
        "tp_hedge_usd":tp_h_usd,
        "sl_hedge_usd":sl_h_usd,
        "positions":   int(t.get("positions", 0)),
        "auto_ratio":  bool(t.get("auto_ratio", True)),
    })

@app.route("/api/client_command/<client_id>", methods=["POST"])
def client_command(client_id):
    cpw = (request.headers.get("X-Client-Password", "") or session.get("client_pw_" + client_id, "")).strip().lower()
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    stored = clients[client_id].get("client_password", "").strip().lower()
    if not stored or not _check_pw(cpw, stored):
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    data = request.get_json(force=True, silent=True) or {}
    cmd  = data.get("command")
    tidx = data.get("trader_index")
    if cmd not in ["buy", "sell", "panic"]:
        return jsonify({"ok": False}), 400
    clients[client_id]["pending_command"]      = cmd
    clients[client_id]["pending_trader_index"] = tidx
    clients[client_id]["pending_command_ts"]   = time.time()
    save_clients(clients)
    return jsonify({"ok": True})

@app.route("/api/set_params/<client_id>", methods=["POST"])
def set_params(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    data = request.get_json(force=True, silent=True) or {}
    tidx = data.get("trader_index")
    params = {
        "stop_prop":    float(data.get("stop_prop", 1000) or 1000),
        "target_hedge": float(data.get("target_hedge", 200) or 200),
        "distanza":     int(data.get("distanza", 1000) or 1000),
        "ratio_prop":   data.get("ratio_prop", "1:3"),
        "ratio_hedge":  data.get("ratio_hedge", "3:1"),
        "auto_ratio":   bool(data.get("auto_ratio", True)),
        "trader_index": int(tidx) if tidx is not None else None,
    }
    clients[client_id]["server_params"]  = params
    clients[client_id]["params_pending"] = True
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/client_set_params/<client_id>", methods=["POST"])
def client_set_params(client_id):
    cpw = request.headers.get("X-Client-Password", "").strip().lower()
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    stored = clients[client_id].get("client_password", "").strip().lower()
    if not stored or not _check_pw(cpw, stored):
        return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    data = request.get_json(force=True, silent=True) or {}
    tidx = data.get("trader_index")
    params = {
        "stop_prop":    float(data.get("stop_prop", 1000) or 1000),
        "target_hedge": float(data.get("target_hedge", 200) or 200),
        "distanza":     int(data.get("distanza", 1000) or 1000),
        "ratio_prop":   data.get("ratio_prop", "1:3"),
        "ratio_hedge":  data.get("ratio_hedge", "3:1"),
        "auto_ratio":   bool(data.get("auto_ratio", True)),
        "trader_index": int(tidx) if tidx is not None else None,
    }
    clients[client_id]["server_params"]  = params
    clients[client_id]["params_pending"] = True
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/check_admin")
def check_admin():
    ip = request.remote_addr
    if not _rate_limit(ip, max_calls=20, window=60):
        return jsonify({"ok": False}), 429
    pw = request.headers.get("X-Admin-Password")
    return jsonify({"ok": pw == ADMIN_PASSWORD})

@app.route("/api/clients")
def get_clients():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({}), 403
    return jsonify(clients)

@app.route("/api/delete_client/<client_id>", methods=["DELETE"])
def delete_client(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id in clients:
        del clients[client_id]
        db_delete_client(client_id)
        _deleted_clients.add(client_id)
        for d in distributors.values():
            if client_id in d.get("clients", []):
                d["clients"].remove(client_id)
        save_distributors()
    socketio.emit("update", clients)
    return jsonify({"ok": True})

@app.route("/api/archive_manual/<client_id>/<int:trader_idx>", methods=["POST"])
def archive_manual(client_id, trader_idx):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    c = clients[client_id]
    traders = c.get("traders", [])
    if trader_idx >= len(traders):
        return jsonify({"ok": False, "error": "Trader non trovato"}), 404
    t = traders[trader_idx]
    data = request.get_json(force=True, silent=True) or {}
    esito      = data.get("esito", "MANUALE")
    size       = float(t.get("size", 0) or 0)
    prop_bal   = float(t.get("prop_bal", 0) or 0)
    costo_prop = float(t.get("costo_prop", 0) or 0)
    prop_cum_list  = c.get("prop_cumulative",  [])
    hedge_cum_list = c.get("hedge_cumulative", [])
    hedge_cum = sum(float(tr.get("profit", 0)) for tr in hedge_cum_list)
    prop_cum  = sum(float(tr.get("profit", 0)) for tr in prop_cum_list)
    archived_entry = {
        "esito":          esito,
        "archived_at":    datetime.utcnow().isoformat() + "Z",
        "prop_firm":      t.get("prop_firm", ""),
        "size":           size,
        "fase":           t.get("fase", ""),
        "costo_prop":     costo_prop,
        "prop_bal_final": prop_bal,
        "prop_cum":       round(prop_cum, 2),
        "hedge_bal_final":float(t.get("hedge_bal", 0) or 0),
        "hedge_cum":      round(hedge_cum, 2),
        "guadagno_hedge": round(hedge_cum - costo_prop, 2),
        "target_pct":     float(t.get("target_pct", 10) or 10),
        "dd_totale_pct":  float(t.get("dd_totale_pct", 10) or 10),
        "trader_index":   trader_idx,
    }
    c.setdefault("traders_archive", []).append(archived_entry)

    # Notifica distributore (se assegnato)
    _icon = "🎯" if esito == "PASSATA" else ("🔴" if esito == "BRUCIATA" else "📁")
    dist_msg = "{} <b>Scheda {} — Cliente {}</b>\n\nProp firm: {}\nSize: ${:,.0f}\nFase: {}\nBilancio finale PROP: ${:,.2f}".format(
        _icon, esito, client_id,
        archived_entry.get("prop_firm","?"),
        archived_entry.get("size",0),
        archived_entry.get("fase","?"),
        archived_entry.get("prop_bal_final",0)
    )
    for dist_id, dist in distributors.items():
        if client_id in dist.get("clients", []):
            d_info = telegram_links.get("distributors", {}).get(dist_id)
            if d_info and d_info.get("chat_id"):
                send_telegram(d_info["chat_id"], dist_msg)
            break
    notify_admins(dist_msg)

    c["prop_cumulative"]  = []
    c["hedge_cumulative"] = []
    c["traders"][trader_idx] = {
        "prop_firm":         t.get("prop_firm", ""),
        "size":              size,
        "costo_prop":        costo_prop,
        "dd_giornaliero_pct":t.get("dd_giornaliero_pct", 5),
        "dd_totale_pct":     float(t.get("dd_totale_pct", 10) or 10),
        "target_pct":        float(t.get("target_pct", 10) or 10),
        "fase":              t.get("fase", ""),
        "ea_trades":         [],
    }
    save_client(client_id, c)
    socketio.emit("update", clients)
    return jsonify({"ok": True, "esito": esito})

@app.route("/api/block_client/<client_id>", methods=["POST"])
def block_client(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["blocked"]    = True
    clients[client_id]["blocked_at"] = datetime.utcnow().isoformat() + "Z"
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/unblock_client/<client_id>", methods=["POST"])
def unblock_client(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["blocked"] = False
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/reset_fingerprint/<client_id>", methods=["POST"])
def reset_fingerprint(client_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    # Recupera fingerprint corrente per rimuoverlo anche dal blocco trial
    fp = clients[client_id].get("fingerprint", "")
    clients[client_id].pop("fingerprint", None)
    save_client(client_id, clients[client_id])
    # Rimuovi anche dal trial blocklist (consente nuovo trial dopo reinstallazione)
    if fp and fp in trial_fingerprints:
        del trial_fingerprints[fp]
        save_trial_fingerprints()
    return jsonify({"ok": True, "trial_reset": bool(fp)})

@app.route("/api/reset_trial_fingerprint", methods=["POST"])
def reset_trial_fingerprint():
    """Rimuove un fingerprint specifico dal trial blocklist.
    Usato per dare un nuovo trial a chi ha legittimamente cambiato/reinstallato PC."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True) or {}
    fp = data.get("fingerprint", "").strip()
    if not fp:
        return jsonify({"ok": False, "error": "fingerprint mancante"}), 400
    if fp in trial_fingerprints:
        del trial_fingerprints[fp]
        save_trial_fingerprints()
        return jsonify({"ok": True, "removed": True})
    return jsonify({"ok": True, "removed": False, "msg": "fingerprint non in blocklist"})

@app.route("/api/list_trial_fingerprints", methods=["GET"])
def list_trial_fingerprints():
    """Restituisce la lista dei fingerprint che hanno usato il trial."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "trials": trial_fingerprints, "count": len(trial_fingerprints)})

@app.route("/api/set_client_password", methods=["POST"])
def set_client_password():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True) or {}
    cid  = data.get("client_id")
    if not cid:
        return jsonify({"ok": False, "error": "Dati mancanti"}), 400
    if cid not in clients:
        clients[cid] = {}
    # Reset password = ultime 4 cifre dell'ID
    default_pw = cid[-4:].lower()
    clients[cid]["client_password"] = _hash_pw(default_pw)
    clients[cid]["first_login"] = True
    save_client(cid, clients[cid])
    # Invalida sessione cliente
    session.pop("client_pw_" + cid, None)
    session.pop("client_id", None)
    return jsonify({"ok": True, "default_pw": default_pw})

@app.route("/api/client_change_password/<client_id>", methods=["POST"])
def client_change_password(client_id):
    """Permette al cliente di cambiare la propria password."""
    ip = request.remote_addr
    if not _rate_limit(ip, max_calls=10, window=60):
        return jsonify({"ok": False, "error": "Troppi tentativi"}), 429
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    data = request.get_json(force=True, silent=True) or {}
    old_pw  = data.get("old_password", "").strip()
    new_pw  = data.get("new_password", "").strip()
    stored  = clients[client_id].get("client_password", "")
    if not stored or not _check_pw(old_pw, stored):
        return jsonify({"ok": False, "error": "Password attuale errata"}), 403
    if len(new_pw) < 4:
        return jsonify({"ok": False, "error": "Password troppo corta (min 4 caratteri)"}), 400
    clients[client_id]["client_password"] = _hash_pw(new_pw)
    clients[client_id]["first_login"] = False
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/client_login", methods=["POST"])
def client_login():
    ip = request.remote_addr
    if not _rate_limit(ip, max_calls=10, window=60):
        return jsonify({"ok": False, "error": "Troppi tentativi, riprova tra un minuto"}), 429
    data = request.get_json(force=True, silent=True) or {}
    cid  = data.get("client_id", "").strip()
    cpw  = data.get("password",  "").strip()
    if cid not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    stored = clients[cid].get("client_password", "")
    # Se non ha password, imposta default = ultime 4 cifre ID
    if not stored:
        default_pw = cid[-4:].lower()
        clients[cid]["client_password"] = _hash_pw(default_pw)
        clients[cid]["first_login"] = True
        save_client(cid, clients[cid])
        stored = clients[cid]["client_password"]
    if not _check_pw(cpw, stored):
        return jsonify({"ok": False, "error": "Password errata"}), 403
    if len(stored.strip()) != 64:
        _migrate_pw(cid, cpw)
    session["client_id"]           = cid
    session["client_pw_" + cid]    = cpw.lower()
    first_login = clients[cid].get("first_login", False)
    return jsonify({"ok": True, "redirect": "/mypanel/" + cid, "first_login": first_login})

@app.route("/api/client_data/<client_id>")
def client_data(client_id):
    cpw_header  = request.headers.get("X-Client-Password", "").strip()
    cpw_session = session.get("client_pw_" + client_id, "").strip()
    cpw = cpw_header or cpw_session
    if client_id not in clients:
        return jsonify({"ok": False, "auth_expired": True}), 404
    stored = clients[client_id].get("client_password", "")
    if not stored or not _check_pw(cpw, stored):
        return jsonify({"ok": False, "auth_expired": True}), 403
    # Aggiorna la sessione se la password header è valida e diversa da quella in sessione
    if cpw_header and cpw_header != cpw_session:
        session["client_pw_" + client_id] = cpw_header.lower()
    # Aggiungi distributor_id se il cliente ha un distributore
    resp = dict(clients[client_id])
    for did, dist in distributors.items():
        if client_id in dist.get("clients", []):
            resp["distributor_id"] = did
            resp["distributor_name"] = dist.get("name", did)
            break
    return jsonify(resp)

@app.route("/api/client_reply_dist/<client_id>", methods=["POST"])
def client_reply_dist(client_id):
    """Cliente risponde al suo distributore."""
    cpw = (request.headers.get("X-Client-Password", "") or session.get("client_pw_" + client_id, "")).strip()
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    stored = clients[client_id].get("client_password", "")
    if not stored or not _check_pw(cpw, stored):
        return jsonify({"ok": False}), 403
    # Trova il distributore del cliente
    dist_id = None
    for did, dist in distributors.items():
        if client_id in dist.get("clients", []):
            dist_id = did
            break
    if not dist_id:
        return jsonify({"ok": False, "error": "Nessun distributore assegnato"}), 400
    text = (request.get_json(force=True, silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Testo vuoto"}), 400
    # Salva il messaggio nella lista messaggi del cliente con tag from=client_to_dist
    msg = {"text": text, "ts": datetime.now().strftime("%d/%m %H:%M"),
           "from": "client", "to_dist": dist_id, "read": False}
    clients[client_id].setdefault("messages", []).append(msg)
    clients[client_id]["messages"] = clients[client_id]["messages"][-20:]
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/create_distributor", methods=["POST"])
def create_distributor():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    data     = request.get_json(force=True, silent=True) or {}
    dist_id  = data.get("id", "").strip().lower().replace(" ", "_")
    name     = data.get("name", "").strip()
    password = data.get("password", "").strip()
    if not dist_id or not password:
        return jsonify({"ok": False, "error": "ID e password obbligatori"}), 400
    if dist_id in distributors:
        return jsonify({"ok": False, "error": "ID gia esistente"}), 400
    distributors[dist_id] = {
        "id": dist_id, "name": name, "password": password, "clients": [],
        # NUOVO: campi billing/commissioni
        "billing_mode":   "fixed",   # fixed | per_lot | hybrid_sum | hybrid_max
        "fixed_amount":   0.0,        # importo fisso mensile in $
        "per_lot_rate":   0.0,        # $/lotto cumulativo
        "currency":       "USD",
        "last_paid_month": "",        # YYYY-MM dell'ultimo mese segnato pagato
    }
    save_distributors()
    return jsonify({"ok": True, "dist_id": dist_id})

@app.route("/api/delete_distributor/<dist_id>", methods=["DELETE"])
def delete_distributor(dist_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False}), 404
    del distributors[dist_id]
    save_distributors()
    return jsonify({"ok": True})

@app.route("/api/distributor_assign/<dist_id>", methods=["POST"])
def distributor_assign(dist_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False}), 404
    client_id = (request.get_json(force=True, silent=True) or {}).get("client_id", "").strip()
    if not client_id:
        return jsonify({"ok": False}), 400
    if client_id not in distributors[dist_id]["clients"]:
        distributors[dist_id]["clients"].append(client_id)
    save_distributors()
    return jsonify({"ok": True})

@app.route("/api/distributor_unassign/<dist_id>", methods=["POST"])
def distributor_unassign(dist_id):
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False}), 404
    client_id = (request.get_json(force=True, silent=True) or {}).get("client_id", "").strip()
    if client_id in distributors[dist_id]["clients"]:
        distributors[dist_id]["clients"].remove(client_id)
    save_distributors()
    return jsonify({"ok": True})

@app.route("/api/distributors")
def get_distributors():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({}), 403
    return jsonify(distributors)

@app.route("/api/distributor_login", methods=["POST"])
def distributor_login():
    ip = request.remote_addr
    if not _rate_limit(ip, max_calls=10, window=60):
        return jsonify({"ok": False, "error": "Troppi tentativi, riprova tra un minuto"}), 429
    data    = request.get_json(force=True, silent=True) or {}
    dist_id = data.get("dist_id", "").strip().lower()
    dpw     = data.get("password", "").strip()
    if dist_id not in distributors:
        return jsonify({"ok": False, "error": "Distributore non trovato"}), 404
    if distributors[dist_id]["password"] != dpw:
        return jsonify({"ok": False, "error": "Password errata"}), 403
    session["dist_id"]             = dist_id
    session["dist_pw_" + dist_id]  = dpw
    return jsonify({"ok": True})

@app.route("/api/distributor_clients/<dist_id>")
def distributor_clients(dist_id):
    dpw = (request.headers.get("X-Distributor-Password", "") or
           session.get("dist_pw_" + dist_id, "")).strip()
    if dist_id not in distributors:
        return jsonify({"ok": False}), 404
    if distributors[dist_id]["password"] != dpw:
        return jsonify({"ok": False}), 403
    result = {}
    for cid in distributors[dist_id].get("clients", []):
        if cid not in clients:
            continue
        c = clients[cid]
        prop_cum_list  = c.get("prop_cumulative",  [])
        hedge_cum_list = c.get("hedge_cumulative", [])
        prop_total  = round(sum(float(tr.get("profit", 0)) for tr in prop_cum_list),  2)
        hedge_total = round(sum(float(tr.get("profit", 0)) for tr in hedge_cum_list), 2)
        traders_view = []
        for t in c.get("traders", []):
            traders_view.append({
                "prop_firm":          t.get("prop_firm", ""),
                "prop_bal":           t.get("prop_bal", 0),
                "hedge_bal":          t.get("hedge_bal", 0),
                "prop_pnl":           t.get("prop_pnl", 0),
                "hedge_pnl":          t.get("hedge_pnl", 0),
                "positions":          t.get("positions", 0),
                "size":               t.get("size", 0),
                "dd_giornaliero_pct": t.get("dd_giornaliero_pct", 5),
                "dd_totale_pct":      t.get("dd_totale_pct", 10),
                "target_pct":         t.get("target_pct", 10),
                "fase":               t.get("fase", ""),
                "trades_cumulative":  prop_cum_list + hedge_cum_list,
                "prop_total":         prop_total,
                "hedge_total":        hedge_total,
                "prop_broker":        t.get("prop_broker", ""),
                "hedge_broker":       t.get("hedge_broker", ""),
                "prop_account":       t.get("prop_account", ""),
                "hedge_account":      t.get("hedge_account", ""),
                # NUOVI campi necessari per profitto netto live e fatturazione
                "costo_prop":         t.get("costo_prop", 0),
                "hedge_pnl_cum":      t.get("hedge_pnl_cum", 0),
                "hedge_lots_cum":     t.get("hedge_lots_cum", 0),
            })
        result[cid] = {
            "client_id":       cid,
            "client_name":     c.get("client_name", ""),
            "license_days":    _calc_license_days(c),
            "license_expiry":  c.get("license_expiry", ""),
            "last_seen":       c.get("last_seen", ""),
            "blocked":         c.get("blocked", False),
            "traders":         traders_view,
            "prop_cumulative": prop_cum_list,
            "hedge_cumulative":hedge_cum_list,
            "totals_reset_ts": c.get("totals_reset_ts", ""),
        }
    return jsonify({"ok": True, "clients": result,
                    "dist_name": distributors[dist_id].get("name", dist_id)})

@app.route("/api/distributor_block/<dist_id>/<client_id>", methods=["POST"])
def distributor_block(dist_id, client_id):
    dpw = request.headers.get("X-Distributor-Password", "")
    if dist_id not in distributors:
        return jsonify({"ok": False}), 404
    if distributors[dist_id]["password"] != dpw:
        return jsonify({"ok": False}), 403
    if client_id not in distributors[dist_id].get("clients", []):
        return jsonify({"ok": False, "error": "Cliente non assegnato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["blocked"]    = True
    clients[client_id]["blocked_at"] = datetime.utcnow().isoformat() + "Z"
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/distributor_unblock/<dist_id>/<client_id>", methods=["POST"])
def distributor_unblock(dist_id, client_id):
    dpw = request.headers.get("X-Distributor-Password", "")
    if dist_id not in distributors:
        return jsonify({"ok": False}), 404
    if distributors[dist_id]["password"] != dpw:
        return jsonify({"ok": False}), 403
    if client_id not in distributors[dist_id].get("clients", []):
        return jsonify({"ok": False, "error": "Cliente non assegnato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["blocked"] = False
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/dist_set_params/<client_id>", methods=["POST"])
def dist_set_params(client_id):
    dist_id = request.headers.get("X-Distributor-Id", "")
    dist_pw = request.headers.get("X-Distributor-Password", "")
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False}), 403
    if client_id not in distributors[dist_id].get("clients", []):
        return jsonify({"ok": False, "error": "Cliente non assegnato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    data = request.get_json(force=True, silent=True) or {}
    tidx = data.get("trader_index")
    params = {
        "stop_prop":    float(data.get("stop_prop", 1000) or 1000),
        "target_hedge": float(data.get("target_hedge", 200) or 200),
        "distanza":     int(data.get("distanza", 1000) or 1000),
        "ratio_prop":   data.get("ratio_prop", "1:3"),
        "ratio_hedge":  data.get("ratio_hedge", "3:1"),
        "auto_ratio":   bool(data.get("auto_ratio", True)),
        "trader_index": int(tidx) if tidx is not None else None,
    }
    clients[client_id]["server_params"]  = params
    clients[client_id]["params_pending"] = True
    save_client(client_id, clients[client_id])
    return jsonify({"ok": True})

@app.route("/api/dist_send_message/<client_id>", methods=["POST"])
def dist_send_message(client_id):
    """Distributore invia messaggio a un suo cliente."""
    dist_id = request.headers.get("X-Distributor-Id", "")
    dist_pw = request.headers.get("X-Distributor-Password", "")
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False}), 403
    if client_id not in distributors[dist_id].get("clients", []):
        return jsonify({"ok": False, "error": "Cliente non assegnato"}), 403
    if client_id not in clients:
        return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
    text = (request.get_json(force=True, silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Messaggio vuoto"}), 400
    msg = {"id": secrets.token_hex(6), "text": text, "ts": datetime.now().strftime("%d/%m %H:%M"), "read": False, "from_dist": dist_id}
    clients[client_id].setdefault("messages", []).append(msg)
    clients[client_id]["messages"] = clients[client_id]["messages"][-50:]
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    # Notifica Telegram al cliente
    cli_tg = telegram_links.get("clients", {}).get(client_id)
    if cli_tg and cli_tg.get("chat_id"):
        send_telegram(cli_tg["chat_id"], "📩 <b>Nuovo messaggio dal tuo operatore</b>\n\n{}".format(text))
    return jsonify({"ok": True})

@app.route("/api/dist_delete_chat/<client_id>", methods=["DELETE"])
def dist_delete_chat(client_id):
    """Distributore cancella chat con un suo cliente."""
    dist_id = request.headers.get("X-Distributor-Id", "")
    dist_pw = request.headers.get("X-Distributor-Password", "")
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False}), 403
    if client_id not in distributors[dist_id].get("clients", []):
        return jsonify({"ok": False}), 403
    if client_id not in clients:
        return jsonify({"ok": False}), 404
    clients[client_id]["messages"] = []
    clients[client_id]["unread_replies"] = 0
    save_client(client_id, clients[client_id])
    socketio.emit("update", clients)
    return jsonify({"ok": True})

@app.route("/api/dist_send_all/<dist_id>", methods=["POST"])
def dist_send_all(dist_id):
    """Distributore invia messaggio a tutti i suoi clienti."""
    dist_pw = request.headers.get("X-Distributor-Password", "")
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False}), 403
    text = (request.get_json(force=True, silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Messaggio vuoto"}), 400
    sent = 0
    for cid in distributors[dist_id].get("clients", []):
        if cid in clients:
            msg = {"text": text, "ts": datetime.now().strftime("%d/%m %H:%M"), "read": False, "from_dist": dist_id}
            clients[cid].setdefault("messages", []).append(msg)
            clients[cid]["messages"] = clients[cid]["messages"][-20:]
            save_client(cid, clients[cid])
            sent += 1
    socketio.emit("update", clients)
    return jsonify({"ok": True, "sent": sent})

@app.route("/api/dist_get_messages/<dist_id>")
def dist_get_messages(dist_id):
    """Distributore legge messaggi ricevuti dai suoi clienti."""
    dist_pw = request.headers.get("X-Distributor-Password", "")
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False}), 403
    result = []
    for cid in distributors[dist_id].get("clients", []):
        if cid not in clients: continue
        msgs = [m for m in clients[cid].get("messages", [])
                if m.get("from") == "client" and m.get("to_dist") == dist_id]
        if msgs:
            result.append({"client_id": cid,
                           "client_name": clients[cid].get("client_name", ""),
                           "messages": msgs,
                           "unread": sum(1 for m in msgs if not m.get("read"))})
    return jsonify({"ok": True, "threads": result})

@app.route("/api/send_message_to_distributor/<dist_id>", methods=["POST"])
def send_message_to_distributor(dist_id):
    """Admin invia messaggio a un distributore (salvato nel dict distributors)."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False, "error": "Distributore non trovato"}), 404
    text = (request.get_json(force=True, silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Messaggio vuoto"}), 400
    msg = {"id": secrets.token_hex(6), "text": text, "ts": datetime.now().strftime("%d/%m %H:%M"), "read": False}
    distributors[dist_id].setdefault("messages", []).append(msg)
    distributors[dist_id]["messages"] = distributors[dist_id]["messages"][-50:]
    save_distributors()
    return jsonify({"ok": True})

@app.route("/api/delete_dist_message/<dist_id>/<msg_id>", methods=["DELETE"])
def delete_dist_message(dist_id, msg_id):
    """Admin cancella singolo messaggio a distributore."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False}), 404
    distributors[dist_id]["messages"] = [m for m in distributors[dist_id].get("messages", []) if m.get("id") != msg_id]
    save_distributors()
    return jsonify({"ok": True})

@app.route("/api/delete_dist_chat/<dist_id>", methods=["DELETE"])
def delete_dist_chat(dist_id):
    """Admin cancella tutta la chat con un distributore."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False}), 404
    distributors[dist_id]["messages"] = []
    save_distributors()
    return jsonify({"ok": True})

@app.route("/api/dist_inbox/<dist_id>")
def dist_inbox(dist_id):
    """Distributore legge i propri messaggi da admin."""
    dist_pw = request.headers.get("X-Distributor-Password", "")
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False}), 403
    msgs = distributors[dist_id].get("messages", [])
    # Segna come letti
    for m in msgs:
        m["read"] = True
    save_distributors()
    return jsonify({"ok": True, "messages": msgs})

@app.route("/api/dist_reply_to_admin/<dist_id>", methods=["POST"])
def dist_reply_to_admin(dist_id):
    """Distributore risponde all'admin — salva come messaggio nel dict distributors."""
    dist_pw = request.headers.get("X-Distributor-Password", "")
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False}), 403
    text = (request.get_json(force=True, silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Testo vuoto"}), 400
    msg = {"text": text, "ts": datetime.now().strftime("%d/%m %H:%M"),
           "read": False, "from": "distributor"}
    distributors[dist_id].setdefault("replies", []).append(msg)
    distributors[dist_id]["replies"] = distributors[dist_id]["replies"][-20:]
    distributors[dist_id]["unread_replies"] = distributors[dist_id].get("unread_replies", 0) + 1
    save_distributors()
    return jsonify({"ok": True})

@app.route("/api/dist_replies/<dist_id>")
def get_dist_replies(dist_id):
    """Admin legge le risposte del distributore."""
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    if dist_id not in distributors:
        return jsonify({"ok": False}), 404
    replies = distributors[dist_id].get("replies", [])
    for r in replies:
        r["read"] = True
    distributors[dist_id]["unread_replies"] = 0
    save_distributors()
    return jsonify({"ok": True, "replies": replies})
def get_dist_quick_messages():
    """Distributore legge i messaggi rapidi."""
    dist_pw = request.headers.get("X-Distributor-Password", "")
    dist_id = request.headers.get("X-Distributor-Id", "")
    if dist_id not in distributors or distributors[dist_id]["password"] != dist_pw:
        return jsonify({"ok": False}), 403
    return jsonify({"ok": True, "messages": _quick_messages})
@app.route("/distributor/<dist_id>")
def distributor_panel(dist_id):
    if dist_id not in distributors:
        return "<script>location.href='/'</script>"
    dpw = request.args.get("pw", "").strip()
    if not dpw:
        dpw = session.get("dist_pw_" + dist_id, "")
    if distributors[dist_id]["password"] != dpw:
        return render_template_string("""<!DOCTYPE html>
<html lang="it" translate="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARBEXA - Distributore</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{background:#0a0f1a;color:#fff;font-family:'Segoe UI',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;}
.box{background:#0A1628;border:2px solid #d4af37;border-radius:16px;padding:40px;text-align:center;width:340px;}
.logo{color:#c8a030;font-size:20px;font-weight:bold;letter-spacing:2px;margin-bottom:4px;}
.sub{color:#d4af37;font-size:11px;letter-spacing:1px;margin-bottom:28px;}
input{width:100%;padding:12px;background:#0D1F3C;border:1px solid #1A3A6A;border-radius:8px;color:#fff;font-size:14px;margin-bottom:12px;outline:none;}
input:focus{border-color:#c8a030;}
button{width:100%;padding:12px;background:#1A3A5C;color:#fff;border:1px solid #c8a030;border-radius:8px;font-size:14px;font-weight:bold;cursor:pointer;}
button:hover{background:#25527A;}
.err{color:#FF3355;font-size:12px;margin-top:10px;min-height:18px;}
</style></head><body>
<div class="box">
  <div class="logo">ARBEXA</div>
  <div class="sub">AREA DISTRIBUTORE</div>
  <input id="dpw" type="password" placeholder="Password distributore">
  <button onclick="login()">ACCEDI</button>
  <div class="err" id="err"></div>
</div>
<script>
function login(){
  var pw=document.getElementById('dpw').value.trim();
  if(!pw){document.getElementById('err').textContent='Inserisci la password';return;}
  fetch('/api/distributor_login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({dist_id:'""" + dist_id + """',password:pw})})
  .then(r=>r.json()).then(function(d){
    if(d.ok) window.location.href='/distributor/""" + dist_id + """?pw='+encodeURIComponent(pw);
    else document.getElementById('err').textContent=d.error||'Errore';
  });
}
document.addEventListener('keydown',function(e){if(e.key==='Enter')login();});
</script></body></html>""")
    session["dist_pw_" + dist_id] = dpw
    dist_name = distributors[dist_id].get("name", dist_id)
    dist_panel_html = open(os.path.join(BASE_DIR, "distributor.html")).read()
    dist_panel_html = dist_panel_html.replace("{DIST_ID}",   esc(dist_id))
    dist_panel_html = dist_panel_html.replace("{DIST_PW}",   esc(dpw))
    dist_panel_html = dist_panel_html.replace("{DIST_NAME}", esc(dist_name))
    return dist_panel_html

@app.route("/mypanel")
def client_panel_login():
    return render_template_string("""<!DOCTYPE html>
<html lang="it" translate="no"><head><meta charset="utf-8">
<meta name="google" content="notranslate"><title>Arbexa - Il mio pannello</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{margin:0;padding:0;box-sizing:border-box;}
body{background:#0a0f1a;color:#fff;font-family:'Segoe UI',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;}
.box{background:#0A1628;border:2px solid #d4af37;border-radius:16px;padding:40px;text-align:center;width:340px;}
.logo{color:#c8a030;font-size:20px;font-weight:bold;letter-spacing:2px;margin-bottom:4px;}
.sub{color:#d4af37;font-size:11px;letter-spacing:1px;margin-bottom:28px;}
input{width:100%;padding:12px;background:#0D1F3C;border:1px solid #1A3A6A;border-radius:8px;color:#fff;font-size:14px;margin-bottom:12px;outline:none;}
input:focus{border-color:#c8a030;}
button{width:100%;padding:12px;background:#1A3A5C;color:#fff;border:1px solid #c8a030;border-radius:8px;font-size:14px;font-weight:bold;cursor:pointer;letter-spacing:1px;}
button:hover{background:#25527A;}
.err{color:#FF3355;font-size:12px;margin-top:10px;min-height:18px;}
</style></head><body>
<div class="box">
  <div class="logo">ARBEXA</div>
  <div class="sub">AREA PERSONALE CLIENTE</div>
  <input id="cid" placeholder="Il tuo ID Cliente" autocomplete="off">
  <input id="cpw" type="password" placeholder="Password personale">
  <button onclick="login()">ACCEDI</button>
  <div class="err" id="err"></div>
</div>
<script>
function login(){
  var cid=document.getElementById('cid').value.trim();
  var cpw=document.getElementById('cpw').value.trim();
  if(!cid||!cpw){document.getElementById('err').textContent='Compila tutti i campi';return;}
  fetch('/api/client_login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_id:cid,password:cpw})})
  .then(r=>r.json()).then(function(d){
    if(d.ok) window.location.href = d.redirect || '/mypanel/'+cid;
    else document.getElementById('err').textContent=d.error||'Errore di accesso';
  }).catch(function(){document.getElementById('err').textContent='Errore di connessione';});
}
document.addEventListener('keydown',function(e){if(e.key==='Enter')login();});
</script></body></html>""")

@app.route("/mypanel/<client_id>")
def client_dashboard(client_id):
    cpw = session.get("client_pw_" + client_id, "")
    if not cpw:
        return "<script>location.href='/mypanel'</script>"
    if client_id not in clients:
        return "<script>location.href='/mypanel'</script>"
    stored_pw = clients[client_id].get("client_password", "")
    if not stored_pw or not _check_pw(cpw, stored_pw):
        return "<script>location.href='/mypanel'</script>"
    c        = clients[client_id]
    lic_days = _calc_license_days(c)
    lic_exp  = c.get("license_expiry", "")
    if lic_exp:
        try:
            parts = lic_exp.split("-")
            lic_exp_fmt = "{}/{}/{}".format(parts[2], parts[1], parts[0])
        except:
            lic_exp_fmt = lic_exp
    else:
        lic_exp_fmt = "-"
    lic_color = "#00FF88" if lic_days > 7 else ("#FFB800" if lic_days > 0 else "#FF3355")
    lic_text  = "Licenza: {} giorni".format(lic_days) if lic_days > 0 else "LICENZA SCADUTA"
    unread    = sum(1 for m in c.get("messages",[]) if not m.get("read") and m.get("from","admin")=="admin")
    first_login = c.get("first_login", False)
    with open(os.path.join(BASE_DIR, "mypanel.html"), encoding="utf-8") as f:
        html_out = f.read()
    for k, v in [("{client_id}", client_id), ("{cpw}", cpw), ("{lic_days}", str(lic_days)),
                 ("{lic_exp}", lic_exp_fmt), ("{unread}", str(unread)),
                 ("{lic_color}", lic_color), ("{lic_text}", lic_text),
                 ("{first_login}", str(first_login))]:
        html_out = html_out.replace(k, str(v))
    return html_out

# ── TELEGRAM ENDPOINTS ──────────────────────────────────────────────
def _find_role_by_chat(chat_id):
    """Ritorna ('client', cid) o ('distributor', did) o (None, None)
    in base al chat_id Telegram."""
    for cid, info in telegram_links.get("clients", {}).items():
        if info.get("chat_id") == chat_id:
            return ("client", cid)
    for did, info in telegram_links.get("distributors", {}).items():
        if info.get("chat_id") == chat_id:
            return ("distributor", did)
    return (None, None)

def _fmt_money(v):
    sign = "+" if v >= 0 else ""
    return "{}{:.2f} $".format(sign, v)

def _tg_oggi_client(cid):
    """Resoconto P&L oggi per cliente (basato su HEDGE che è il conto reale)."""
    if cid not in clients:
        return "Cliente non trovato."
    traders = clients[cid].get("traders", [])
    if not traders:
        return "Nessuna scheda configurata."
    lines = ["📊 <b>OGGI</b> - " + datetime.utcnow().strftime("%d/%m"), "─" * 25]
    total = 0.0
    for t in traders:
        firm = t.get("prop_firm", "?")
        pnl_d = float(t.get("hedge_pnl_day", 0) or 0)  # HEDGE = conto reale
        total += pnl_d
        icon = "✅" if pnl_d >= 0 else "🔴"
        lines.append("{} <b>{}</b>: {}".format(icon, firm, _fmt_money(pnl_d)))
    lines.append("─" * 25)
    icon_tot = "✅" if total >= 0 else "🔴"
    lines.append("{} <b>Totale: {}</b>".format(icon_tot, _fmt_money(total)))
    return "\n".join(lines)

def _tg_stato_client(cid):
    """Stato live tutte le schede del cliente. Mostra dati HEDGE (conto reale)."""
    if cid not in clients:
        return "Cliente non trovato."
    traders = clients[cid].get("traders", [])
    if not traders:
        return "Nessuna scheda configurata."
    lines = ["⚡ <b>STATO LIVE</b>", "─" * 25]
    for t in traders:
        firm = t.get("prop_firm", "?")
        # HEDGE bal/pnl (conto reale)
        hb     = float(t.get("hedge_bal", 0) or 0)
        hp     = float(t.get("hedge_pnl", 0) or 0)
        h_eq   = hb + hp
        h_cum  = float(t.get("hedge_pnl_cum", 0) or 0)
        cost   = float(t.get("costo_prop", 0) or 0)
        netto  = h_cum + hp - cost  # profitto netto (chiuso + floating - costo)
        pos    = int(t.get("positions", 0) or 0)
        pnl_d  = float(t.get("hedge_pnl_day", 0) or 0)
        # PROP per riferimento DD/Target
        size   = float(t.get("size", 0) or 0)
        pb     = float(t.get("prop_bal", 0) or 0)
        dd_pct  = float(t.get("dd_giornaliero_pct", 5) or 5)
        dd_lim  = size * dd_pct / 100
        # Prendiamo il prop_pnl_day per il DD (è il valore monitorato dal sistema)
        prop_pnl_d = float(t.get("prop_pnl_day", 0) or 0)
        dd_rim  = max(dd_lim - max(-prop_pnl_d, 0), 0)
        tgt_pct = float(t.get("target_pct", 10) or 10)
        tgt_bal = size * (1 + tgt_pct/100)
        manca = max(tgt_bal - pb, 0)
        lines.append("<b>{}</b> ({})".format(firm, t.get("fase", "?")))
        lines.append("  💰 HEDGE Bal: ${:,.2f} | Eq: ${:,.2f}".format(hb, h_eq))
        if pos > 0:
            lines.append("  🔵 Posizioni aperte: {} ({})".format(pos, _fmt_money(hp)))
        lines.append("  📈 P&amp;L oggi (HEDGE): {}".format(_fmt_money(pnl_d)))
        lines.append("  📊 Profitto netto: <b>{}</b>".format(_fmt_money(netto)))
        lines.append("  💳 Saldo PROP: ${:,.2f}".format(pb))
        lines.append("")
    return "\n".join(lines).strip()

def _tg_storico_client(cid, limit=10):
    """Ultime N operazioni chiuse per cliente. Legge da archivio per scheda."""
    if cid not in clients:
        return "Cliente non trovato."
    # Cerca nei file trades_history per ogni trader (locale al server: si usa chiusure aggregate)
    # Visto che il server non memorizza storico granulare per scheda, ricaviamo dai messaggi
    # eventualmente. Soluzione semplice: leggiamo dall'archivio cicli broker_history.
    traders = clients[cid].get("traders", [])
    if not traders:
        return "Nessuna scheda."
    lines = ["📜 <b>ULTIME OPERAZIONI</b>", "─" * 25]
    # Per ogni trader, ottieni le ultime ops dal trades_history salvato (se presente)
    found = []
    for t in traders:
        firm = t.get("prop_firm", "?")
        pnl_d = float(t.get("hedge_pnl_day", 0) or 0)  # HEDGE
        found.append((firm, pnl_d))
    if found:
        for firm, pnl in found[-limit:]:
            icon = "✅" if pnl >= 0 else "🔴"
            lines.append("{} <b>{}</b>: {} (oggi)".format(icon, firm, _fmt_money(pnl)))
    # Aggiungi storico cicli se disponibili
    hist = broker_history.get(cid, [])
    if hist:
        lines.append("")
        lines.append("<b>📦 Cicli archiviati:</b>")
        for h in hist[-5:][::-1]:
            firm = h.get("prop_firm", "?")
            pn = float(h.get("profitto_netto", 0) or 0)
            icon = "✅" if pn >= 0 else "🔴"
            lines.append("{} {}: {}".format(icon, firm, _fmt_money(pn)))
    return "\n".join(lines)

def _tg_mese_client(cid):
    """Riepilogo mese: usa storico cicli + P&L odierno."""
    if cid not in clients:
        return "Cliente non trovato."
    traders = clients[cid].get("traders", [])
    # Calcola netto del mese (cicli archiviati + P&L oggi)
    from datetime import datetime as _dt
    now = _dt.utcnow()
    month_str = now.strftime("%Y-%m")
    cicli = broker_history.get(cid, [])
    cicli_mese = [c for c in cicli if c.get("ts", "").startswith(month_str)]
    pn_cicli = sum(float(c.get("profitto_netto", 0) or 0) for c in cicli_mese)
    pnl_oggi = sum(float(t.get("hedge_pnl_day", 0) or 0) for t in traders)
    netto = pn_cicli + pnl_oggi
    lines = ["📅 <b>MESE</b> - " + now.strftime("%B %Y"), "─" * 25]
    lines.append("Cicli archiviati: {}".format(len(cicli_mese)))
    lines.append("Profitto netto cicli: {}".format(_fmt_money(pn_cicli)))
    lines.append("P&amp;L oggi (live): {}".format(_fmt_money(pnl_oggi)))
    lines.append("─" * 25)
    icon = "✅" if netto >= 0 else "🔴"
    lines.append("{} <b>Totale mese: {}</b>".format(icon, _fmt_money(netto)))
    return "\n".join(lines)

def _tg_clienti_dist(did):
    """Lista clienti del distributore con P&L oggi."""
    if did not in distributors:
        return "Distributore non trovato."
    cli_ids = distributors[did].get("clients", [])
    if not cli_ids:
        return "Nessun cliente assegnato."
    lines = ["👥 <b>I TUOI CLIENTI</b>", "─" * 25]
    for cid in cli_ids:
        c = clients.get(cid, {})
        traders = c.get("traders", [])
        # Stato online: ultimo last_seen < 5 min
        last = c.get("last_seen", "")
        online = False
        try:
            if last:
                last_dt = datetime.fromisoformat(last.replace("Z", ""))
                online = (datetime.utcnow() - last_dt).total_seconds() < 300
        except: pass
        pnl_oggi = sum(float(t.get("hedge_pnl_day", 0) or 0) for t in traders)
        status_icon = "🟢" if online else "⚫"
        pnl_icon = "✅" if pnl_oggi >= 0 else "🔴"
        lines.append("{} <code>{}</code> {} {}".format(
            status_icon, cid, pnl_icon, _fmt_money(pnl_oggi)))
    return "\n".join(lines)

def _tg_oggi_dist(did):
    """Aggregato P&L di oggi di tutti i clienti del distributore."""
    if did not in distributors:
        return "Distributore non trovato."
    cli_ids = distributors[did].get("clients", [])
    if not cli_ids:
        return "Nessun cliente assegnato."
    lines = ["📊 <b>OGGI - I TUOI CLIENTI</b>", "─" * 25]
    total = 0.0
    for cid in cli_ids:
        c = clients.get(cid, {})
        traders = c.get("traders", [])
        pnl = sum(float(t.get("hedge_pnl_day", 0) or 0) for t in traders)
        total += pnl
        icon = "✅" if pnl >= 0 else "🔴"
        lines.append("{} <code>{}</code>: {}".format(icon, cid, _fmt_money(pnl)))
    lines.append("─" * 25)
    icon_tot = "✅" if total >= 0 else "🔴"
    lines.append("{} <b>Aggregato: {}</b>".format(icon_tot, _fmt_money(total)))
    return "\n".join(lines)

@app.route("/api/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Riceve gli aggiornamenti dal bot Telegram (messaggi /start, codici di linking).
    Telegram invia un POST quando un utente scrive al bot."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        msg  = data.get("message", {})
        chat = msg.get("chat", {}) or {}
        text = (msg.get("text") or "").strip()
        chat_id = chat.get("id")
        if not chat_id:
            return jsonify({"ok": True})
        username = chat.get("username") or chat.get("first_name") or ""
        # Comando /start: spiega come collegarsi
        if text.startswith("/start"):
            # Genera un codice di 6 cifre per questo chat_id
            code = gen_link_code()
            telegram_links["pending"][code] = {
                "chat_id": chat_id, "username": username, "ts": time.time()
            }
            save_telegram_links()
            send_telegram(chat_id,
                "👋 <b>Benvenuto in ARBEXA Notify!</b>\n\n"
                "Per collegare il tuo account, usa questo codice:\n\n"
                "<b>🔑 <code>{}</code></b>\n\n"
                "Inseriscilo nel tuo pannello (MyPanel oppure Distributor) "
                "→ Account → Notifiche Telegram.\n\n"
                "Il codice scade in 10 minuti.".format(code))
        elif text.startswith("/stop") or text.startswith("/disconnect"):
            # Disconnetti il chat_id da qualsiasi cliente/distributore
            removed = []
            for key in ("clients", "distributors"):
                for k, v in list(telegram_links.get(key, {}).items()):
                    if v.get("chat_id") == chat_id:
                        del telegram_links[key][k]
                        removed.append(k)
            save_telegram_links()
            if removed:
                send_telegram(chat_id, "✅ Disconnesso. Non riceverai più notifiche.")
            else:
                send_telegram(chat_id, "Non risulti collegato a nessun account.")
        elif text.startswith("/help"):
            # Help personalizzato in base al ruolo
            role, _ = _find_role_by_chat(chat_id)
            if role == "client":
                send_telegram(chat_id,
                    "<b>🤖 Comandi cliente:</b>\n\n"
                    "/stato - Stato live (balance, equity, posizioni)\n"
                    "/stop - Disconnetti notifiche\n"
                    "/help - Mostra questo messaggio\n\n"
                    "<i>Riceverai automaticamente:\n"
                    "• Notifica ad ogni chiusura operazione (TP/SL)\n"
                    "• Resoconto giornaliero a fine sessione</i>")
            elif role == "distributor":
                send_telegram(chat_id,
                    "<b>🤖 Comandi distributore:</b>\n\n"
                    "/clienti - Lista tuoi clienti con stato\n"
                    "/stop - Disconnetti notifiche\n"
                    "/help - Mostra questo messaggio")
            else:
                send_telegram(chat_id,
                    "<b>Comandi disponibili:</b>\n\n"
                    "/start - Genera codice di collegamento\n"
                    "/help - Mostra questo messaggio\n\n"
                    "<i>Per usare gli altri comandi devi prima collegare il tuo account "
                    "con /start.</i>")
        elif text.startswith("/oggi") or text.startswith("/storico") or text.startswith("/mese"):
            # Comandi disattivati in v4.8.7 — sostituiti da notifiche automatiche
            send_telegram(chat_id,
                "ℹ Comando non più disponibile.\n\n"
                "Le statistiche arrivano in automatico:\n"
                "• Notifica ad ogni chiusura operazione\n"
                "• Resoconto a fine giornata\n\n"
                "Usa /stato per vedere il live, /help per la lista comandi.")
        elif text.startswith("/stato"):
            role, target_id = _find_role_by_chat(chat_id)
            if role == "client":
                send_telegram(chat_id, _tg_stato_client(target_id))
            else:
                send_telegram(chat_id, "❌ Comando disponibile solo per clienti collegati.")
        elif text.startswith("/clienti"):
            role, target_id = _find_role_by_chat(chat_id)
            if role == "distributor":
                send_telegram(chat_id, _tg_clienti_dist(target_id))
            else:
                send_telegram(chat_id, "❌ Comando disponibile solo per distributori.")
        else:
            send_telegram(chat_id,
                "Comando non riconosciuto. Usa /help per la lista comandi.")
        return jsonify({"ok": True})
    except Exception as e:
        print("telegram_webhook error:", e)
        return jsonify({"ok": False}), 200  # 200 per non far re-inviare

@app.route("/api/telegram/link", methods=["POST"])
def telegram_link():
    """Cliente o distributore inserisce il codice di 6 cifre dal pannello."""
    data = request.get_json(force=True, silent=True) or {}
    code = str(data.get("code", "")).strip()
    role = data.get("role", "client")  # "client" o "distributor"
    target_id = (data.get("id") or "").strip()
    if not code or not target_id:
        return jsonify({"ok": False, "error": "Dati mancanti"}), 400
    # Auth: cliente verifica con sua password, distributore con sua
    if role == "client":
        cpw = request.headers.get("X-Client-Password", "").strip().lower()
        if target_id not in clients:
            return jsonify({"ok": False, "error": "Cliente non trovato"}), 404
        stored = clients[target_id].get("client_password", "").strip().lower()
        if not stored or not _check_pw(cpw, stored):
            return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    elif role == "distributor":
        dpw = request.headers.get("X-Distributor-Password", "")
        if target_id not in distributors or distributors[target_id].get("password") != dpw:
            return jsonify({"ok": False, "error": "Non autorizzato"}), 403
    else:
        return jsonify({"ok": False, "error": "Ruolo non valido"}), 400
    # Trova il codice pending
    pending = telegram_links.get("pending", {})
    info = pending.get(code)
    if not info:
        return jsonify({"ok": False, "error": "Codice non valido o scaduto"}), 404
    # Verifica scadenza (10 min)
    if time.time() - info.get("ts", 0) > 600:
        del pending[code]
        save_telegram_links()
        return jsonify({"ok": False, "error": "Codice scaduto, generane uno nuovo con /start"}), 410
    # Collega
    section = "clients" if role == "client" else "distributors"
    telegram_links[section][target_id] = {
        "chat_id":  info["chat_id"],
        "username": info.get("username", ""),
        "linked_at": time.time(),
    }
    del pending[code]
    save_telegram_links()
    # Conferma all'utente Telegram
    label = "cliente" if role == "client" else "distributore"
    send_telegram(info["chat_id"],
        "✅ <b>Account collegato!</b>\n\n"
        "Riceverai notifiche per il {} <b>{}</b> ogni volta che "
        "un'operazione si chiude per Take Profit o Stop Loss.\n\n"
        "Per disconnetterti scrivi /stop.".format(label, target_id))
    return jsonify({"ok": True, "username": info.get("username", "")})

@app.route("/api/telegram/status/<role>/<target_id>")
def telegram_status(role, target_id):
    """Verifica se un cliente/distributore è collegato a Telegram."""
    section = "clients" if role == "client" else "distributors"
    info = telegram_links.get(section, {}).get(target_id, {})
    return jsonify({
        "linked":   bool(info),
        "username": info.get("username", "") if info else "",
    })

@app.route("/api/telegram/unlink", methods=["POST"])
def telegram_unlink():
    """Disconnetti Telegram dal pannello (senza usare il bot)."""
    data = request.get_json(force=True, silent=True) or {}
    role = data.get("role", "client")
    target_id = (data.get("id") or "").strip()
    if role == "client":
        cpw = request.headers.get("X-Client-Password", "").strip().lower()
        if target_id not in clients:
            return jsonify({"ok": False}), 403
        stored = clients[target_id].get("client_password", "").strip().lower()
        if not stored or not _check_pw(cpw, stored):
            return jsonify({"ok": False}), 403
    elif role == "distributor":
        dpw = request.headers.get("X-Distributor-Password", "")
        if target_id not in distributors or distributors[target_id].get("password") != dpw:
            return jsonify({"ok": False}), 403
    section = "clients" if role == "client" else "distributors"
    info = telegram_links.get(section, {}).pop(target_id, None)
    save_telegram_links()
    if info and info.get("chat_id"):
        send_telegram(info["chat_id"],
            "🔌 Account scollegato dal pannello. Non riceverai più notifiche.")
    return jsonify({"ok": True})

@app.route("/api/notify_trade", methods=["POST"])
def notify_trade():
    """L'app desktop chiama questo endpoint quando si chiude una operazione
    per TP o SL. Invia notifica Telegram al cliente e al suo distributore."""
    data = request.get_json(force=True, silent=True) or {}
    cid       = (data.get("client_id") or "").strip()
    prop_firm = data.get("prop_firm", "?")
    symbol    = data.get("symbol", "?")
    profit    = float(data.get("profit", 0) or 0)
    lots      = float(data.get("lots", 0) or 0)
    reason    = (data.get("reason") or "").upper()  # "TP" o "SL"
    pnl_day   = float(data.get("pnl_day", 0) or 0)
    if not cid or cid not in clients:
        return jsonify({"ok": False, "error": "Cliente non valido"}), 400
    if reason not in ("TP", "SL"):
        return jsonify({"ok": False, "error": "Reason invalido"}), 400
    # Costruisci messaggio
    if profit >= 0:
        icon  = "✅"
        title = "Operazione chiusa in profitto"
    else:
        icon  = "🔴"
        title = "Operazione chiusa in perdita"
    sign = "+" if profit >= 0 else ""
    lines = [
        "{} <b>{}</b>".format(icon, title),
        "─────────────",
        "Scheda: <b>{}</b>".format(prop_firm),
        "Simbolo: {}".format(symbol),
        "P&amp;L: <b>{}{:.2f} $</b>".format(sign, profit),
        "Lotti: {:.2f}".format(lots),
        "",
        "📊 P&amp;L oggi: {}{:.2f} $".format("+" if pnl_day >= 0 else "", pnl_day),
    ]
    msg = "\n".join(lines)
    sent_to = []
    # Notifica cliente
    cli_info = telegram_links.get("clients", {}).get(cid)
    if cli_info and cli_info.get("chat_id"):
        if send_telegram(cli_info["chat_id"], msg):
            sent_to.append("client")
            
    # Notifica Admin se spuntata
    if clients[cid].get("notify_admin_telegram"):
        for admin_cid, admin_c in clients.items():
            if admin_c.get("is_admin"):
                admin_tg = telegram_links.get("clients", {}).get(admin_cid)
                if admin_tg and admin_tg.get("chat_id"):
                    admin_msg = "👤 <b>[ADMIN] Cliente: {}</b>\n\n{}".format(clients[cid].get("client_name", cid), msg)
                    if send_telegram(admin_tg["chat_id"], admin_msg):
                        if "admin" not in sent_to:
                            sent_to.append("admin")
                            
    # Notifica Distributore se spuntata
    if clients[cid].get("notify_distributor_telegram"):
        for did, dist in distributors.items():
            if cid in dist.get("clients", []):
                dist_tg = telegram_links.get("distributors", {}).get(did)
                if dist_tg and dist_tg.get("chat_id"):
                    dist_msg = "👤 <b>[DISTRIBUTOR] Cliente: {}</b>\n\n{}".format(clients[cid].get("client_name", cid), msg)
                    if send_telegram(dist_tg["chat_id"], dist_msg):
                        if "distributor" not in sent_to:
                            sent_to.append("distributor")
                            
    return jsonify({"ok": True, "sent_to": sent_to})

@app.route("/api/notify_daily_recap", methods=["POST"])
def notify_daily_recap():
    """L'app desktop chiama questo endpoint al reset 22:00 UTC per inviare
    il resoconto della giornata: operazioni positive/negative + saldi live."""
    data = request.get_json(force=True, silent=True) or {}
    cid        = (data.get("client_id") or "").strip()
    prop_firm  = data.get("prop_firm", "?")
    date_s     = data.get("date", "")
    ops_pos    = int(data.get("ops_pos", 0) or 0)
    ops_neg    = int(data.get("ops_neg", 0) or 0)
    profit_pos = float(data.get("profit_pos", 0) or 0)
    loss_neg   = float(data.get("loss_neg", 0) or 0)   # negativo
    net        = float(data.get("net", 0) or 0)
    prop_bal   = float(data.get("prop_bal", 0) or 0)
    hedge_bal  = float(data.get("hedge_bal", 0) or 0)
    if not cid or cid not in clients:
        return jsonify({"ok": False, "error": "Cliente non valido"}), 400
    icon = "✅" if net >= 0 else "🔴"
    net_sign = "+" if net >= 0 else ""
    pos_sign = "+" if profit_pos >= 0 else ""
    lines = [
        "{} <b>RESOCONTO GIORNATA</b>".format(icon),
        "─────────────",
        "Scheda: <b>{}</b>".format(prop_firm),
        "Data: {}".format(date_s),
        "",
        "📊 <b>Operazioni</b>",
        "   ✅ Positive: <b>{}</b>  ({}{:.2f} $)".format(ops_pos, pos_sign, profit_pos),
        "   🔴 Negative: <b>{}</b>  ({:.2f} $)".format(ops_neg, loss_neg),
        "   ─────────",
        "   <b>Netto: {}{:.2f} $</b>".format(net_sign, net),
        "",
        "💰 <b>Saldi a fine giornata</b>",
        "   PROP:  ${:,.2f}".format(prop_bal),
        "   HEDGE: ${:,.2f}".format(hedge_bal),
    ]
    msg = "\n".join(lines)
    sent_to = []
    # Notifica cliente
    cli_info = telegram_links.get("clients", {}).get(cid)
    if cli_info and cli_info.get("chat_id"):
        if send_telegram(cli_info["chat_id"], msg):
            sent_to.append("client")
            
    # Notifica Admin se spuntata
    if clients[cid].get("notify_admin_telegram"):
        for admin_cid, admin_c in clients.items():
            if admin_c.get("is_admin"):
                admin_tg = telegram_links.get("clients", {}).get(admin_cid)
                if admin_tg and admin_tg.get("chat_id"):
                    admin_msg = "👤 <b>[ADMIN] Cliente: {}</b>\n\n{}".format(clients[cid].get("client_name", cid), msg)
                    if send_telegram(admin_tg["chat_id"], admin_msg):
                        if "admin" not in sent_to:
                            sent_to.append("admin")
                            
    # Notifica Distributore se spuntata
    if clients[cid].get("notify_distributor_telegram"):
        for did, dist in distributors.items():
            if cid in dist.get("clients", []):
                dist_tg = telegram_links.get("distributors", {}).get(did)
                if dist_tg and dist_tg.get("chat_id"):
                    dist_msg = "👤 <b>[DISTRIBUTOR] Cliente: {}</b>\n\n{}".format(clients[cid].get("client_name", cid), msg)
                    if send_telegram(dist_tg["chat_id"], dist_msg):
                        if "distributor" not in sent_to:
                            sent_to.append("distributor")
                            
    return jsonify({"ok": True, "sent_to": sent_to})


@app.route("/api/version")
def api_version():
    version_file = os.path.join(BASE_DIR, "version.txt")
    try:
        with open(version_file) as f:
            v = f.read().strip()
    except:
        v = "1.0.0"
    return jsonify({"version": v})

@app.route("/api/toggle_exe", methods=["POST"])
def toggle_exe():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    exe_path    = os.path.join(EXE_DIR, "ARBEXA.zip")
    hidden_path = os.path.join(EXE_DIR, "ARBEXA.zip.hidden")
    if os.path.exists(exe_path):
        os.rename(exe_path, hidden_path)
        return jsonify({"ok": True, "visible": False})
    elif os.path.exists(hidden_path):
        os.rename(hidden_path, exe_path)
        return jsonify({"ok": True, "visible": True})
    return jsonify({"ok": False, "error": "File non trovato"})

@app.route("/api/exe_status")
def exe_status():
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    exe_path    = os.path.join(EXE_DIR, "ARBEXA.zip")
    hidden_path = os.path.join(EXE_DIR, "ARBEXA.zip.hidden")
    visible = os.path.exists(exe_path)
    exists  = visible or os.path.exists(hidden_path)
    return jsonify({"ok": True, "visible": visible, "exists": exists, "download_count": _download_count})

@app.route("/api/reset_download_count", methods=["POST"])
def reset_download_count():
    global _download_count
    pw = request.headers.get("X-Admin-Password")
    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False}), 403
    _download_count = 0
    _save_download_count(0)
    return jsonify({"ok": True})


@app.route("/static/logo.jpg")
def serve_logo():
    with open(os.path.join(BASE_DIR, "static", "logo.jpg"), "rb") as f:
        from flask import Response
        return Response(f.read(), mimetype="image/jpeg")

@app.route("/")
def home():
    with open(os.path.join(BASE_DIR, "index.html"), encoding="utf-8") as f:
        return f.read()

@app.route("/download")
def download_page():
    with open(os.path.join(BASE_DIR, "download.html"), encoding="utf-8") as f:
        return f.read()

@app.route("/termini")
def termini_page():
    with open(os.path.join(BASE_DIR, "termini.html"), encoding="utf-8") as f:
        return f.read()

@app.route("/arbexa")
def arbexa_page():
    with open(os.path.join(BASE_DIR, "arbexa.html"), encoding="utf-8") as f:
        return f.read()

@app.route("/istruzioni")
def istruzioni_page():
    with open(os.path.join(BASE_DIR, "istruzioni.html"), encoding="utf-8") as f:
        return f.read()

@app.route("/static/<path:filename>")
def custom_static(filename):
    return send_from_directory(os.path.join(BASE_DIR, "static"), filename)

@app.route("/admin")
def admin():
    html_file = os.path.join(BASE_DIR, "panel.html")
    with open(html_file) as f:
        return render_template_string(f.read())

@app.route("/download/<filename>")
def download_file(filename):
    global _download_count
    _download_count += 1
    _save_download_count(_download_count)
    return send_from_directory(EXE_DIR, filename, as_attachment=True)

@app.route("/privacy")
def privacy():
    return render_template_string("""<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">
<title>Privacy - Arbexa</title></head><body style="background:#060f1e;color:#ccc;font-family:sans-serif;padding:40px;max-width:800px;margin:0 auto;">
<h1 style="color:#d4af37;">Informativa sulla Privacy</h1>
<p>Raccogliamo esclusivamente: ID Cliente univoco, stato della licenza, IP di connessione.</p>
<p>Contatti: <a href="mailto:info@sniperpro.online" style="color:#d4af37;">info@sniperpro.online</a></p>
</body></html>""")

@app.route("/contatti")
def contatti():
    return render_template_string("""<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">
<title>Contatti - Arbexa</title></head><body style="background:#060f1e;color:#ccc;font-family:sans-serif;padding:40px;max-width:800px;margin:0 auto;">
<h1 style="color:#d4af37;">Contattaci</h1>
<p>Email: <a href="mailto:info@sniperpro.online" style="color:#d4af37;">info@sniperpro.online</a></p>
<p>Sito: <a href="https://sniperpro.online" style="color:#d4af37;">sniperpro.online</a></p>
</body></html>""")

@app.route("/prezzi")
def prezzi():
    return redirect("/contatti")

@app.route("/gestionale")
def gestionale():
    pw = request.args.get("pw", "")
    if pw != GESTIONALE_PW:
        return """<!DOCTYPE html><html><head><meta charset="utf-8">
<style>body{background:#111;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-family:sans-serif;}
.box{background:#1a1a1a;padding:30px;border-radius:12px;text-align:center;}
input{padding:10px;border-radius:8px;border:1px solid #444;background:#222;color:#fff;width:200px;font-size:15px;margin:10px 0;}
button{padding:10px 24px;border-radius:8px;border:none;background:#d4af37;color:#000;font-weight:700;cursor:pointer;font-size:15px;}
p{color:#aaa;margin-bottom:16px;}</style></head>
<body><div class="box"><p>Accesso riservato</p>
<input type="password" id="pw" placeholder="Password"><br>
<button onclick="location.href='/gestionale?pw='+document.getElementById('pw').value">Entra</button>
</div></body></html>""", 401
    try:
        with open(os.path.join(BASE_DIR, "gestionale.html"), encoding="utf-8") as f:
            return f.read()
    except:
        return "File non trovato", 404

@app.route("/menu")
def menu():
    try:
        with open(os.path.join(BASE_DIR, "menu.html"), encoding="utf-8") as f:
            return f.read()
    except:
        return "File non trovato", 404

# ── GESTIONALE API ────────────────────────────────────────────────
GESTIONALE_STATE_FILE = os.path.join(BASE_DIR, "gestionale_stato.json")
# GESTIONALE_PW è definita in testa al file (variabile d'ambiente ARBEXA_GESTIONALE_PW)

def _load_gestionale_stato():
    try:
        with open(GESTIONALE_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except:
        return None

def _save_gestionale_stato(data):
    with open(GESTIONALE_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

@app.route("/gestionale/stato", methods=["GET"])
def gestionale_get_stato():
    pw = request.headers.get("X-Gestionale-Password", "")
    if pw != GESTIONALE_PW:
        return jsonify({"ok": False}), 403
    stato = _load_gestionale_stato()
    return jsonify({"ok": True, "stato": stato})

@app.route("/gestionale/stato", methods=["POST"])
def gestionale_set_stato():
    pw = request.headers.get("X-Gestionale-Password", "")
    if pw != GESTIONALE_PW:
        return jsonify({"ok": False}), 403
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"ok": False, "error": "Dati mancanti"}), 400
    _save_gestionale_stato(data)
    # Notifica tutti i dispositivi connessi
    socketio.emit("gestionale_update", data)
    return jsonify({"ok": True})

@app.route("/gestionale/stato", methods=["DELETE"])
def gestionale_delete_stato():
    pw = request.headers.get("X-Gestionale-Password", "")
    if pw != GESTIONALE_PW:
        return jsonify({"ok": False}), 403
    try:
        os.remove(GESTIONALE_STATE_FILE)
    except:
        pass
    socketio.emit("gestionale_update", None)
    return jsonify({"ok": True})


if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5000)
