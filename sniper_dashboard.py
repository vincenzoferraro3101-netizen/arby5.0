import customtkinter as ctk
import MetaTrader5 as mt5
from mt5_utils import (
    trova_simbolo, calcola_lotto, get_account_info,
    invia_ordine, chiudi_posizioni, get_closed_trades,
    get_daily_pnl, get_daily_closed_pnl, get_spread
)

def _conta_posizioni_live(terminal_path):
    """Conta le posizioni aperte direttamente da MT5 in tempo reale."""
    try:
        info = get_account_info(terminal_path)
        if info and isinstance(info, dict):
            return int(info.get("positions", 0))
    except:
        pass
    return -1  # -1 = impossibile leggere
import threading
import json
import os
import time
import random
import string
import base64
import hashlib
import subprocess
from datetime import date, datetime, timedelta
from tkinter import messagebox, filedialog, colorchooser
try:
    import urllib.request
    import urllib.error
except:
    pass

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

# Font nitido — usa Calibri come fallback più leggibile di Segoe UI a bassa risoluzione
import tkinter.font as _tkfont
_FONT_MAIN = "Calibri"

# === DPI AWARENESS (Windows) ===
# Senza questo, su monitor 4K o con scaling Windows >100% l'app appare minuscola
# perché Windows la considera "DPI unaware" e la mostra a 96 DPI virtuali.
import sys as _sys
if _sys.platform == "win32":
    try:
        from ctypes import windll
        # Per-Monitor DPI Awareness V2 (Windows 10 1607+) - ideale per multi-monitor
        try:
            windll.user32.SetProcessDpiAwarenessContext(-4)  # PER_MONITOR_AWARE_V2
        except:
            try:
                windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE (Win 8.1+)
            except:
                windll.user32.SetProcessDPIAware()  # System-wide (Win Vista+)
    except:
        pass

# Calcola scaling UI in base al DPI reale dello schermo
import tkinter as _tk
_tmp_root = _tk.Tk()
_tmp_root.withdraw()
try:
    _DPI = _tmp_root.winfo_fpixels("1i")
    _SCREEN_W = _tmp_root.winfo_screenwidth()
    _SCREEN_H = _tmp_root.winfo_screenheight()
except:
    _DPI = 96
    _SCREEN_W = 1920
    _SCREEN_H = 1080
_tmp_root.destroy()
del _tmp_root

# NB: scaling fisso a 1.0 — l'app è ora completamente DPI-aware grazie a
# SetProcessDpiAwarenessContext sopra, quindi Windows passa il DPI reale a Tk.
# Non applichiamo set_widget_scaling perché causa "scatti" durante il resize.
_SCALE = 1.0

def _fs(size):
    """Compatibilità: ritorna size invariato (scaling gestito da DPI awareness)."""
    return size

# ═══════════════════════════════════════════════════════════════════
# COSTANTI — importate dal modulo centralizzato core/constants.py
# ═══════════════════════════════════════════════════════════════════
from core.constants import (
    BG, CARD, CARD2, BORDER, GREEN, RED, GOLD, BLUE, WHITE, GRAY, LIGHT,
    FONT_MAIN as _FONT_MAIN, BASE_DIR, DATA_DIR,
    CONFIG_FILE, LICENSE_FILE, TRADING_DAYS_FILE, TRADES_HISTORY_FILE,
    HEDGE_CUM_FILE, HEDGE_LOTS_FILE, NOTIFIED_FILE, FIRST_RUN_FILE,
)

import sys

def _load_notified():
    """Set degli position_id già notificati (persiste tra riavvii)."""
    try:
        with open(NOTIFIED_FILE, "r") as f:
            return set(json.load(f))
    except:
        return set()

def _save_notified(notified_set):
    try:
        # Mantiene solo gli ultimi 1000 ID per evitare crescita illimitata
        ids = sorted(notified_set)[-1000:]
        with open(NOTIFIED_FILE, "w") as f:
            json.dump(ids, f)
    except: pass

def _is_first_run():
    """True se ARBEXA non è mai stato avviato in modo completo (senza schede o nessun marker)."""
    return not os.path.exists(FIRST_RUN_FILE)

def _mark_first_run_done():
    """Crea il marker per non ripetere l'onboarding."""
    try:
        with open(FIRST_RUN_FILE, "w") as f:
            json.dump({"done": True, "ts": datetime.now().isoformat()}, f)
    except: pass

# ── LOGGING ───────────────────────────────────────────────────────
import logging as _logging
from datetime import datetime as _dt_log

def _get_log_file():
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"arbexa_{_dt_log.now().strftime('%Y-%m-%d')}.txt")

def _setup_logger():
    logger = _logging.getLogger("arbexa")
    logger.setLevel(_logging.DEBUG)
    if not logger.handlers:
        handler = _logging.FileHandler(_get_log_file(), encoding="utf-8")
        handler.setFormatter(_logging.Formatter(
            "[%(asctime)s] %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(handler)
    return logger

_logger = _setup_logger()

def log(msg, level="INFO"):
    """Log a message. Level: INFO, WARNING, ERROR, DEBUG"""
    # Rotate log file se è cambiato giorno
    global _logger
    try:
        log_path = _get_log_file()
        if not any(h.baseFilename == log_path for h in _logger.handlers if hasattr(h, 'baseFilename')):
            _logger.handlers.clear()
            _logger = _setup_logger()
        getattr(_logger, level.lower(), _logger.info)(msg)
    except:
        pass

# === CACHE JSON IN MEMORIA ===
# Riduce I/O su disco del 90% — importante quando il loop refresh fa molte letture
# Le cache si invalidano automaticamente al save (sono sempre coerenti).
_hedge_cum_cache = None
_hedge_lots_cache = None
_trades_history_cache = None
_trading_days_cache = None

def _load_hedge_cum_file():
    global _hedge_cum_cache
    if _hedge_cum_cache is not None:
        return _hedge_cum_cache
    try:
        with open(HEDGE_CUM_FILE, "r") as f:
            _hedge_cum_cache = json.load(f)
    except:
        _hedge_cum_cache = {}
    return _hedge_cum_cache

def _load_hedge_cum(trader_id):
    data = _load_hedge_cum_file()
    return float(data.get(trader_id, 0.0))

def _save_hedge_cum(trader_id, value):
    global _hedge_cum_cache
    try:
        data = _load_hedge_cum_file()
        data[trader_id] = value
        _hedge_cum_cache = data
        with open(HEDGE_CUM_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except:
        pass

def _load_hedge_lots_file():
    global _hedge_lots_cache
    if _hedge_lots_cache is not None:
        return _hedge_lots_cache
    try:
        with open(HEDGE_LOTS_FILE, "r") as f:
            _hedge_lots_cache = json.load(f)
    except:
        _hedge_lots_cache = {}
    return _hedge_lots_cache

def _load_hedge_lots(trader_id):
    """Carica il cumulativo lotti hedge per il trader."""
    data = _load_hedge_lots_file()
    return float(data.get(trader_id, 0.0))

def _save_hedge_lots(trader_id, value):
    """Salva il cumulativo lotti hedge per il trader."""
    global _hedge_lots_cache
    try:
        data = _load_hedge_lots_file()
        data[trader_id] = value
        _hedge_lots_cache = data
        with open(HEDGE_LOTS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except:
        pass

def _load_trades_history_file():
    global _trades_history_cache
    if _trades_history_cache is not None:
        return _trades_history_cache
    try:
        with open(TRADES_HISTORY_FILE, "r") as f:
            _trades_history_cache = json.load(f)
    except:
        _trades_history_cache = {}
    return _trades_history_cache

def _load_trades_history(trader_id):
    data = _load_trades_history_file()
    return data.get(trader_id, [])

def _save_trades_history(trader_id, trades):
    global _trades_history_cache
    try:
        data = _load_trades_history_file()
        data[trader_id] = trades[-500:]  # Mantieni max ultimi 500 trade
        _trades_history_cache = data
        with open(TRADES_HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except:
        pass

def _load_trading_days_file():
    global _trading_days_cache
    if _trading_days_cache is not None:
        return _trading_days_cache
    try:
        with open(TRADING_DAYS_FILE, "r") as f:
            _trading_days_cache = json.load(f)
    except:
        _trading_days_cache = {}
    return _trading_days_cache

def _load_trading_days(trader_id):
    """Carica i giorni di trading persistenti per un trader."""
    data = _load_trading_days_file()
    return set(data.get(trader_id, []))

def _save_trading_days(trader_id, days_set):
    """Salva i giorni di trading persistenti."""
    global _trading_days_cache
    try:
        data = _load_trading_days_file()
        data[trader_id] = sorted(list(days_set))
        _trading_days_cache = data
        with open(TRADING_DAYS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except:
        pass

import ctypes as _ctypes
import uuid
import socket

# ------------------------------------------------
#  PREVENZIONE SOSPENSIONE WINDOWS
# ------------------------------------------------
_ES_CONTINUOUS       = 0x80000000
_ES_SYSTEM_REQUIRED  = 0x00000001

def _prevent_sleep():
    try:
        _ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
    except: pass

def _allow_sleep():
    try:
        _ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except: pass

# Attiva subito
_prevent_sleep()

_fingerprint_cache = None  # calcolato una sola volta all'avvio

def _get_fingerprint():
    """Genera fingerprint robusto: MAC + hostname + volume serial + BIOS UUID.
    Il risultato viene calcolato una volta sola e cachato — i subprocess wmic
    possono bloccarsi se chiamati ripetutamente ad ogni heartbeat."""
    global _fingerprint_cache
    if _fingerprint_cache is not None:
        return _fingerprint_cache

    import subprocess
    parts = []
    try:
        mac = ':'.join(['{:02x}'.format((uuid.getnode() >> i) & 0xff)
                        for i in range(0, 8*6, 8)][::-1])
        parts.append(mac)
    except: pass
    try:
        parts.append(socket.gethostname())
    except: pass
    try:
        # Volume serial disco C — timeout 10s per evitare blocchi
        result = subprocess.check_output(
            "vol C:", shell=True, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10).decode(errors="ignore")
        for line in result.splitlines():
            if "Serial" in line or "numero di serie" in line.lower():
                parts.append(line.strip().split()[-1])
                break
    except: pass
    try:
        # BIOS UUID via wmic — timeout 10s
        result = subprocess.check_output(
            "wmic csproduct get UUID", shell=True, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10).decode(errors="ignore")
        lines = [l.strip() for l in result.splitlines() if l.strip() and l.strip() != "UUID"]
        if lines: parts.append(lines[0])
    except: pass
    try:
        # Rileva VM — timeout 10s
        result = subprocess.check_output(
            "wmic computersystem get model", shell=True, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10).decode(errors="ignore").lower()
        if any(x in result for x in ["vmware", "virtualbox", "virtual machine", "hyper-v", "qemu"]):
            parts.append("VM")
    except: pass
    if not parts:
        _fingerprint_cache = "unknown"
    else:
        raw = "|".join(parts)
        _fingerprint_cache = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return _fingerprint_cache

# ------------------------------------------------
#  SISTEMA LICENZA
# ------------------------------------------------

EXPIRY_DAYS = 0          # nessun periodo di prova — richiede attivazione
# _LKEY rimossa — validazione solo server-side

# Chiave cifratura license.dat (derivata da costanti locali)
_LIC_KEY = hashlib.sha256(b"arbexa_lic_2026_vins").digest()

def _lic_encrypt(data: str) -> str:
    """XOR + base64 per cifrare license.dat."""
    key = _LIC_KEY
    raw = data.encode("utf-8")
    enc = bytes([raw[i] ^ key[i % len(key)] for i in range(len(raw))])
    return base64.b64encode(enc).decode()

def _lic_decrypt(data: str) -> str:
    """Decifra license.dat."""
    key = _LIC_KEY
    raw = base64.b64decode(data.encode())
    dec = bytes([raw[i] ^ key[i % len(key)] for i in range(len(raw))])
    return dec.decode("utf-8")

# ------------------------------------------------
#  SERVER REMOTO (lascia vuoto per disabilitare)
# ------------------------------------------------
SERVER_URL  = "https://sniperpro.online"  # <-- dominio attivo (migrare a arbexa.pro quando pronto)

# Stato globale condiviso per heartbeat
_heartbeat_data = {"traders": [], "license_days": 0, "client_id": ""}
_heartbeat_lock = threading.Lock()
_pending_command  = None
_pending_params   = None
_pending_messages     = []   # messaggi ricevuti dal server
_new_messages         = 0    # contatore messaggi non letti
_msg_counter          = {"n": 0}  # container mutabile condiviso tra thread
_server_license_days  = None # giorni licenza dal server (fonte di verità)
_pending_trader_index = None  # indice scheda per comando remoto

def _send_heartbeat():
    """Invia dati al server ogni 5 secondi e riceve eventuali comandi."""
    global _pending_command, _pending_params, _new_messages
    if not SERVER_URL:
        return
    while True:
        try:
            with _heartbeat_lock:
                payload = json.dumps({
                    "client_id":       _heartbeat_data["client_id"],
                    "license_days":    _heartbeat_data["license_days"],
                    "traders":         _heartbeat_data["traders"],
                    "params_received": _heartbeat_data.get("params_received", False),
                    "fingerprint":     _get_fingerprint(),
                }).encode("utf-8")
                # Reset flag dopo invio
                _heartbeat_data["params_received"] = False
            req = urllib.request.Request(
                SERVER_URL + "/api/heartbeat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if data.get("command"):
                    _pending_command = data["command"]
                    globals()["_pending_trader_index"] = data.get("trader_index")
                if data.get("params"):
                    _pending_params = data["params"]
                if data.get("license_expiry"):
                    update_local_expiry(data["license_expiry"])
                # Aggiorna giorni dal server (fonte di verità)
                if data.get("license_days") is not None:
                    globals()["_server_license_days"] = int(data["license_days"])
                if data.get("blocked"):
                    update_local_expiry("2000-01-01")
                lic = _load_license()
                lic["last_server_contact"] = date.today().isoformat()
                _save_license(lic)
                # Messaggi dal server
                if data.get("messages") is not None:
                    msgs = data["messages"]
                    _pending_messages[:] = msgs
                    # Conta messaggi non ancora visti dall'exe
                    recent = [m for m in msgs
                              if not m.get("read_exe")
                              and m.get("from", "admin") != "client"]
                    _new_messages = len(recent)
                    _msg_counter["n"] = len(recent)
        except Exception as e:
            pass
        # Heartbeat fisso ogni 5s (più semplice e prevedibile)
        time.sleep(5)

def start_heartbeat(client_id, days):
    """Avvia il thread heartbeat."""
    with _heartbeat_lock:
        _heartbeat_data["client_id"]    = client_id
        _heartbeat_data["license_days"] = days
    t = threading.Thread(target=_send_heartbeat, daemon=True)
    t.start()

def update_heartbeat_traders(traders_data):
    """Aggiorna i dati traders inviati al server."""
    with _heartbeat_lock:
        _heartbeat_data["traders"] = traders_data

def _load_license():
    try:
        with open(LICENSE_FILE, "r") as f:
            raw = f.read().strip()
        # Prova prima come JSON cifrato, poi come JSON plain (retrocompatibilità)
        try:
            return json.loads(_lic_decrypt(raw))
        except:
            data = json.loads(raw)
            # Era in chiaro — riscrivilo cifrato subito
            _save_license(data)
            return data
    except:
        return {}

def _save_license(data):
    with open(LICENSE_FILE, "w") as f:
        f.write(_lic_encrypt(json.dumps(data)))

def get_client_id():
    """ID univoco per ogni installazione."""
    lic = _load_license()
    if "client_id" not in lic:
        cid = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=12))
        lic["client_id"] = cid
        _save_license(lic)
    return lic["client_id"]

def get_days_left():
    """Legge scadenza dal file locale (cache). La verità è sul server.
    Se non esiste licenza locale, ritorna 0: il server al primo heartbeat
    assegnerà eventualmente un trial gratuito (logica server-side)."""
    lic = _load_license()
    if "expiry" not in lic:
        # Nessuna licenza locale — il server deciderà se concedere il trial
        return 0
    try:
        expiry = datetime.strptime(lic["expiry"], "%Y-%m-%d").date()
        days = (expiry - date.today()).days
        # Blocco offline: se non contatta il server da 7+ giorni → blocca
        last_contact = lic.get("last_server_contact", "")
        if last_contact:
            offline_days = (date.today() - datetime.strptime(last_contact, "%Y-%m-%d").date()).days
            if offline_days >= 7:
                return -1
        return days
    except:
        return -1

def update_local_expiry(expiry_str):
    """Aggiorna la scadenza locale con il valore dal server."""
    try:
        lic = _load_license()
        lic["expiry"] = expiry_str
        _save_license(lic)
    except:
        pass

def apply_renewal_code(code):
    """Invia il codice al server per validazione — _LKEY non è nel client."""
    try:
        cid     = get_client_id()
        payload = json.dumps({"client_id": cid, "code": code.strip()}).encode("utf-8")
        req     = urllib.request.Request(
            SERVER_URL + "/api/activate_license",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("ok"):
                # Aggiorna cache locale con la scadenza dal server
                if data.get("expiry"):
                    update_local_expiry(data["expiry"])
                return True, data.get("message", "Licenza attivata!")
            else:
                return False, data.get("error", "Codice non valido.")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", "Errore server")
        except:
            err = "Errore server ({})".format(e.code)
        return False, err
    except Exception as e:
        return False, "Errore: {}".format(str(e))

# generate_renewal_code rimossa — generazione solo server-side

PROP_COLORS = {
    "FTMO":             "#1A6B3A",
    "Prop Number One":  "#1A3A6B",
    "MyForexFunds":     "#6B1A1A",
    "The5ers":          "#4A1A6B",
    "FundedNext":       "#6B4A1A",
    "Altro":            "#1A4A4A",
}

APP_VERSION = "5.0.0"

# Card size dinamica in base alla risoluzione schermo
if _SCREEN_W <= 1366:
    CARD_W = 340
    CARD_H = 555
elif _SCREEN_W >= 3000:
    CARD_W = 420
    CARD_H = 620
else:
    CARD_W = 400
    CARD_H = 590

# ------------------------------------------------
#  CONFIG
# ------------------------------------------------

def has_accepted_terms():
    lic = _load_license()
    return lic.get("terms_accepted", False)

def set_terms_accepted():
    lic = _load_license()
    lic["terms_accepted"] = True
    _save_license(lic)

# ------------------------------------------------
#  FINESTRA ACCETTAZIONE TERMINI
# ------------------------------------------------

TERMS_TEXT = """CONTRATTO DI LICENZA D'USO - ARBEXA

Leggere attentamente prima di utilizzare il software.

1. OGGETTO
Arbexa è uno strumento tecnico di automazione per operatività su piattaforme di trading. Non costituisce consulenza finanziaria, gestione patrimoniale o servizio di investimento.

2. LIMITAZIONI DI RESPONSABILITÀ
Il software è fornito "così com'è" senza garanzie di alcun tipo. Il Licenziante non è responsabile per perdite finanziarie derivanti dall'utilizzo del software. Il trading comporta rischi significativi di perdita del capitale.

3. DISCLAIMER FINANZIARIO
Le performance passate non sono indicative di risultati futuri. L'utente opera in piena autonomia e responsabilità. Il software non gestisce fondi per conto terzi.

4. PROPRIETÀ INTELLETTUALE
Il software è protetto dalle leggi sul diritto d'autore. È vietata la copia, distribuzione, modifica o reverse engineering.

5. LICENZA D'USO
La licenza è personale, non trasferibile e valida per il periodo acquistato. È vietata la rivendita o sublicenza a terzi.

6. PRIVACY
Il software raccoglie un ID univoco del dispositivo per la gestione della licenza. Nessun dato finanziario viene trasmesso a terzi.

7. LEGGE APPLICABILE
Il presente contratto è regolato dalla legge italiana.

Cliccando "ACCETTO" dichiari di aver letto, compreso e accettato integralmente i presenti Termini e Condizioni d'uso.

Per informazioni: info@arbexa.pro
"""

class TermsWindow(ctk.CTkToplevel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.title("Arbexa - Termini e Condizioni")
        self.geometry("600x580")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self._accepted = False
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="🎯 ARBEXA",
                     font=("Segoe UI", 18, "bold"),
                     text_color=GOLD).pack(pady=(20, 2))
        ctk.CTkLabel(self, text="TERMINI E CONDIZIONI D'USO",
                     font=("Segoe UI", 11),
                     text_color=BLUE).pack(pady=(0, 12))

        # Testo termini
        frame = ctk.CTkFrame(self, fg_color=CARD2, corner_radius=10,
                             border_width=1, border_color=BORDER)
        frame.pack(fill="both", expand=True, padx=24, pady=(0, 10))

        txt = ctk.CTkTextbox(frame, fg_color=CARD2, text_color=LIGHT,
                             font=("Segoe UI", 10), wrap="word",
                             scrollbar_button_color=BORDER)
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("end", TERMS_TEXT)
        txt.configure(state="disabled")

        # Checkbox accettazione
        self.check_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self, text="Ho letto e accetto i Termini e Condizioni d'uso",
                        variable=self.check_var,
                        font=("Segoe UI", 11),
                        text_color=WHITE,
                        checkbox_width=20, checkbox_height=20,
                        border_color=BLUE, checkmark_color=GREEN,
                        hover_color=CARD2).pack(pady=(4, 10))

        # Bottoni
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(pady=(0, 20))
        ctk.CTkButton(bf, text="RIFIUTA", width=140, height=40,
                      fg_color=CARD2, hover_color=BORDER,
                      text_color=RED, font=("Segoe UI", 12, "bold"),
                      command=self._rifiuta).pack(side="left", padx=8)
        ctk.CTkButton(bf, text="✓  ACCETTO", width=180, height=40,
                      fg_color="#0A3D1F", hover_color="#145214",
                      text_color=GREEN, font=("Segoe UI", 13, "bold"),
                      command=self._accetta).pack(side="left", padx=8)

    def _accetta(self):
        if not self.check_var.get():
            messagebox.showwarning("Attenzione",
                                   "Devi spuntare la casella per accettare i Termini e Condizioni.")
            return
        set_terms_accepted()
        self._accepted = True
        self.destroy()

    def _rifiuta(self):
        self.destroy()

def load_config():
    """Carica config da disco. Usa cache in memoria con invalidazione su save."""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                _config_cache = json.load(f)
                return _config_cache
        except:
            pass
    _config_cache = {"traders": []}
    return _config_cache

def save_config(cfg):
    """Salva config su disco e aggiorna cache."""
    global _config_cache
    _config_cache = cfg
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# Cache config in memoria (riduce I/O del 90%)
_config_cache = None

def get_traders():
    cfg = load_config()
    traders = cfg.get("traders", [])
    if not traders:
        traders = [make_default_trader()]
        cfg["traders"] = traders
        save_config(cfg)
    return traders

def save_traders(traders):
    cfg = load_config()
    cfg["traders"] = traders
    save_config(cfg)

def make_default_trader():
    tid = "t_" + "".join(random.choices(string.ascii_lowercase, k=6))
    return {
        "id": tid,
        "prop_firm": "Prop Number One",
        "prop_color": "#1A3A6B",
        "simbolo_prop": "XAUUSD",
        "simbolo_hedge": "XAUUSD",
        "size": 100000,
        "stop_prop": 1000,        # 1% della size (100k × 1%)
        "target_hedge": 300,      # 10% della size hedge (es. 3000 × 10%)
        "distanza": 750,          # nuovo default SL stretto in partenza
        "distanza_f1": 750,
        "distanza_f2": 750,
        "fase": "Fase 1",
        "dd_giornaliero_pct": 5.0,
        "dd_totale_pct": 10.0,
        "dd_stop_pct": 4.6,
        "target_pct": 10.0,
        "ratio_prop": "1:4",
        "ratio_hedge": "4:1",
        "ratio_prop_f1": "1:4",
        "ratio_hedge_f1": "4:1",
        "ratio_prop_f2": "1:2",
        "ratio_hedge_f2": "2:1",
        "broker_prop_path": "",
        "broker_hedge_path": "",
    }

def gradient_color(ratio):
    """ratio 0.0=rosso 0.5=giallo 1.0=verde"""
    ratio = max(0.0, min(1.0, ratio))
    if ratio < 0.5:
        r = 255
        g = int(255 * (ratio / 0.5))
    else:
        r = int(255 * (1.0 - (ratio - 0.5) / 0.5))
        g = 255
    b = 0
    return "#{:02X}{:02X}{:02X}".format(r, g, b)

def fmt_size(size):
    if size >= 1000:
        return str(int(size // 1000)) + "K"
    return str(int(size))
# ------------------------------------------------
#  PANNELLO CONFIGURAZIONE (overlay interno)
# ------------------------------------------------

_PROP_FIRMS_DEFAULT = {
    "FTMO":             {"dd_day": 5.0, "dd_tot": 10.0, "target": 10.0},
    "Funded Next":      {"dd_day": 5.0, "dd_tot": 10.0, "target":  8.0},
    "Prop Number One":  {"dd_day": 4.0, "dd_tot":  8.0, "target":  8.0},
    "The Funded Trader":{"dd_day": 5.0, "dd_tot": 10.0, "target": 10.0},
    "True Forex Funds": {"dd_day": 5.0, "dd_tot": 10.0, "target": 10.0},
    "Apex":             {"dd_day": 3.0, "dd_tot":  6.0, "target":  9.0},
    "Altro":            {"dd_day": 5.0, "dd_tot": 10.0, "target": 10.0},
}

_PROP_FIRMS_FILE = os.path.join(BASE_DIR, "prop_firms.json")

def _load_prop_firms():
    try:
        if os.path.exists(_PROP_FIRMS_FILE):
            with open(_PROP_FIRMS_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return dict(_PROP_FIRMS_DEFAULT)

def _save_prop_firms(data):
    try:
        with open(_PROP_FIRMS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except:
        pass

PROP_FIRMS = _load_prop_firms()

class PropFirmsEditor(ctk.CTkFrame):
    """Editor per modificare/aggiungere prop firm preset."""

    def __init__(self, parent, on_close=None):
        super().__init__(parent, fg_color=BG, border_width=2,
                         border_color=GOLD, corner_radius=14)
        self.on_close = on_close
        self._rows = []
        self._build()

    def _build(self):
        # Header
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.pack(fill="x", padx=16, pady=(14, 4))
        ctk.CTkLabel(hdr, text="✏ GESTISCI PROP FIRM",
                     font=("Segoe UI", 13, "bold"), text_color=GOLD).pack(side="left")
        ctk.CTkButton(hdr, text="✕", width=28, height=28,
                      fg_color="transparent", hover_color=BORDER,
                      text_color=GRAY, font=("Segoe UI", 13),
                      command=self._close).pack(side="right")

        # Header colonne
        col_frame = ctk.CTkFrame(self, fg_color=CARD2, corner_radius=0)
        col_frame.pack(fill="x", padx=16, pady=(4, 0))
        for txt, w in [("Nome", 130), ("DD Giorn.%", 70), ("DD Tot.%", 70), ("Target%", 70), ("", 60)]:
            ctk.CTkLabel(col_frame, text=txt, font=("Segoe UI", 9, "bold"),
                         text_color=GRAY, width=w, anchor="center").pack(side="left", padx=2)

        # Scroll area righe
        self.sc = ctk.CTkScrollableFrame(self, fg_color="transparent", height=220)
        self.sc.pack(fill="x", padx=16, pady=4)

        self._reload_rows()

        # Footer — aggiungi nuova + salva
        ft = ctk.CTkFrame(self, fg_color="transparent")
        ft.pack(fill="x", padx=16, pady=(4, 14))
        ctk.CTkButton(ft, text="+ AGGIUNGI PROP", width=150, height=32,
                      fg_color=CARD2, hover_color=BORDER,
                      text_color=BLUE, font=("Segoe UI", 11, "bold"),
                      command=self._add_row).pack(side="left", padx=(0, 8))
        ctk.CTkButton(ft, text="💾 SALVA", width=110, height=32,
                      fg_color="#1A3A1A", hover_color="#1A6B1A",
                      text_color=GREEN, font=("Segoe UI", 11, "bold"),
                      command=self._save).pack(side="left")
        ctk.CTkButton(ft, text="↺ RIPRISTINA DEFAULT", width=160, height=32,
                      fg_color=CARD2, hover_color=BORDER,
                      text_color=GRAY, font=("Segoe UI", 10),
                      command=self._reset).pack(side="right")

    def _reload_rows(self):
        for w in self.sc.winfo_children():
            w.destroy()
        self._rows = []
        for name, p in PROP_FIRMS.items():
            self._add_row(name, p["dd_day"], p["dd_tot"], p["target"],
                          locked=(name == "Altro"))

    def _add_row(self, name="", dd_day=5.0, dd_tot=10.0, target=10.0, locked=False):
        row = ctk.CTkFrame(self.sc, fg_color=CARD, corner_radius=6)
        row.pack(fill="x", pady=2)

        e_name = ctk.CTkEntry(row, width=130, height=28, font=("Segoe UI", 11),
                               corner_radius=5)
        e_name.insert(0, name)
        if locked:
            e_name.configure(state="disabled", text_color=GRAY)
        e_name.pack(side="left", padx=3)

        entries = []
        for val, w in [(dd_day, 70), (dd_tot, 70), (target, 70)]:
            e = ctk.CTkEntry(row, width=w, height=28, font=("Segoe UI", 11),
                              corner_radius=5)
            e.insert(0, str(val))
            e.pack(side="left", padx=3)
            entries.append(e)

        if not locked:
            ctk.CTkButton(row, text="🗑", width=30, height=28,
                          fg_color="transparent", hover_color=BORDER,
                          text_color=GRAY, font=("Segoe UI", 12),
                          command=lambda r=row: self._del_row(r)).pack(side="left", padx=3)

        self._rows.append((row, e_name, entries[0], entries[1], entries[2], locked))

    def _del_row(self, row_frame):
        self._rows = [r for r in self._rows if r[0] != row_frame]
        row_frame.destroy()

    def _save(self):
        new_firms = {}
        for _, e_name, e_ddd, e_ddt, e_tgt, _ in self._rows:
            name = e_name.get().strip()
            if not name:
                continue
            try:
                new_firms[name] = {
                    "dd_day": float(e_ddd.get() or 5),
                    "dd_tot": float(e_ddt.get() or 10),
                    "target": float(e_tgt.get() or 10),
                }
            except ValueError:
                pass
        if "Altro" not in new_firms:
            new_firms["Altro"] = {"dd_day": 5.0, "dd_tot": 10.0, "target": 10.0}
        global PROP_FIRMS
        PROP_FIRMS = new_firms
        _save_prop_firms(new_firms)
        self._close()

    def _reset(self):
        global PROP_FIRMS
        PROP_FIRMS = dict(_PROP_FIRMS_DEFAULT)
        _save_prop_firms(PROP_FIRMS)
        self._reload_rows()

    def _close(self):
        if self.on_close:
            self.on_close()
        self.destroy()

# ------------------------------------------------
#  AUTO-DETECT MT5 INSTALLATIONS
# ------------------------------------------------

# Path tipici dove cercare terminal64.exe
_MT5_SEARCH_PATHS_LIST = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    os.path.expanduser(r"~\AppData\Roaming\MetaQuotes\Terminal"),
]

def find_mt5_installations():
    """Cerca installazioni MT5 sul PC e ritorna lista di path terminal64.exe trovati."""
    found = []
    if os.name != "nt":
        return found
    seen = set()
    for base in _MT5_SEARCH_PATHS_LIST:
        if not os.path.exists(base): continue
        try:
            for root_dir, dirs, files in os.walk(base):
                # Limita profondità per evitare scansione lentissima
                depth = root_dir[len(base):].count(os.sep)
                if depth > 3:
                    dirs[:] = []
                    continue
                if "terminal64.exe" in files:
                    p = os.path.join(root_dir, "terminal64.exe")
                    if p not in seen:
                        seen.add(p)
                        found.append(p)
        except Exception: pass
    return found


def pick_mt5_path(parent, entry, title="Seleziona MT5"):
    """Mostra una finestra con i MT5 trovati automaticamente + opzione sfoglia.
    Quando l'utente sceglie, riempie l'entry con il path."""
    win = ctk.CTkToplevel(parent)
    win.title(title)
    win.geometry("560x380")
    win.configure(fg_color=BG)
    try:
        win.transient(parent)
        win.grab_set()
        win.lift()
        win.attributes("-topmost", True)
    except: pass
    # Centra
    try:
        win.update_idletasks()
        sw = win.winfo_screenwidth(); sh = win.winfo_screenheight()
        x = (sw - 560) // 2; y = (sh - 380) // 2
        win.geometry(f"560x380+{x}+{y}")
    except: pass

    ctk.CTkLabel(win, text="🔍 Selezione MT5",
                 font=("Segoe UI", 16, "bold"), text_color=GOLD).pack(pady=(18, 4))
    lbl_status = ctk.CTkLabel(win, text="Ricerca installazioni MT5 sul PC...",
                               font=("Segoe UI", 11), text_color=GRAY)
    lbl_status.pack(pady=(0, 10))

    sc = ctk.CTkScrollableFrame(win, fg_color="transparent", height=210)
    sc.pack(fill="both", expand=True, padx=18, pady=(0, 8))

    def populate(paths):
        for w in sc.winfo_children(): w.destroy()
        if not paths:
            lbl_status.configure(text="⚠ Nessuna installazione trovata. Sfoglia manualmente sotto.",
                                 text_color=GOLD)
            return
        lbl_status.configure(text=f"✓ Trovate {len(paths)} installazioni — clicca per selezionare",
                             text_color=GREEN)
        for p in paths:
            short = p.replace("C:\\Program Files\\", "").replace("\\terminal64.exe", "")
            short = short.replace("C:\\Program Files (x86)\\", "")
            def pick(_p=p):
                entry.delete(0, "end")
                entry.insert(0, _p)
                win.destroy()
            ctk.CTkButton(sc, text=short, command=pick,
                          fg_color=CARD2, hover_color=BORDER,
                          anchor="w", height=36,
                          font=("Segoe UI", 11)).pack(fill="x", pady=2)

    def do_browse_manual():
        path = filedialog.askopenfilename(
            title="Seleziona terminal64.exe",
            filetypes=[("Executable", "*.exe")])
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)
            win.destroy()

    btn_frame = ctk.CTkFrame(win, fg_color=BG)
    btn_frame.pack(fill="x", padx=18, pady=(0, 14))
    ctk.CTkButton(btn_frame, text="📂 Sfoglia manualmente...",
                  width=240, height=36,
                  fg_color=CARD2, hover_color=BORDER,
                  command=do_browse_manual).pack(side="left")
    ctk.CTkButton(btn_frame, text="✕ Annulla",
                  width=110, height=36,
                  fg_color="transparent", border_color=BORDER, border_width=1,
                  text_color=GRAY,
                  command=win.destroy).pack(side="right")

    # Popolazione asincrona
    def task():
        paths = find_mt5_installations()
        try: win.after(0, lambda: populate(paths))
        except: pass
    threading.Thread(target=task, daemon=True).start()


class ConfigPanel(ctk.CTkFrame):
    """Pannello che si sovrappone alla finestra principale - no Toplevel."""

    def __init__(self, parent, on_save, on_cancel, trader=None, start_tab="config"):
        super().__init__(parent, fg_color=BG,
                         border_width=2, border_color=BORDER,
                         corner_radius=16)
        self.on_save    = on_save
        self.on_cancel  = on_cancel
        self.trader     = trader
        self._start_tab = start_tab if start_tab in ("config", "params", "broker") else "config"
        self.sel_color = trader["prop_color"] if trader else "#1A3A6B"
        self._lbl_status = None  # inizializza prima di _build
        self._build()
        if trader:
            self._prefill()

    def _build(self):
        ctk.CTkLabel(self, text="CONFIGURA SCHEDA",
                     font=("Segoe UI", 15, "bold"),
                     text_color=GOLD).pack(pady=(16, 6))

        # ── TAB buttons (3 tab) ─────────────────────────────────
        tab_bar = ctk.CTkFrame(self, fg_color=CARD2, corner_radius=8)
        tab_bar.pack(fill="x", padx=20, pady=(0, 8))
        self._tab = ctk.StringVar(value=self._start_tab)
        self._tab_frames = {}

        def show_tab(name):
            self._tab.set(name)
            for n, f in self._tab_frames.items():
                f.pack_forget()
            self._tab_frames[name].pack(fill="both", expand=True, padx=20)
            btn_config.configure(fg_color=BORDER if name=="config" else "transparent",
                                 text_color=WHITE if name=="config" else GRAY)
            btn_params.configure(fg_color=BORDER if name=="params" else "transparent",
                                 text_color=WHITE if name=="params" else GRAY)
            btn_broker.configure(fg_color=BORDER if name=="broker" else "transparent",
                                 text_color=WHITE if name=="broker" else GRAY)

        btn_config = ctk.CTkButton(tab_bar, text="⚙ Configurazione", width=150, height=30,
                                    fg_color=BORDER, text_color=WHITE, hover_color=BORDER,
                                    font=("Segoe UI", 11, "bold"),
                                    command=lambda: show_tab("config"))
        btn_config.pack(side="left", padx=4, pady=4)
        btn_params = ctk.CTkButton(tab_bar, text="📊 Parametri", width=130, height=30,
                                    fg_color="transparent", text_color=GRAY, hover_color=CARD2,
                                    font=("Segoe UI", 11, "bold"),
                                    command=lambda: show_tab("params"))
        btn_params.pack(side="left", padx=4, pady=4)
        btn_broker = ctk.CTkButton(tab_bar, text="🔗 Broker", width=110, height=30,
                                    fg_color="transparent", text_color=GRAY, hover_color=CARD2,
                                    font=("Segoe UI", 11, "bold"),
                                    command=lambda: show_tab("broker"))
        btn_broker.pack(side="left", padx=4, pady=4)

        # ── TAB 1: Configurazione ──────────────────────────────────
        sc = ctk.CTkScrollableFrame(self, fg_color="transparent", height=420)
        self._tab_frames["config"] = sc

        def sel_row(lbl, values, default_val):
            f = ctk.CTkFrame(sc, fg_color="transparent")
            f.pack(fill="x", pady=4)
            ctk.CTkLabel(f, text=lbl, font=("Segoe UI", 11),
                         text_color=GRAY, width=190, anchor="w").pack(side="left")
            var = ctk.StringVar(value=str(default_val))
            om = ctk.CTkOptionMenu(f, variable=var, values=[str(v) for v in values],
                                   width=160, font=("Segoe UI", 12))
            om.pack(side="left")
            return var

        def entry_row(parent, lbl, ph, default=""):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            f.pack(fill="x", pady=3)
            ctk.CTkLabel(f, text=lbl, font=("Segoe UI", 11),
                         text_color=GRAY, width=190, anchor="w").pack(side="left")
            e = ctk.CTkEntry(f, height=32, corner_radius=7,
                             font=("Segoe UI", 12), placeholder_text=ph)
            e.pack(side="left", fill="x", expand=True)
            if default:
                e.insert(0, str(default))
            return e

        # Prop firm
        ctk.CTkLabel(sc, text="PROP FIRM", font=("Segoe UI", 11, "bold"),
                     text_color=GOLD).pack(anchor="w", pady=(8, 2))
        pf_frame = ctk.CTkFrame(sc, fg_color="transparent")
        pf_frame.pack(fill="x", pady=3)
        ctk.CTkLabel(pf_frame, text="Seleziona Prop", font=("Segoe UI", 11),
                     text_color=GRAY, width=190, anchor="w").pack(side="left")
        self.prop_var = ctk.StringVar(value="Seleziona...")
        self.prop_menu = ctk.CTkOptionMenu(
            pf_frame, variable=self.prop_var,
            values=list(PROP_FIRMS.keys()),
            width=180, font=("Segoe UI", 12),
            command=self._on_prop_selected)
        self.prop_menu.pack(side="left")
        ctk.CTkButton(pf_frame, text="✏", width=32, height=32,
                      fg_color=CARD2, hover_color=BORDER,
                      font=("Segoe UI", 13),
                      command=self._open_prop_editor).pack(side="left", padx=(6,0))

        self.e_firm = entry_row(sc, "Nome Prop Firm", "es. FTMO", "")

        # Selettori configurazione
        ctk.CTkLabel(sc, text="CONFIGURAZIONE CONTO", font=("Segoe UI", 11, "bold"),
                     text_color=GOLD).pack(anchor="w", pady=(12, 4))

        self.size_var    = sel_row("Size Conto PROP ($)", [5000,10000,25000,50000,100000,200000], 100000)
        self.dd_day_var  = sel_row("DD Giornaliero (%)", [3,4,5], 5)
        self.dd_tot_var  = sel_row("DD Massimo (%)", [8,10], 10)
        self.tgt_var     = sel_row("Target Profitto (%)", [5,8,10], 10)

        # DD Stop auto (entry libera)
        self.e_dd_stop = entry_row(sc, "DD STOP Auto (%)", "es. 4.6", "4.6")

        # Fase
        ctk.CTkLabel(sc, text="FASE", font=("Segoe UI", 11, "bold"),
                     text_color=GOLD).pack(anchor="w", pady=(12, 4))
        ff = ctk.CTkFrame(sc, fg_color="transparent")
        ff.pack(fill="x", pady=3)
        self.fase_var = ctk.StringVar(value="Fase 1")
        for f_nome in ["Fase 1", "Fase 2", "Funded"]:
            ctk.CTkRadioButton(ff, text=f_nome, variable=self.fase_var, value=f_nome,
                               font=("Segoe UI", 12), text_color=WHITE,
                               command=self._update_trade_tab).pack(side="left", padx=8)

        # Simboli
        ctk.CTkLabel(sc, text="SIMBOLI", font=("Segoe UI", 11, "bold"),
                     text_color=GOLD).pack(anchor="w", pady=(12, 4))
        self.e_simbolo_p = entry_row(sc, "Simbolo PROP",  "es. XAUUSD", "XAUUSD")
        self.e_simbolo_h = entry_row(sc, "Simbolo HEDGE", "es. XAUUSD", "XAUUSD")

        # Colore scheda
        ctk.CTkLabel(sc, text="COLORE SCHEDA", font=("Segoe UI", 11, "bold"),
                     text_color=GRAY).pack(anchor="w", pady=(12, 4))
        cf = ctk.CTkFrame(sc, fg_color="transparent")
        cf.pack(fill="x", pady=2)
        self.color_prev = ctk.CTkFrame(cf, width=30, height=30, corner_radius=6,
                                        fg_color=self.sel_color)
        self.color_prev.pack(side="left", padx=(0, 8))
        self.color_prev.pack_propagate(False)
        for name, color in PROP_COLORS.items():
            ctk.CTkButton(cf, text="", width=22, height=22, corner_radius=4,
                          fg_color=color, hover_color=color,
                          command=lambda c=color: self._set_color(c)).pack(side="left", padx=2)
        ctk.CTkButton(cf, text="Custom", width=60, height=26,
                      fg_color=CARD2, hover_color=BORDER,
                      font=("Segoe UI", 10),
                      command=self._pick_color).pack(side="left", padx=6)

        # Costo prop + calcola + scenari in TAB 1
        ctk.CTkLabel(sc, text="COSTO PROP & SCENARI", font=("Segoe UI", 11, "bold"),
                     text_color=GOLD).pack(anchor="w", pady=(14, 4))
        cp_f = ctk.CTkFrame(sc, fg_color="transparent")
        cp_f.pack(fill="x", pady=3)
        ctk.CTkLabel(cp_f, text="Costo Prop (€)", font=("Segoe UI", 11),
                     text_color=GRAY, width=190, anchor="w").pack(side="left")
        self.e_costo_prop = ctk.CTkEntry(cp_f, height=32, corner_radius=7,
                                          font=("Segoe UI", 12), placeholder_text="es. 500")
        self.e_costo_prop.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(sc, text="⚡ CALCOLA PARAMETRI", height=36,
                      fg_color="#1A3A5C", hover_color="#1E4D7A",
                      font=("Segoe UI", 12, "bold"),
                      command=self._calcola_parametri_auto).pack(fill="x", pady=(8, 4))
        ctk.CTkLabel(sc, text="SCENARI", font=("Segoe UI", 11, "bold"),
                     text_color=BLUE).pack(anchor="w", pady=(8, 4))
        # Blocco singolo: mostra solo l'importo della fase corrente
        self.frm_scenari = ctk.CTkFrame(sc, fg_color=CARD2,
                                         corner_radius=12, border_width=2, border_color=GOLD)
        self.frm_scenari.pack(fill="x", pady=4)
        self.lbl_scenario_fase = ctk.CTkLabel(self.frm_scenari, text="— FASE 1 —",
                                               font=("Segoe UI", 14, "bold"), text_color=GOLD,
                                               anchor="center", justify="center")
        self.lbl_scenario_fase.pack(pady=(14, 4), fill="x")
        self.lbl_scenario_titolo = ctk.CTkLabel(self.frm_scenari,
                                                 text="💰 DA CARICARE SU CONTO HEDGE",
                                                 font=("Segoe UI", 11, "bold"), text_color=GRAY,
                                                 anchor="center", justify="center")
        self.lbl_scenario_titolo.pack(pady=(0, 6), fill="x")
        self.lbl_scenario_importo = ctk.CTkLabel(self.frm_scenari, text="$ ---",
                                                  font=("Segoe UI", 32, "bold"), text_color=GREEN,
                                                  anchor="center", justify="center")
        self.lbl_scenario_importo.pack(pady=(0, 16), fill="x")

        # ── TAB 2: Parametri Trading ──────────────────────────────
        sc2 = ctk.CTkScrollableFrame(self, fg_color="transparent", height=420)
        self._tab_frames["params"] = sc2

        self._params_locked = True

        # Overlay password che copre tutto il tab2
        self._pw_overlay = ctk.CTkFrame(sc2, fg_color=BG, corner_radius=0)
        self._pw_overlay.pack(fill="both", expand=True)
        ctk.CTkLabel(self._pw_overlay, text="🔒", font=("Segoe UI", 40),
                     text_color=GOLD).pack(pady=(40, 8))
        ctk.CTkLabel(self._pw_overlay, text="PARAMETRI PROTETTI",
                     font=("Segoe UI", 14, "bold"), text_color=GOLD).pack(pady=(0, 4))
        ctk.CTkLabel(self._pw_overlay, text="Inserisci la password per visualizzare\ne modificare i parametri di trading.",
                     font=("Segoe UI", 11), text_color=GRAY, justify="center").pack(pady=(0, 20))
        self.e_params_pw = ctk.CTkEntry(self._pw_overlay, height=36, corner_radius=8,
                                         width=200, font=("Segoe UI", 13),
                                         placeholder_text="Password", show="*")
        self.e_params_pw.pack(pady=(0, 10))
        self.e_params_pw.bind("<Return>", lambda e: self._sblocca_parametri())
        ctk.CTkButton(self._pw_overlay, text="🔓 SBLOCCA", width=160, height=36,
                      fg_color="#1A3A5C", hover_color="#1E4D7A",
                      font=("Segoe UI", 12, "bold"),
                      command=self._sblocca_parametri).pack()

        # Contenuto tab2 — nascosto finché non sbloccato
        self._params_content = ctk.CTkScrollableFrame(sc2, fg_color="transparent", height=380)
        # NON pack — verrà mostrato solo dopo sblocco

        def param_row(lbl, ph, default=""):
            f = ctk.CTkFrame(self._params_content, fg_color="transparent")
            f.pack(fill="x", pady=3)
            ctk.CTkLabel(f, text=lbl, font=("Segoe UI", 11),
                         text_color=GRAY, width=190, anchor="w").pack(side="left")
            e = ctk.CTkEntry(f, height=32, corner_radius=7,
                             font=("Segoe UI", 12), placeholder_text=ph)
            e.pack(side="left", fill="x", expand=True)
            if default:
                e.insert(0, str(default))
            return e

        ctk.CTkLabel(self._params_content, text="PARAMETRI TRADING",
                     font=("Segoe UI", 11, "bold"), text_color=GOLD).pack(anchor="w", pady=(8, 6))
        self.e_size     = param_row("Size HEDGE ($)",      "calcolato", "")
        self.e_stop     = param_row("Stop Loss PROP ($)",  "es. 1000",  "")
        self.e_target_h = param_row("Target HEDGE ($)",    "es. 115",   "")
        self.e_distanza = param_row("Distanza punti",      "es. 750",   "750")

        # Rapporto SL:TP — unico set, cambia al cambio fase
        ctk.CTkLabel(self._params_content, text="RAPPORTO SL:TP",
                     font=("Segoe UI", 11, "bold"), text_color=GOLD).pack(anchor="w", pady=(12, 4))
        rp_f = ctk.CTkFrame(self._params_content, fg_color="transparent")
        rp_f.pack(fill="x", pady=3)
        ctk.CTkLabel(rp_f, text="PROP (SL:TP)", font=("Segoe UI", 11),
                     text_color=GRAY, width=190, anchor="w").pack(side="left")
        self.ratio_prop_var = ctk.StringVar(value="1:4")
        self.ratio_prop_menu = ctk.CTkOptionMenu(
            rp_f, variable=self.ratio_prop_var,
            values=["1:1", "1:1.5", "1:2", "1:3", "1:4"],
            width=120, font=("Segoe UI", 12),
            command=self._update_hedge_ratio)
        self.ratio_prop_menu.pack(side="left")

        rh_f = ctk.CTkFrame(self._params_content, fg_color="transparent")
        rh_f.pack(fill="x", pady=3)
        ctk.CTkLabel(rh_f, text="HEDGE (TP:SL)", font=("Segoe UI", 11),
                     text_color=GRAY, width=190, anchor="w").pack(side="left")
        self.lbl_ratio_hedge = ctk.CTkLabel(rh_f, text="4:1",
                                             font=("Segoe UI", 12, "bold"),
                                             text_color=BLUE)
        self.lbl_ratio_hedge.pack(side="left")

        # Label fase attiva nei parametri
        self.lbl_fase_params = ctk.CTkLabel(self._params_content,
                                             text="● Parametri Fase 1",
                                             font=("Segoe UI", 10, "bold"),
                                             text_color=GOLD)
        self.lbl_fase_params.pack(anchor="w", pady=(8, 0))

        # ── Toggle Ratio Automatico ───────────────────────────────
        ctk.CTkLabel(self._params_content, text="RATIO AUTOMATICO",
                     font=("Segoe UI", 11, "bold"), text_color=GOLD).pack(anchor="w", pady=(12, 4))
        auto_f = ctk.CTkFrame(self._params_content, fg_color="transparent")
        auto_f.pack(fill="x", pady=3)
        ctk.CTkLabel(auto_f, text="Adatta R:R alla distanza dal target",
                     font=("Segoe UI", 11), text_color=GRAY, width=190, anchor="w").pack(side="left")
        self._auto_ratio_var = ctk.BooleanVar(value=True)
        self._auto_ratio_switch = ctk.CTkSwitch(
            auto_f, text="", variable=self._auto_ratio_var,
            width=46, command=self._on_auto_ratio_change,
            onvalue=True, offvalue=False)
        self._auto_ratio_switch.pack(side="left", padx=8)
        self.lbl_auto_ratio = ctk.CTkLabel(auto_f, text="ON",
                                            font=("Segoe UI", 11, "bold"), text_color=GREEN)
        self.lbl_auto_ratio.pack(side="left")
        ctk.CTkLabel(self._params_content,
                     text="ON: ratio si adatta automaticamente (1:4→1:1)\nOFF: usa il ratio impostato manualmente sopra",
                     font=("Segoe UI", 9), text_color=GRAY, justify="left").pack(anchor="w", padx=2)


        # Alias retrocompatibilità
        self.e_distanza_f1 = self.e_distanza
        self.e_distanza_f2 = self.e_distanza
        self.ratio_prop_f2_var = ctk.StringVar(value="1:2")

        # ── TAB 3: Broker ─────────────────────────────────────────
        sc3 = ctk.CTkScrollableFrame(self, fg_color="transparent", height=420)
        self._tab_frames["broker"] = sc3

        ctk.CTkLabel(sc3, text="CONNESSIONE BROKER", font=("Segoe UI", 11, "bold"),
                     text_color=GOLD).pack(anchor="w", pady=(8, 4))
        ctk.CTkLabel(sc3, text="Seleziona il percorso del terminale MT5 per PROP e HEDGE.",
                     font=("Segoe UI", 10), text_color=GRAY).pack(anchor="w", pady=(0, 10))

        def browse_row3(lbl, ph):
            f = ctk.CTkFrame(sc3, fg_color="transparent")
            f.pack(fill="x", pady=5)
            ctk.CTkLabel(f, text=lbl, font=("Segoe UI", 11),
                         text_color=GRAY, width=190, anchor="w").pack(side="left")
            e = ctk.CTkEntry(f, height=32, corner_radius=7,
                             font=("Segoe UI", 11), placeholder_text=ph)
            e.pack(side="left", fill="x", expand=True, padx=(0, 6))
            ctk.CTkButton(f, text="...", width=36, height=32,
                          fg_color=CARD2, hover_color=BORDER,
                          command=lambda en=e: self._browse(en)).pack(side="left")
            return e

        self.e_bp = browse_row3("Broker PROP (terminal64.exe)",  "C:/percorso/terminal64.exe")
        self.e_bh = browse_row3("Broker HEDGE (terminal64.exe)", "C:/percorso/terminal64.exe")

        show_tab(self._start_tab)

        # Bottoni
        bf = ctk.CTkFrame(self, fg_color="transparent")
        bf.pack(pady=10)
        ctk.CTkButton(bf, text="ANNULLA", width=120, height=38,
                      fg_color=CARD2, hover_color=BORDER,
                      command=self.on_cancel).pack(side="left", padx=8)
        ctk.CTkButton(bf, text="SALVA", width=120, height=38,
                      fg_color="#1A3A5C", hover_color="#1E4D7A",
                      font=("Segoe UI", 13, "bold"),
                      command=self._save).pack(side="left", padx=8)

        # Messaggio status
        self._lbl_status = ctk.CTkLabel(self, text="", font=("Segoe UI", 11),
                                         text_color=GOLD, height=20)
        self._lbl_status.pack(pady=(2, 0))

    def set_msg(self, msg, color=GOLD):
        try:
            if self._lbl_status:
                self._lbl_status.configure(text=msg, text_color=color)
        except: pass

    def _calcola_parametri_auto(self):
        """Calcola parametri automaticamente da costo prop e selettori."""
        try:
            costo_str = self.e_costo_prop.get().strip()
            if not costo_str:
                self.set_msg("Inserisci il costo prop", RED)
                return
            costo   = float(costo_str)
            size    = float(self.size_var.get().replace(",","").replace(".",""))
            tgt_pct = float(self.tgt_var.get()) / 100
        except Exception as ex:
            self.set_msg("Err input: " + str(ex)[:40], RED)
            return

        # Calcoli base — SL PROP = 1% size sempre
        # TP HEDGE: Fase 1 = 10% size hedge, Fase 2 = 20% size hedge
        fase_sel   = self.fase_var.get() if hasattr(self, "fase_var") else "Fase 1"
        hedge_size = round(costo * 2.0, 2)
        sl_prop    = round(size * 0.01, 2)
        tp_pct     = 0.20 if fase_sel == "Fase 2" else 0.10
        tp_hedge   = round(hedge_size * tp_pct, 2)

        # Distanza punti: invertita — più piccolo il conto, più punti di stop
        dist_map = {5000:1200, 10000:1200, 25000:1000, 50000:800, 100000:600, 200000:400}
        distanza = dist_map.get(int(size), 600)

        # Aggiorna campi parametri base
        for e, val in [(self.e_size, hedge_size), (self.e_stop, sl_prop),
                       (self.e_target_h, tp_hedge)]:
            try:
                e.delete(0, "end")
                e.insert(0, str(val))
            except: pass

        # Aggiorna distanza e salva nel cache fase
        try:
            self.e_distanza.delete(0, "end")
            self.e_distanza.insert(0, str(distanza))
            if fase_sel == "Fase 1":
                self._dist_f1 = str(distanza)
                self._ratio_f1 = self.ratio_prop_var.get()
            else:
                self._dist_f2 = str(distanza)
                self._ratio_f2 = self.ratio_prop_var.get()
        except: pass

        # Aggiorna scenari
        self._calcola_scenari()
        self.set_msg("✅ Parametri calcolati!", GREEN)

    def _aggiorna_rapporto_da_target(self):
        """Imposta SL+R:R dinamico in base alla distanza dal target.
        Logica: SL parte stretto (750) lontano dal target e si allarga (1200)
        avvicinandosi al target. R:R parte alto (1:4) e scende (1:1) a target."""
        try:
            size    = float(self.size_var.get().replace(",",""))
            tgt_pct = float(self.tgt_var.get()) / 100
            tgt_val = size * tgt_pct
            # Guadagno attuale (approssimato a 0 se non disponibile)
            guad = 0
            if hasattr(self, '_balance_start_day') and self._balance_start_day:
                bal  = getattr(self, 'lbl_prop_bal', None)
                if bal:
                    try: guad = max(0, float(str(bal.cget("text")).replace("$","").replace(",","")) - size)
                    except: pass
            manca_pct = max(0, tgt_val - guad) / tgt_val if tgt_val > 0 else 1.0
            # Scala SL+R:R: parte stretto/aggressivo lontano, si allarga/conservativo vicino
            if   manca_pct > 0.07: ratio, dist = "1:4",   750   # lontano dal target
            elif manca_pct > 0.05: ratio, dist = "1:4",   850
            elif manca_pct > 0.03: ratio, dist = "1:3",   950
            elif manca_pct > 0.01: ratio, dist = "1:2",   1100
            else:                   ratio, dist = "1:1",   1200  # a target
            self.ratio_prop_var.set(ratio)
            self._update_hedge_ratio(ratio)
            # Aggiorna distanza
            if not self._params_locked:
                self.e_distanza.delete(0, "end")
                self.e_distanza.insert(0, str(dist))
        except: pass

    def _on_auto_ratio_change(self):
        on = self._auto_ratio_var.get()
        try:
            self.lbl_auto_ratio.configure(
                text="ON" if on else "OFF",
                text_color=GREEN if on else RED)
            # Abilita/disabilita il menu ratio manuale
            state = "disabled" if on else "normal"
            self.ratio_prop_menu.configure(state=state)
        except: pass

    def _on_boost_change(self):
        on = self._boost_var.get()
        try:
            self.lbl_boost.configure(
                text="ON 🚀" if on else "OFF",
                text_color=GOLD if on else GRAY)
            if on:
                lp = getattr(self, '_lotto_prop',  0.01) or 0.01
                lh = getattr(self, '_lotto_hedge', 0.01) or 0.01
                self.lbl_boost_lotti.configure(
                    text="→ PROP: {}  HEDGE: {}".format(round(lp*2,2), round(lh*2,2)))
            else:
                self.lbl_boost_lotti.configure(text="")
        except: pass

    def _sblocca_parametri(self):
        """Sblocca i parametri calcolati con password."""
        PARAMS_PW = "arbexa2025"
        pw = self.e_params_pw.get().strip()
        if pw != PARAMS_PW:
            self.set_msg("Password errata", RED)
            return
        # Nasconde overlay e mostra contenuto
        self._pw_overlay.pack_forget()
        self._params_content.pack(fill="both", expand=True)
        self._params_locked = False
        self.set_msg("🔓 Parametri sbloccati", GREEN)

    def _on_prop_selected(self, val):
        if val in PROP_FIRMS and val != "Altro":
            p = PROP_FIRMS[val]
            for e, v in [(self.e_firm, val)]:
                e.delete(0, "end")
                e.insert(0, str(v))
            # Selettori
            try: self.dd_day_var.set(str(int(float(p["dd_day"]))))
            except: pass
            try: self.dd_tot_var.set(str(int(float(p["dd_tot"]))))
            except: pass
            try: self.tgt_var.set(str(int(float(p["target"]))))
            except: pass
        self._update_trade_tab()

    def _open_prop_editor(self):
        editor = PropFirmsEditor(self.winfo_toplevel(), on_close=self._refresh_prop_menu)
        editor.place(relx=0.5, rely=0.5, anchor="center")
        editor.lift()

    def _refresh_prop_menu(self):
        """Aggiorna il menu dopo modifiche alle prop firms."""
        global PROP_FIRMS
        PROP_FIRMS = _load_prop_firms()
        self.prop_menu.configure(values=list(PROP_FIRMS.keys()))

    def _update_trade_tab(self):
        """Aggiorna scenari e switcha i parametri al cambio fase."""
        fase = self.fase_var.get() if hasattr(self, "fase_var") else "Fase 1"
        # Switch automatico parametri — funziona sempre, indipendentemente dal lock
        try:
            if fase == "Fase 1":
                dist  = getattr(self, "_dist_f1",  "750")
                ratio = getattr(self, "_ratio_f1", "1:4")
                if hasattr(self, "lbl_fase_params"):
                    self.lbl_fase_params.configure(text="● Parametri Fase 1", text_color=GOLD)
            elif fase == "Fase 2":
                dist  = getattr(self, "_dist_f2",  "750")
                ratio = getattr(self, "_ratio_f2", "1:4")
                if hasattr(self, "lbl_fase_params"):
                    self.lbl_fase_params.configure(text="● Parametri Fase 2", text_color=BLUE)
            else:
                dist  = getattr(self, "_dist_f1",  "750")
                ratio = getattr(self, "_ratio_f1", "1:4")
                if hasattr(self, "lbl_fase_params"):
                    self.lbl_fase_params.configure(text="● Parametri Funded", text_color=GREEN)
            if hasattr(self, "e_distanza"):
                self.e_distanza.configure(state="normal")
                self.e_distanza.delete(0, "end")
                self.e_distanza.insert(0, str(dist))
            if hasattr(self, "ratio_prop_var"):
                self.ratio_prop_var.set(ratio)
                self._update_hedge_ratio()
        except: pass
        costo_txt = self.e_costo_prop.get().strip() if hasattr(self, "e_costo_prop") else ""
        if costo_txt:
            self._calcola_scenari()

    def _calcola_scenari(self):
        try:
            costo = float(self.e_costo_prop.get().strip())
        except:
            return

        # Determina la fase corrente dalla scheda
        try:
            fase = self.fase_var.get()
        except:
            fase = "Fase 1"

        # Calcolo a cascata delle 3 formule
        hedge_f1 = round(costo * 2.10, 2)
        hedge_f2 = round((costo + hedge_f1) * 1.40 / 2, 2)
        hedge_fd = round((costo + hedge_f1 + hedge_f2) * 1.30, 2)

        # Mostra solo l'importo della fase corrente
        try:
            if fase == "Fase 2":
                self.lbl_scenario_fase.configure(text="— FASE 2 —", text_color=BLUE)
                self.lbl_scenario_importo.configure(text="$ {:,.2f}".format(hedge_f2), text_color=BLUE)
                self.frm_scenari.configure(border_color=BLUE)
            elif fase == "Funded":
                self.lbl_scenario_fase.configure(text="— FUNDED —", text_color=GREEN)
                self.lbl_scenario_importo.configure(text="$ {:,.2f}".format(hedge_fd), text_color=GREEN)
                self.frm_scenari.configure(border_color=GREEN)
            else:  # Fase 1 (default)
                self.lbl_scenario_fase.configure(text="— FASE 1 —", text_color=GOLD)
                self.lbl_scenario_importo.configure(text="$ {:,.2f}".format(hedge_f1), text_color=GOLD)
                self.frm_scenari.configure(border_color=GOLD)
        except: pass

    def _browse(self, entry):
        # Apre selettore con auto-detect MT5 + opzione sfoglia manuale
        pick_mt5_path(self.winfo_toplevel(), entry, title="Seleziona terminal64.exe")

    def _update_hedge_ratio(self, val=None):
        """Aggiorna ratio HEDGE a specchio del PROP."""
        rp = self.ratio_prop_var.get().split(":")
        hedge_val = "{}:{}".format(rp[1], rp[0])
        try: self.lbl_ratio_hedge.configure(text=hedge_val)
        except: pass
        # *** FIX: se auto_ratio è OFF, sincronizza anche trader dict ***
        auto_on = getattr(self, '_auto_ratio_var', None)
        if auto_on is not None and not auto_on.get():
            ratio_cur = self.ratio_prop_var.get()
            self.trader["ratio_prop"]  = ratio_cur
            self.trader["ratio_hedge"] = hedge_val

    def _set_color(self, c):
        self.sel_color = c
        self.color_prev.configure(fg_color=c)

    def _pick_color(self):
        r = colorchooser.askcolor(color=self.sel_color, title="Scegli colore")
        if r and r[1]:
            self._set_color(r[1])

    def _prefill(self):
        t = self.trader
        # Campi testo semplici
        for entry, key in [
            (self.e_firm,      "prop_firm"),
            (self.e_simbolo_p, "simbolo_prop"),
            (self.e_simbolo_h, "simbolo_hedge"),
            (self.e_dd_stop,   "dd_stop_pct"),
            (self.e_bp,        "broker_prop_path"),
            (self.e_bh,        "broker_hedge_path"),
        ]:
            try:
                entry.delete(0, "end")
                entry.insert(0, str(t.get(key, "")))
            except: pass
        # Campi parametri (possono essere bloccati)
        for entry, key in [
            (self.e_size,     "size"),
            (self.e_stop,     "stop_prop"),
            (self.e_target_h, "target_hedge"),
            (self.e_distanza, "distanza"),
        ]:
            try:
                cur_state = entry.cget("state")
                entry.configure(state="normal")
                entry.delete(0, "end")
                entry.insert(0, str(t.get(key, "")))
                entry.configure(state=cur_state)
            except: pass
        # Carica cache F1/F2 per lo switch automatico
        self._dist_f1  = str(t.get("distanza_f1",  t.get("distanza", "750")))
        self._dist_f2  = str(t.get("distanza_f2",  t.get("distanza", "750")))
        self._ratio_f1 = t.get("ratio_prop_f1", t.get("ratio_prop", "1:4"))
        self._ratio_f2 = t.get("ratio_prop_f2", "1:4")
        # Costo prop
        try:
            self.e_costo_prop.delete(0, "end")
            self.e_costo_prop.insert(0, str(t.get("costo_prop", "")))
        except: pass
        # Selettori
        try:
            sz = int(float(t.get("size", 100000)))
            sizes = [5000,10000,25000,50000,100000,200000]
            self.size_var.set(str(min(sizes, key=lambda x: abs(x-sz))))
        except: pass
        try:
            sh = t.get("size_hedge", "")
            if sh:
                cur = self.e_size.cget("state")
                self.e_size.configure(state="normal")
                self.e_size.delete(0,"end")
                self.e_size.insert(0, str(sh))
                self.e_size.configure(state=cur)
        except: pass
        try: self.dd_day_var.set(str(int(float(t.get("dd_giornaliero_pct", 5)))))
        except: pass
        try: self.dd_tot_var.set(str(int(float(t.get("dd_totale_pct", 10)))))
        except: pass
        try: self.tgt_var.set(str(int(float(t.get("target_pct", 10)))))
        except: pass
        # Fase e ratio
        try: self.fase_var.set(t.get("fase", "Fase 1"))
        except: pass
        try:
            fase_cur  = t.get("fase", "Fase 1")
            auto      = t.get("auto_ratio", True)
            if not auto:
                # Auto ratio OFF — carica il ratio manuale salvato
                self.ratio_prop_var.set(t.get("ratio_prop", "1:3"))
            elif fase_cur == "Fase 2":
                self.ratio_prop_var.set(t.get("ratio_prop_f2", t.get("ratio_prop", "1:2")))
            else:
                self.ratio_prop_var.set(t.get("ratio_prop_f1", t.get("ratio_prop", "1:4")))
            self._update_hedge_ratio()
        except: pass
        # Carica auto_ratio dal trader salvato
        try:
            auto = t.get("auto_ratio", True)
            self._auto_ratio_var.set(bool(auto))
            self._on_auto_ratio_change()
        except: pass
        # Prop firm dropdown
        firm = t.get("prop_firm", "")
        if firm in PROP_FIRMS:
            try: self.prop_var.set(firm)
            except: pass

    def _save(self):
        firm = self.e_firm.get().strip()
        if not firm:
            messagebox.showerror("Errore", "Inserisci il nome della Prop Firm!")
            return
        tid = self.trader["id"] if self.trader else "t_" + "".join(random.choices(string.ascii_lowercase, k=6))
        rp = self.ratio_prop_var.get().split(":")
        ratio_hedge = "{}:{}".format(rp[1], rp[0])
        fase_corrente = self.fase_var.get()
        dist_cur = int(self.e_distanza.get() or 1200)
        ratio_cur = self.ratio_prop_var.get()
        # Salva cache F1/F2 aggiornato
        if fase_corrente == "Fase 1":
            dist_f1 = dist_cur; dist_f2 = int(getattr(self, "_dist_f2", 800))
            ratio_f1 = ratio_cur; ratio_f2 = getattr(self, "_ratio_f2", "1:2")
        else:
            dist_f1 = int(getattr(self, "_dist_f1", 1200)); dist_f2 = dist_cur
            ratio_f1 = getattr(self, "_ratio_f1", "1:4"); ratio_f2 = ratio_cur
        data = {
            "id":                 tid,
            "prop_firm":          firm,
            "prop_color":         self.sel_color,
            "simbolo_prop":       self.e_simbolo_p.get().strip() or "XAUUSD",
            "simbolo_hedge":      self.e_simbolo_h.get().strip() or "XAUUSD",
            "size":               float(self.size_var.get().replace(",","").replace(".","") if hasattr(self,"size_var") else 100000),
            "size_hedge":         float(self.e_size.get() or 0) if self.e_size.get() else 0,
            "stop_prop":          float(self.e_stop.get() or 1000),
            "target_hedge":       float(self.e_target_h.get() or 300),
            "distanza":           dist_cur,
            "distanza_f1":        dist_f1,
            "distanza_f2":        dist_f2,
            "costo_prop":         float(self.e_costo_prop.get().strip() or 0),
            "fase":               fase_corrente,
            "dd_giornaliero_pct": float(self.dd_day_var.get() if hasattr(self,"dd_day_var") else 5),
            "dd_stop_pct":        float(self.e_dd_stop.get() or 4.6),
            "dd_totale_pct":      float(self.dd_tot_var.get() if hasattr(self,"dd_tot_var") else 10),
            "target_pct":         float(self.tgt_var.get() if hasattr(self,"tgt_var") else 10),
            "ratio_prop":         ratio_cur,
            "ratio_hedge":        ratio_hedge,
            "ratio_prop_f1":      ratio_f1,
            "ratio_hedge_f1":     "{}:{}".format(ratio_f1.split(":")[1], ratio_f1.split(":")[0]),
            "ratio_prop_f2":      ratio_f2,
            "ratio_hedge_f2":     "{}:{}".format(ratio_f2.split(":")[1], ratio_f2.split(":")[0]),
            "auto_ratio":         self._auto_ratio_var.get() if hasattr(self, "_auto_ratio_var") and self._auto_ratio_var else True,
            "broker_prop_path":   self.e_bp.get().strip(),
            "broker_hedge_path":  self.e_bh.get().strip(),
        }
        self.on_save(data)


# ------------------------------------------------
#  TRADER CARD
# ------------------------------------------------

class TraderCard(ctk.CTkFrame):
    def __init__(self, parent, trader, on_delete, on_edit, **kwargs):
        color = trader.get("prop_color", "#1A3A6B")
        super().__init__(parent, fg_color=CARD, corner_radius=14,
                         border_width=2, border_color=color,
                         width=CARD_W, height=CARD_H, **kwargs)
        self.pack_propagate(False)
        self.grid_propagate(False)
        self.trader    = trader
        self.on_delete = on_delete
        self.on_edit   = on_edit
        self.running   = True
        self._order_in_progress = False  # lock locale per evitare ordini doppi
        self._last_prop  = None
        self._last_hedge = None
        self._boost_var  = ctk.BooleanVar(value=False)
        self._auto_ratio_var = ctk.BooleanVar(value=bool(trader.get("auto_ratio", True)))
        self._build()
        self._start_refresh()

    def _build(self):
        t     = self.trader
        color = t.get("prop_color", "#1A3A6B")
        fase  = t.get("fase", "Fase 1")
        fase_color = {"Fase 1": "#d4af37", "Fase 2": "#c8a030", "Funded": "#00FF88"}.get(fase, GRAY)
        prop_name = t.get("prop_firm", "---")
        size_str  = "$" + fmt_size(t.get("size", 100000))

        # TITOLO — nome prop firm da solo ben visibile
        title_bar = ctk.CTkFrame(self, fg_color=color, corner_radius=10, height=30)
        title_bar.pack(fill="x", padx=2, pady=(2, 0))
        title_bar.pack_propagate(False)
        ctk.CTkButton(title_bar, text="⚙", width=24, height=20,
                      corner_radius=5, fg_color="transparent", hover_color="#2A2000",
                      font=("Segoe UI", 11, "bold"), text_color=GOLD,
                      command=lambda: self.on_edit(self.trader)
                      ).pack(side="left", padx=(6, 2))
        ctk.CTkLabel(title_bar, text=prop_name + "  " + size_str,
                     font=("Segoe UI", 13, "bold"),
                     text_color=WHITE).pack(side="left", padx=4)

        # CONTROLLI — riga compatta sotto il titolo
        hdr = ctk.CTkFrame(self, fg_color=CARD2, corner_radius=0, height=26)
        hdr.pack(fill="x", padx=2, pady=(0, 0))
        hdr.pack_propagate(False)

        ctk.CTkLabel(hdr, text=fase,
                     font=("Segoe UI", 8, "bold"), text_color=fase_color,
                     fg_color="#1A1A2E", corner_radius=5,
                     padx=5, pady=1).pack(side="left", padx=(8, 2))
        self.lbl_status = ctk.CTkLabel(hdr, text="OFFLINE",
                                        font=("Segoe UI", 8, "bold"), text_color=GRAY)
        self.lbl_status.pack(side="left", padx=4)

        ctk.CTkButton(hdr, text="X", width=24, height=20,
                      corner_radius=5, fg_color="#2A0A0A", hover_color="#550000",
                      font=("Segoe UI", 10, "bold"), text_color="#FF6666",
                      command=lambda: self.on_delete(self.trader["id"])
                      ).pack(side="right", padx=(0, 4))
        self.lbl_trading_days = ctk.CTkLabel(hdr, text="📅 0 gg",
                                              font=("Segoe UI", 8, "bold"), text_color=GOLD)
        self.lbl_trading_days.pack(side="right", padx=4)

        # TIMBRO PASSATA/BRUCIATA + bottone ARCHIVIA
        self._stamp_row = ctk.CTkFrame(self, fg_color="transparent")
        self._stamp_row.pack(fill="x", padx=10, pady=(2, 0))
        self.lbl_stamp = ctk.CTkLabel(self._stamp_row, text="",
                                       font=("Segoe UI", 13, "bold"), text_color=GREEN)
        self.lbl_stamp.pack(side="left")
        self._btn_archivia = ctk.CTkButton(
            self._stamp_row, text="📁", width=36, height=26,
            corner_radius=6, fg_color="#0D2B1F", hover_color="#1A4A2E",
            font=("Segoe UI", 10, "bold"), text_color=GREEN,
            command=lambda: self._do_archive(getattr(self,"_current_stamp","MANUALE")))
        # non mostrato finché non c'è uno stamp

        # SALDI
        saldi = ctk.CTkFrame(self, fg_color="transparent")
        saldi.pack(fill="x", padx=10, pady=(4, 2))
        saldi.columnconfigure(0, weight=1)
        saldi.columnconfigure(1, weight=1)

        bp = ctk.CTkFrame(saldi, fg_color=CARD2, corner_radius=8,
                          border_width=1, border_color=BORDER)
        bp.grid(row=0, column=0, padx=(0, 4), sticky="nsew")
        _bp_top = ctk.CTkFrame(bp, fg_color="transparent")
        _bp_top.pack(fill="x", padx=6, pady=(6, 0))
        ctk.CTkLabel(_bp_top, text="PROP", font=("Segoe UI", 11, "bold"), text_color=GREEN).pack(side="left")
        self.lbl_spread_prop = ctk.CTkLabel(_bp_top, text="●", font=("Segoe UI", 11), text_color=GRAY)
        self.lbl_spread_prop.pack(side="right")
        self.lbl_prop_bal = ctk.CTkLabel(bp, text="---", font=("Segoe UI", 15, "bold"), text_color=WHITE)
        self.lbl_prop_bal.pack()
        self.lbl_prop_pnl = ctk.CTkLabel(bp, text="---", font=("Segoe UI", 11, "bold"), text_color=GRAY)
        self.lbl_prop_pnl.pack(pady=(0, 6))

        bh = ctk.CTkFrame(saldi, fg_color=CARD2, corner_radius=8,
                          border_width=1, border_color=BORDER)
        bh.grid(row=0, column=1, padx=(4, 0), sticky="nsew")
        _bh_top = ctk.CTkFrame(bh, fg_color="transparent")
        _bh_top.pack(fill="x", padx=6, pady=(6, 0))
        ctk.CTkLabel(_bh_top, text="HEDGE", font=("Segoe UI", 11, "bold"), text_color=BLUE).pack(side="left")
        self.lbl_spread_hedge = ctk.CTkLabel(_bh_top, text="●", font=("Segoe UI", 11), text_color=GRAY)
        self.lbl_spread_hedge.pack(side="right")
        self.lbl_hedge_bal = ctk.CTkLabel(bh, text="---", font=("Segoe UI", 15, "bold"), text_color=WHITE)
        self.lbl_hedge_bal.pack()
        self.lbl_hedge_pnl = ctk.CTkLabel(bh, text="---", font=("Segoe UI", 11, "bold"), text_color=GRAY)
        self.lbl_hedge_pnl.pack(pady=(0, 6))

        # NETTO + LOTTI
        netto_row = ctk.CTkFrame(self, fg_color="transparent")
        netto_row.pack(fill="x", padx=10, pady=(2, 0))
        self.lbl_netto = ctk.CTkLabel(netto_row, text="HEDGE P&L: ---  |  OPS: 0",
                                       font=("Segoe UI", 12, "bold"), text_color=GRAY)
        self.lbl_netto.pack(side="left")
        ctk.CTkButton(netto_row, text="↺ RESET CUM", width=90, height=22,
                      fg_color=CARD2, hover_color=BORDER,
                      font=("Segoe UI", 10), text_color=GRAY,
                      command=self._reset_hedge_cum).pack(side="right", padx=(4, 0))
        self.lbl_lotti = ctk.CTkLabel(self, text="Lotto PROP: ---  |  HEDGE: ---",
                                       font=("Segoe UI", 11), text_color=BLUE)
        self.lbl_lotti.pack(pady=(0, 2))

        # ── BOOST 2x toggle ──────────────────────────────────────
        boost_f = ctk.CTkFrame(self, fg_color=CARD2, corner_radius=8,
                                border_width=1, border_color=BORDER)
        boost_f.pack(fill="x", padx=10, pady=(0, 4))
        boost_inner = ctk.CTkFrame(boost_f, fg_color="transparent")
        boost_inner.pack(fill="x", padx=8, pady=6)
        ctk.CTkLabel(boost_inner, text="BOOST 2x",
                     font=("Segoe UI", 11, "bold"), text_color=GOLD,
                     width=70, anchor="w").pack(side="left")
        self._boost_var = ctk.BooleanVar(value=False)
        self._boost_switch = ctk.CTkSwitch(
            boost_inner, text="", variable=self._boost_var,
            width=46, command=self._on_boost_change,
            onvalue=True, offvalue=False)
        self._boost_switch.pack(side="left", padx=(4, 8))
        self.lbl_boost = ctk.CTkLabel(boost_inner, text="OFF",
                                       font=("Segoe UI", 11, "bold"), text_color=GRAY)
        self.lbl_boost.pack(side="left")
        self.lbl_boost_lotti = ctk.CTkLabel(boost_inner, text="",
                                             font=("Segoe UI", 11), text_color=GOLD)
        self.lbl_boost_lotti.pack(side="right", padx=4)

        # DRAWDOWN con barre
        dd = ctk.CTkFrame(self, fg_color=CARD2, corner_radius=8,
                          border_width=1, border_color=BORDER)
        dd.pack(fill="x", padx=10, pady=2)

        def dd_bar_row(parent, title, bar_color):
            f = ctk.CTkFrame(parent, fg_color="transparent")
            f.pack(fill="x", padx=8, pady=3)
            hdr = ctk.CTkFrame(f, fg_color="transparent")
            hdr.pack(fill="x")
            lbl_title = ctk.CTkLabel(hdr, text=title, font=("Segoe UI", 12, "bold"),
                                      text_color=GRAY)
            lbl_title.pack(side="left")
            lbl_pct = ctk.CTkLabel(hdr, text="---", font=("Segoe UI", 12, "bold"),
                                    text_color=bar_color)
            lbl_pct.pack(side="left", padx=6)
            lbl_rem = ctk.CTkLabel(hdr, text="---", font=("Segoe UI", 12),
                                    text_color=GRAY)
            lbl_rem.pack(side="right")
            bar = ctk.CTkProgressBar(f, height=13, corner_radius=6,
                                      fg_color="#1A2A3A",
                                      progress_color=bar_color)
            bar.set(0)
            bar.pack(fill="x", pady=(3, 0))
            return lbl_pct, lbl_rem, bar

        self.lbl_dd_day_pct, self.lbl_dd_day_rem, self.bar_dd_day = dd_bar_row(dd, "DD DAILY",  "#d4af37")
        self.lbl_dd_tot_pct, self.lbl_dd_tot_rem, self.bar_dd_tot = dd_bar_row(dd, "DD TOTALE", "#FF3355")
        self.lbl_tgt_pct,    self.lbl_tgt_rem,    self.bar_tgt    = dd_bar_row(dd, "TARGET",    "#00FF88")

        # STATUS MSG
        self.lbl_msg = ctk.CTkLabel(self, text="",
                                     font=("Segoe UI", 11, "bold"), text_color=GOLD)
        self.lbl_msg.pack(pady=(2, 0))

        # BOTTONI TRADE
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=10, pady=(2, 8))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        btns.columnconfigure(2, weight=0)

        ctk.CTkButton(btns, text="BUY PROP",
                      fg_color="#0A3D1F", hover_color="#145214",
                      text_color=GREEN, font=("Segoe UI", 13, "bold"),
                      height=42, corner_radius=10,
                      command=self.arbexa_buy
                      ).grid(row=0, column=0, padx=(0, 3), sticky="ew")
        ctk.CTkButton(btns, text="SELL PROP",
                      fg_color="#3D0A0A", hover_color="#6B1515",
                      text_color=RED, font=("Segoe UI", 13, "bold"),
                      height=42, corner_radius=10,
                      command=self.arbexa_sell
                      ).grid(row=0, column=1, padx=(3, 3), sticky="ew")
        ctk.CTkButton(btns, text="PANIC",
                      fg_color="#5C0000", hover_color="#8B0000",
                      text_color="#FF4444", font=("Segoe UI", 12, "bold"),
                      width=70, height=42, corner_radius=10,
                      command=self.panic_trader
                      ).grid(row=0, column=2)

    def _browse(self, entry):
        # Apre selettore con auto-detect MT5 + opzione sfoglia manuale
        pick_mt5_path(self.winfo_toplevel(), entry, title="Seleziona terminal64.exe")

    def _salva_path(self, key, entry):
        val = entry.get().strip()
        self.trader[key] = val
        traders = get_traders()
        for i, t in enumerate(traders):
            if t["id"] == self.trader["id"]:
                traders[i][key] = val
                break
        save_traders(traders)
        self.set_msg("Percorso salvato!", GREEN)

    def set_msg(self, msg, color=GOLD):
        try:
            self.lbl_msg.configure(text=msg, text_color=color)
        except: pass
        try:
            _color_map = {GREEN: "green", RED: "red", GOLD: "gold", GRAY: "gray", WHITE: "white"}
            self._last_status_msg   = msg
            self._last_status_color = _color_map.get(color, "gold")
            self._last_status_ts    = datetime.now().strftime("%H:%M:%S")
        except: pass

    def _calc_dynamic_ratio(self, auto_on=None, boost_on=None):
        """Calcola ratio dinamico in base alla distanza dal target. Ritorna (sl_p, tp_p, sl_h, tp_h)."""
        t = self.trader

        # Se i valori non sono passati (chiamata dal heartbeat), leggi dai BooleanVar
        if auto_on is None:
            _av = getattr(self, '_auto_ratio_var', None)
            try: auto_on = bool(_av.get()) if _av else True
            except: auto_on = True
        if boost_on is None:
            _bv = getattr(self, '_boost_var', None)
            try: boost_on = bool(_bv.get()) if _bv else False
            except: boost_on = False

        # Se auto ratio è disabilitato, usa il ratio manuale
        if not auto_on:
            rp = t.get("ratio_prop", "1:3").split(":")
            rh = t.get("ratio_hedge", "3:1").split(":")
            try:
                sl_p, tp_p = int(rp[0]), int(rp[1])
                sl_h, tp_h = int(rh[0]), int(rh[1])
            except:
                sl_p, tp_p, sl_h, tp_h = 1, 3, 3, 1
            return sl_p, tp_p, sl_h, tp_h
        prop = getattr(self, '_last_prop', None)
        size      = float(t.get("size", 0) or 0)
        target_pct = float(t.get("target_pct", 10) or 10)
        target_bal = size * (1 + target_pct / 100) if size > 0 else 0

        # Distanza % rimanente al target (0% = al target, 100% = all'inizio)
        if prop and size > 0 and target_bal > size:
            bal = prop["balance"]
            dist_pct = max(0.0, min(1.0, (target_bal - bal) / (target_bal - size)))
        else:
            dist_pct = 1.0  # default: lontano dal target

        # Ratio dinamico in base alla distanza
        if dist_pct > 0.66:
            ratio_str = "1:4"   # lontano → aggressivo
        elif dist_pct > 0.33:
            ratio_str = "1:3"   # metà strada → neutro
        elif dist_pct > 0.10:
            ratio_str = "1:2"   # vicino → conservativo
        else:
            ratio_str = "1:1"   # quasi arrivato → ultra conservativo

        rp = ratio_str.split(":")
        sl_p, tp_p = int(rp[0]), int(rp[1])
        # Hedge è sempre l'inverso
        sl_h, tp_h = tp_p, sl_p

        # Cap al target — TP della PROP non deve far sforare il target
        if prop and size > 0 and target_bal > 0:
            bal        = prop["balance"]
            manca      = max(target_bal - bal, 0)
            lp         = getattr(self, '_lotto_prop', 0.01) or 0.01
            distanza   = int(t.get("distanza", 1000) or 1000)
            # Valore punto per lotto dalla funzione calcola_lotto (approssimato)
            # punti_tp = distanza × tp_p
            punti_tp = distanza * tp_p
            # Calcola valore per punto in modo approssimato: manca / punti_max = $/punto
            # punti_max = manca / (lp × valor_punto) — usiamo 1$/punto come fallback sicuro
            # Preferiamo non sforare mai → calcoliamo punti_max con valore punto attuale
            val_punto = getattr(self, '_val_punto_prop', None)
            if val_punto and val_punto > 0 and manca > 0:
                punti_max = int(manca / (lp * val_punto))
                if punti_max > 0 and punti_tp > punti_max:
                    tp_p = max(1, punti_max // distanza) if distanza > 0 else 1

        self.after(0, lambda rstr=ratio_str, dp=dist_pct: self.set_msg(
            "Ratio auto: {} (dist {:.0f}%)".format(rstr, dp*100), GOLD))

        # *** BOOST 2x: gestito nel lottaggio, non nei ratio ***

        return sl_p, tp_p, sl_h, tp_h

    def _on_boost_change(self):
        on = self._boost_var.get()
        try:
            self.lbl_boost.configure(
                text="ON 🚀" if on else "OFF",
                text_color=GOLD if on else GRAY)
            if on:
                lp = getattr(self, '_lotto_prop',  0.01) or 0.01
                lh = getattr(self, '_lotto_hedge', 0.01) or 0.01
                self.lbl_boost_lotti.configure(
                    text="→ PROP: {}  HEDGE: {}".format(round(lp*2,2), round(lh*2,2)))
            else:
                self.lbl_boost_lotti.configure(text="")
        except: pass

    def arbexa_buy(self):
        _auto_on  = bool(self.trader.get("auto_ratio", True))
        _boost_on = self._boost_var.get() if self._boost_var else False
        def task():
            t  = self.trader
            if getattr(self, '_dd_blocked', False):
                self.after(0, lambda: self.set_msg("⛔ DD STOP - Bloccato fino a mezzanotte!", RED))
                return
            # Check ordine in corso CON TIMEOUT: se il flag è bloccato da >30s,
            # è sicuro un bug residuo — auto-reset
            if getattr(self, '_order_in_progress', False):
                _started = getattr(self, '_order_started_ts', 0)
                if _started and (time.time() - _started) > 30:
                    # Auto-unlock: il lock è "vecchio", probabilmente bug
                    self._order_in_progress = False
                else:
                    self.after(0, lambda: self.set_msg("⛔ Ordine già in corso!", RED))
                    return
            pp = t.get("broker_prop_path",  "")
            hp = t.get("broker_hedge_path", "")
            if not pp or not hp:
                self.after(0, lambda: self.set_msg("⛔ Path broker non impostato!", RED))
                return
            # Check posizioni live direttamente da MT5
            pos_live = _conta_posizioni_live(pp)
            if pos_live > 0:
                self.after(0, lambda: self.set_msg("⛔ Posizione già aperta su PROP!", RED))
                return
            if pos_live == -1 and int(t.get("positions", 0)) > 0:
                self.after(0, lambda: self.set_msg("⛔ Posizione già aperta su questa scheda!", RED))
                return
            # Calcola parametri per la preview
            sl_p, tp_p, sl_h, tp_h = self._calc_dynamic_ratio(_auto_on, _boost_on)
            lp = getattr(self, '_lotto_prop',  0.01)
            lh = getattr(self, '_lotto_hedge', 0.01)
            if lp <= 0: lp = 0.01
            if lh <= 0: lh = 0.01
            if _boost_on:
                lp = round(lp * 2, 2)
                lh = round(lh * 2, 2)
            # Lock immediato per evitare doppi click
            self._order_in_progress = True
            self._order_started_ts = time.time()
            # Mostra preview e attendi conferma
            confirmed = [False]
            _preview_event = __import__('threading').Event()
            # Valori reali in dollari dai parametri
            stop_prop    = float(t.get("stop_prop", 1000) or 1000)
            target_hedge = float(t.get("target_hedge", 100) or 100)
            boost_mult = 2.0 if _boost_on else 1.0
            sl_p_usd = round(stop_prop * boost_mult, 0)
            tp_p_usd = round(stop_prop * boost_mult * tp_p / sl_p, 0) if sl_p else stop_prop * boost_mult
            tp_h_usd = round(target_hedge * boost_mult, 0)
            sl_h_usd = round(target_hedge * boost_mult * sl_h / tp_h, 0) if tp_h else target_hedge * boost_mult
            ratio_str = "{}:{}".format(sl_p, tp_p)
            def show_preview():
                win = ctk.CTkToplevel()
                win.title("Conferma Ordine")
                win.geometry("400x410")
                win.configure(fg_color="#0a0f1a")
                win.grab_set()
                win.lift()
                win.attributes("-topmost", True)
                ctk.CTkLabel(win, text="▲ CONFERMA BUY", font=("Segoe UI", 15, "bold"),
                             text_color=GREEN).pack(pady=(18, 2))
                ctk.CTkLabel(win, text=t.get("prop_firm", "Scheda"), font=("Segoe UI", 11),
                             text_color=GRAY).pack(pady=(0, 2))
                ctk.CTkLabel(win, text="Ratio: {}".format(ratio_str), font=("Segoe UI", 11, "bold"),
                             text_color=GOLD).pack(pady=(0, 10))
                frame = ctk.CTkFrame(win, fg_color="#0D1F3C", corner_radius=10)
                frame.pack(fill="x", padx=20, pady=4)
                rows = [
                    ("PROP — Lotti",  f"{lp:.2f}",            GREEN),
                    ("PROP — SL",     f"${sl_p_usd:.0f}",      "#FF6B6B"),
                    ("PROP — TP",     f"${tp_p_usd:.0f}",      GREEN),
                    ("HEDGE — Lotti", f"{lh:.2f}",              "#4fc3f7"),
                    ("HEDGE — TP",    f"${tp_h_usd:.0f}",      GREEN),
                    ("HEDGE — SL",    f"${sl_h_usd:.0f}",      "#FF6B6B"),
                ]
                for label, value, col in rows:
                    row = ctk.CTkFrame(frame, fg_color="transparent")
                    row.pack(fill="x", padx=14, pady=4)
                    ctk.CTkLabel(row, text=label, font=("Segoe UI", 10),
                                 text_color=GRAY, anchor="w").pack(side="left")
                    ctk.CTkLabel(row, text=value, font=("Segoe UI", 10, "bold"),
                                 text_color=col, anchor="e").pack(side="right")
                btns = ctk.CTkFrame(win, fg_color="transparent")
                btns.pack(fill="x", padx=20, pady=16)
                def confirm():
                    confirmed[0] = True
                    _preview_event.set()
                    win.destroy()
                def cancel():
                    win.destroy()
                    _preview_event.set()
                ctk.CTkButton(btns, text="✅ CONFERMA", fg_color="#0A3D1F", text_color=GREEN,
                              hover_color="#145214", font=("Segoe UI", 12, "bold"),
                              height=40, command=confirm).pack(side="left", expand=True, fill="x", padx=(0,6))
                ctk.CTkButton(btns, text="✕ ANNULLA", fg_color="#3D0A0A", text_color=RED,
                              hover_color="#6B1515", font=("Segoe UI", 12, "bold"),
                              height=40, command=cancel).pack(side="right", expand=True, fill="x", padx=(6,0))
                win.wait_window()
            self.after(0, show_preview)
            # Aspetta che la finestra sia chiusa (max 60s)
            import time as _time
            _preview_event.wait(timeout=120)
            if not confirmed[0]:
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("Ordine annullato", GRAY))
                return
            self.after(0, lambda: self.set_msg("Esecuzione BUY...", GOLD))
            # ── STEP 1: apri PROP ────────────────────────────────
            ok_p, msg_p = False, ""
            for _ in range(3):
                ok_p, msg_p = invia_ordine(pp, t["simbolo_prop"], mt5.ORDER_TYPE_BUY, lp, t["distanza"], True, sl_p, tp_p)
                if ok_p: break
                time.sleep(0.5)
            if not ok_p:
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("⛔ PROP fallita: " + msg_p, RED))
                return
            # ── STEP 2: apri HEDGE — se fallisce chiudi subito PROP ──
            ok_h, msg_h = False, ""
            for _ in range(3):
                ok_h, msg_h = invia_ordine(hp, t["simbolo_hedge"], mt5.ORDER_TYPE_SELL, lh, t["distanza"], False, sl_h, tp_h)
                if ok_h: break
                time.sleep(0.5)
            if not ok_h:
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("⛔ HEDGE fallita - annullo PROP...", RED))
                chiudi_posizioni(pp)
                self.after(0, lambda: self.set_msg("⛔ HEDGE fallita: " + msg_h + " — PROP annullata", RED))
                return
            self._order_in_progress = False
            log("ORDINE BUY | scheda=" + str(t.get("prop_firm","?")) + " | lp=" + str(lp) + " | lh=" + str(lh))
            self.after(0, lambda: self.set_msg("🔥 FUOCO! BUY PROP / SELL HEDGE", GREEN))
        threading.Thread(target=task, daemon=True).start()

    def arbexa_sell(self):
        _auto_on  = bool(self.trader.get("auto_ratio", True))
        _boost_on = self._boost_var.get() if self._boost_var else False
        def task():
            t  = self.trader
            if getattr(self, '_dd_blocked', False):
                self.after(0, lambda: self.set_msg("⛔ DD STOP - Bloccato fino a mezzanotte!", RED))
                return
            # Check ordine in corso CON TIMEOUT: se il flag è bloccato da >30s,
            # è sicuro un bug residuo — auto-reset
            if getattr(self, '_order_in_progress', False):
                _started = getattr(self, '_order_started_ts', 0)
                if _started and (time.time() - _started) > 30:
                    # Auto-unlock: il lock è "vecchio", probabilmente bug
                    self._order_in_progress = False
                else:
                    self.after(0, lambda: self.set_msg("⛔ Ordine già in corso!", RED))
                    return
            pp = t.get("broker_prop_path",  "")
            hp = t.get("broker_hedge_path", "")
            if not pp or not hp:
                self.after(0, lambda: self.set_msg("⛔ Path broker non impostato!", RED))
                return
            # Check posizioni live direttamente da MT5
            pos_live = _conta_posizioni_live(pp)
            if pos_live > 0:
                self.after(0, lambda: self.set_msg("⛔ Posizione già aperta su PROP!", RED))
                return
            if pos_live == -1 and int(t.get("positions", 0)) > 0:
                self.after(0, lambda: self.set_msg("⛔ Posizione già aperta su questa scheda!", RED))
                return
            # Calcola parametri per la preview
            sl_p, tp_p, sl_h, tp_h = self._calc_dynamic_ratio(_auto_on, _boost_on)
            lp = getattr(self, '_lotto_prop',  0.01)
            lh = getattr(self, '_lotto_hedge', 0.01)
            if lp <= 0: lp = 0.01
            if lh <= 0: lh = 0.01
            if _boost_on:
                lp = round(lp * 2, 2)
                lh = round(lh * 2, 2)
            # Lock immediato per evitare doppi click
            self._order_in_progress = True
            self._order_started_ts = time.time()
            # Mostra preview e attendi conferma
            confirmed = [False]
            _preview_event = __import__('threading').Event()
            # Valori reali in dollari dai parametri
            stop_prop    = float(t.get("stop_prop", 1000) or 1000)
            target_hedge = float(t.get("target_hedge", 100) or 100)
            boost_mult = 2.0 if _boost_on else 1.0
            sl_p_usd = round(stop_prop * boost_mult, 0)
            tp_p_usd = round(stop_prop * boost_mult * tp_p / sl_p, 0) if sl_p else stop_prop * boost_mult
            tp_h_usd = round(target_hedge * boost_mult, 0)
            sl_h_usd = round(target_hedge * boost_mult * sl_h / tp_h, 0) if tp_h else target_hedge * boost_mult
            ratio_str = "{}:{}".format(sl_p, tp_p)
            def show_preview():
                win = ctk.CTkToplevel()
                win.title("Conferma Ordine")
                win.geometry("400x410")
                win.configure(fg_color="#0a0f1a")
                win.grab_set()
                win.lift()
                win.attributes("-topmost", True)
                ctk.CTkLabel(win, text="▼ CONFERMA SELL", font=("Segoe UI", 15, "bold"),
                             text_color=RED).pack(pady=(18, 2))
                ctk.CTkLabel(win, text=t.get("prop_firm", "Scheda"), font=("Segoe UI", 11),
                             text_color=GRAY).pack(pady=(0, 2))
                ctk.CTkLabel(win, text="Ratio: {}".format(ratio_str), font=("Segoe UI", 11, "bold"),
                             text_color=GOLD).pack(pady=(0, 10))
                frame = ctk.CTkFrame(win, fg_color="#0D1F3C", corner_radius=10)
                frame.pack(fill="x", padx=20, pady=4)
                rows = [
                    ("PROP — Lotti",  f"{lp:.2f}",            RED),
                    ("PROP — SL",     f"${sl_p_usd:.0f}",      "#FF6B6B"),
                    ("PROP — TP",     f"${tp_p_usd:.0f}",      GREEN),
                    ("HEDGE — Lotti", f"{lh:.2f}",              "#4fc3f7"),
                    ("HEDGE — TP",    f"${tp_h_usd:.0f}",      GREEN),
                    ("HEDGE — SL",    f"${sl_h_usd:.0f}",      "#FF6B6B"),
                ]
                for label, value, col in rows:
                    row = ctk.CTkFrame(frame, fg_color="transparent")
                    row.pack(fill="x", padx=14, pady=4)
                    ctk.CTkLabel(row, text=label, font=("Segoe UI", 10),
                                 text_color=GRAY, anchor="w").pack(side="left")
                    ctk.CTkLabel(row, text=value, font=("Segoe UI", 10, "bold"),
                                 text_color=col, anchor="e").pack(side="right")
                btns = ctk.CTkFrame(win, fg_color="transparent")
                btns.pack(fill="x", padx=20, pady=16)
                def confirm():
                    confirmed[0] = True
                    _preview_event.set()
                    win.destroy()
                def cancel():
                    win.destroy()
                    _preview_event.set()
                ctk.CTkButton(btns, text="✅ CONFERMA", fg_color="#0A3D1F", text_color=GREEN,
                              hover_color="#145214", font=("Segoe UI", 12, "bold"),
                              height=40, command=confirm).pack(side="left", expand=True, fill="x", padx=(0,6))
                ctk.CTkButton(btns, text="✕ ANNULLA", fg_color="#3D0A0A", text_color=RED,
                              hover_color="#6B1515", font=("Segoe UI", 12, "bold"),
                              height=40, command=cancel).pack(side="right", expand=True, fill="x", padx=(6,0))
                win.wait_window()
            self.after(0, show_preview)
            import time as _time
            _preview_event.wait(timeout=120)
            if not confirmed[0]:
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("Ordine annullato", GRAY))
                return
            self.after(0, lambda: self.set_msg("Esecuzione SELL...", GOLD))
            # ── STEP 1: apri PROP ────────────────────────────────
            ok_p, msg_p = False, ""
            for _ in range(3):
                ok_p, msg_p = invia_ordine(pp, t["simbolo_prop"], mt5.ORDER_TYPE_SELL, lp, t["distanza"], True, sl_p, tp_p)
                if ok_p: break
                time.sleep(0.5)
            if not ok_p:
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("⛔ PROP fallita: " + msg_p, RED))
                return
            # ── STEP 2: apri HEDGE — se fallisce chiudi subito PROP ──
            ok_h, msg_h = False, ""
            for _ in range(3):
                ok_h, msg_h = invia_ordine(hp, t["simbolo_hedge"], mt5.ORDER_TYPE_BUY, lh, t["distanza"], False, sl_h, tp_h)
                if ok_h: break
                time.sleep(0.5)
            if not ok_h:
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("⛔ HEDGE fallita - annullo PROP...", RED))
                chiudi_posizioni(pp)
                self.after(0, lambda: self.set_msg("⛔ HEDGE fallita: " + msg_h + " — PROP annullata", RED))
                return
            self._order_in_progress = False
            log("ORDINE SELL | scheda=" + str(t.get("prop_firm","?")) + " | lp=" + str(lp) + " | lh=" + str(lh))
            self.after(0, lambda: self.set_msg("🔥 FUOCO! SELL PROP / BUY HEDGE", GREEN))
        threading.Thread(target=task, daemon=True).start()

    def _execute_buy_direct(self):
        """BUY diretto senza preview per comandi remoti."""
        t = self.trader
        if getattr(self, '_dd_blocked', False):
            self.after(0, lambda: self.set_msg("⛔ DD STOP!", RED)); return
        # Check ordine in corso CON TIMEOUT: se bloccato da >30s, auto-reset
        if getattr(self, '_order_in_progress', False):
            _started = getattr(self, '_order_started_ts', 0)
            if _started and (time.time() - _started) > 30:
                self._order_in_progress = False
            else:
                self.after(0, lambda: self.set_msg("⛔ Ordine già in corso!", RED)); return
        pp = t.get("broker_prop_path", "")
        hp = t.get("broker_hedge_path", "")
        if not pp or not hp:
            self.after(0, lambda: self.set_msg("⛔ Path broker non impostato!", RED)); return
        # Check posizioni LIVE da MT5 (non da cache)
        pos_live = _conta_posizioni_live(pp)
        if pos_live > 0:
            self.after(0, lambda: self.set_msg("⛔ Posizione già aperta su PROP!", RED)); return
        self._order_in_progress = True
        self._order_started_ts = time.time()
        try:
            _auto_on  = bool(t.get("auto_ratio", True))
            _boost_on = False
            try: _boost_on = bool(self._boost_var.get())
            except: pass
            self.after(0, lambda: self.set_msg("📡 BUY REMOTO...", GOLD))
            sl_p, tp_p, sl_h, tp_h = self._calc_dynamic_ratio(_auto_on, _boost_on)
            lp = max(getattr(self, '_lotto_prop',  0.01) or 0.01, 0.01)
            lh = max(getattr(self, '_lotto_hedge', 0.01) or 0.01, 0.01)
            if _boost_on:
                lp = round(lp * 2, 2)
                lh = round(lh * 2, 2)
            distanza = int(t.get("distanza", 1000) or 1000)
            sim_p = t.get("simbolo_prop",  "XAUUSD") or "XAUUSD"
            sim_h = t.get("simbolo_hedge", "XAUUSD") or "XAUUSD"
            ok_p, msg_p = False, ""
            for _ in range(3):
                ok_p, msg_p = invia_ordine(pp, sim_p, mt5.ORDER_TYPE_BUY, lp, distanza, True, sl_p, tp_p)
                if ok_p: break
                time.sleep(0.5)
            if not ok_p:
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("⛔ PROP fallita: " + msg_p, RED))
                return
            ok_h, msg_h = False, ""
            for _ in range(3):
                ok_h, msg_h = invia_ordine(hp, sim_h, mt5.ORDER_TYPE_SELL, lh, distanza, False, sl_h, tp_h)
                if ok_h: break
                time.sleep(0.5)
            if not ok_h:
                chiudi_posizioni(pp)
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("⛔ HEDGE fallita: " + msg_h, RED))
            else:
                self._order_in_progress = False
                log("BUY REMOTO eseguito | scheda=" + str(t.get("prop_firm","?")))
                self.after(0, lambda: self.set_msg("✅ BUY REMOTO eseguito!", GREEN))
        except Exception as e:
            self._order_in_progress = False
            self.after(0, lambda: self.set_msg("⛔ Errore: " + str(e), RED))

    def _execute_sell_direct(self):
        """SELL diretto senza preview per comandi remoti."""
        t = self.trader
        if getattr(self, '_dd_blocked', False):
            self.after(0, lambda: self.set_msg("⛔ DD STOP!", RED)); return
        # Check ordine in corso CON TIMEOUT: se bloccato da >30s, auto-reset
        if getattr(self, '_order_in_progress', False):
            _started = getattr(self, '_order_started_ts', 0)
            if _started and (time.time() - _started) > 30:
                self._order_in_progress = False
            else:
                self.after(0, lambda: self.set_msg("⛔ Ordine già in corso!", RED)); return
        pp = t.get("broker_prop_path", "")
        hp = t.get("broker_hedge_path", "")
        if not pp or not hp:
            self.after(0, lambda: self.set_msg("⛔ Path broker non impostato!", RED)); return
        # Check posizioni LIVE da MT5 (non da cache)
        pos_live = _conta_posizioni_live(pp)
        if pos_live > 0:
            self.after(0, lambda: self.set_msg("⛔ Posizione già aperta su PROP!", RED)); return
        self._order_in_progress = True
        self._order_started_ts = time.time()
        try:
            _auto_on  = bool(t.get("auto_ratio", True))
            _boost_on = False
            try: _boost_on = bool(self._boost_var.get())
            except: pass
            self.after(0, lambda: self.set_msg("📡 SELL REMOTO...", GOLD))
            sl_p, tp_p, sl_h, tp_h = self._calc_dynamic_ratio(_auto_on, _boost_on)
            lp = max(getattr(self, '_lotto_prop',  0.01) or 0.01, 0.01)
            lh = max(getattr(self, '_lotto_hedge', 0.01) or 0.01, 0.01)
            if _boost_on:
                lp = round(lp * 2, 2)
                lh = round(lh * 2, 2)
            distanza = int(t.get("distanza", 1000) or 1000)
            sim_p = t.get("simbolo_prop",  "XAUUSD") or "XAUUSD"
            sim_h = t.get("simbolo_hedge", "XAUUSD") or "XAUUSD"
            ok_p, msg_p = False, ""
            for _ in range(3):
                ok_p, msg_p = invia_ordine(pp, sim_p, mt5.ORDER_TYPE_SELL, lp, distanza, True, sl_p, tp_p)
                if ok_p: break
                time.sleep(0.5)
            if not ok_p:
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("⛔ PROP fallita: " + msg_p, RED))
                return
            ok_h, msg_h = False, ""
            for _ in range(3):
                ok_h, msg_h = invia_ordine(hp, sim_h, mt5.ORDER_TYPE_BUY, lh, distanza, False, sl_h, tp_h)
                if ok_h: break
                time.sleep(0.5)
            if not ok_h:
                chiudi_posizioni(pp)
                self._order_in_progress = False
                self.after(0, lambda: self.set_msg("⛔ HEDGE fallita: " + msg_h, RED))
            else:
                self._order_in_progress = False
                log("SELL REMOTO eseguito | scheda=" + str(t.get("prop_firm","?")))
                self.after(0, lambda: self.set_msg("✅ SELL REMOTO eseguito!", GREEN))
        except Exception as e:
            self._order_in_progress = False
            self.after(0, lambda: self.set_msg("⛔ Errore: " + str(e), RED))

    def panic_trader(self):
        # Imposta flag PRIMA della chiusura: il loop salterà la notifica Telegram
        self._panic_just_pressed = True
        # IMPORTANTE: reset del lock ordine in corso, così dopo PANIC
        # si possono inviare nuovi ordini senza dover riavviare l'app
        self._order_in_progress = False
        def task():
            t = self.trader
            self.after(0, lambda: self.set_msg("PANIC...", RED))
            n  = chiudi_posizioni(t.get("broker_prop_path",  ""))
            n += chiudi_posizioni(t.get("broker_hedge_path", ""))
            # Doppio reset post-chiusura per sicurezza
            self._order_in_progress = False
            self.after(0, lambda n=n: self.set_msg("PANIC: {} chiuse".format(n), RED))
        threading.Thread(target=task, daemon=True).start()

    def _show_trades(self):
        """Mostra storico operazioni chiuse."""
        trades = getattr(self, "_trades_history", [])
        win = ctk.CTkToplevel(self.winfo_toplevel())
        win.title("Storico Operazioni")
        win.geometry("580x500")
        win.configure(fg_color=BG)
        win.grab_set()

        ctk.CTkLabel(win, text="📊 STORICO OPERAZIONI",
                     font=("Segoe UI", 13, "bold"), text_color=GOLD).pack(pady=(14,4))

        # Totali separati PROP e HEDGE
        tot_prop  = sum(t.get("profit", 0) for t in trades if t.get("conto") == "PROP")
        tot_hedge = sum(t.get("profit", 0) for t in trades if t.get("conto") == "HEDGE")
        n_prop    = sum(1 for t in trades if t.get("conto") == "PROP")
        n_hedge   = sum(1 for t in trades if t.get("conto") == "HEDGE")

        stat = ctk.CTkFrame(win, fg_color=CARD2, corner_radius=8)
        stat.pack(fill="x", padx=16, pady=(0,8))
        for txt, val in [
            ("PROP ops",    str(n_prop)),
            ("PROP totale", ("+" if tot_prop>=0 else "")+"${:,.2f}".format(tot_prop)),
            ("HEDGE ops",   str(n_hedge)),
            ("HEDGE totale",("+" if tot_hedge>=0 else "")+"${:,.2f}".format(tot_hedge)),
        ]:
            f = ctk.CTkFrame(stat, fg_color="transparent")
            f.pack(side="left", expand=True, pady=8)
            color = (GREEN if val.startswith("+") else RED) if "$" in val else WHITE
            ctk.CTkLabel(f, text=val, font=("Segoe UI", 13, "bold"), text_color=color).pack()
            ctk.CTkLabel(f, text=txt, font=("Segoe UI", 9), text_color=GRAY).pack()

        ctk.CTkFrame(win, fg_color=BORDER, height=1).pack(fill="x", padx=16)

        # Header tabella
        hdr = ctk.CTkFrame(win, fg_color=CARD2, corner_radius=0)
        hdr.pack(fill="x", padx=16, pady=(4,0))
        for txt, w in [("Conto",60),("Data",75),("Apertura",75),("Chiusura",75),("Tipo",45),("Risultato",90)]:
            ctk.CTkLabel(hdr, text=txt, width=w, font=("Segoe UI", 9, "bold"),
                         text_color=GRAY).pack(side="left", padx=4, pady=4)

        sc = ctk.CTkScrollableFrame(win, fg_color="transparent")
        sc.pack(fill="both", expand=True, padx=16, pady=4)

        if not trades:
            ctk.CTkLabel(sc, text="Nessuna operazione chiusa.",
                         font=("Segoe UI", 11), text_color=GRAY).pack(pady=20)
        else:
            for tr in reversed(trades):
                profit = tr.get("profit", 0)
                conto  = tr.get("conto", "—")
                row_bg = "#0A1A0A" if profit >= 0 else "#1A0A0A"
                conto_color = "#4FC3F7" if conto == "HEDGE" else "#d4af37"
                row = ctk.CTkFrame(sc, fg_color=row_bg, corner_radius=4)
                row.pack(fill="x", pady=1)
                for txt, w, col in [
                    (conto,                                   60,  conto_color),
                    (tr.get("date","—"),                      75,  GRAY),
                    (tr.get("open_time","—"),                 75,  GRAY),
                    (tr.get("close_time","—"),                75,  GRAY),
                    (tr.get("type","—"),                      45,  GREEN if tr.get("type")=="BUY" else RED),
                    (("+" if profit>=0 else "")+"${:,.2f}".format(profit), 90, GREEN if profit>=0 else RED),
                ]:
                    ctk.CTkLabel(row, text=txt, width=w, font=("Segoe UI", 10),
                                 text_color=col).pack(side="left", padx=4, pady=3)

        ctk.CTkButton(win, text="✓ CHIUDI", width=140, height=32,
                      fg_color=CARD2, hover_color=BORDER, text_color=WHITE,
                      font=("Segoe UI", 11),
                      command=win.destroy).pack(pady=(4,12))

    def _reset_hedge_cum(self):
        """Reset manuale del cumulativo HEDGE."""
        if messagebox.askyesno("Reset Cumulativo", "Azzerare il cumulativo HEDGE?\nQuesta azione non può essere annullata."):
            self._hedge_pnl_cum = 0.0
            _save_hedge_cum(self._trader_id, 0.0)
            self.set_msg("Cumulativo HEDGE azzerato", GOLD)

    def _do_archive(self, esito, auto=False):
        """Archivia la scheda sul server."""
        if not auto:
            if not messagebox.askyesno("Archivia", "Archiviare questa scheda come {}?".format(esito)):
                return
        def task():
            try:
                cid = get_client_id()
                pw  = get_client_password()
                traders = get_traders()
                idx = next((i for i,t in enumerate(traders) if t.get("id")==self.trader.get("id")), 0)
                payload = json.dumps({"esito": esito}).encode()
                req = urllib.request.Request(
                    SERVER_URL + "/api/archive_manual/{}/{}".format(cid, idx),
                    data=payload,
                    headers={"Content-Type": "application/json",
                             "X-Client-Password": pw or ""},
                    method="POST")
                with urllib.request.urlopen(req, timeout=8) as r:
                    d = json.loads(r.read().decode())
                self._auto_archiving = False
                if d.get("ok"):
                    self.after(0, lambda: self.set_msg("📁 Archiviato — "+esito, GREEN))
                    self.after(1000, self._notify_app_reload)
                else:
                    self.after(0, lambda: self.set_msg("Errore archivio", RED))
            except Exception as e:
                self._auto_archiving = False
                self.after(0, lambda: self.set_msg("Errore: "+str(e)[:40], RED))
        threading.Thread(target=task, daemon=True).start()

    def _notify_app_reload(self):
        """Ricarica le card nell'app principale dopo archiviazione."""
        try:
            app = self.winfo_toplevel()
            if hasattr(app, '_load_cards'):
                app._load_cards()
        except: pass

    def _start_refresh(self):
        self._prev_prop_pos      = -1
        self._prev_hedge_pos     = -1
        self._dd_blocked         = False
        self._dd_block_date      = None
        self._balance_start_day  = None
        self._balance_start_date = None
        self._balance_start_is_fallback = False
        self._trade_count        = 0
        self._last_broker_prop   = self.trader.get("broker_prop_path", "")
        self._last_broker_hedge  = self.trader.get("broker_hedge_path", "")
        _tid_init = self.trader.get("id", "default")
        self._hedge_pnl_cum      = _load_hedge_cum(_tid_init)  # persiste tra riavvii
        self._hedge_lots_cum     = _load_hedge_lots(_tid_init)  # cumulativo lotti hedge
        self._prev_hedge_volume  = -1.0  # -1 = non inizializzato (al primo ciclo viene letto da MT5 senza sommare)
        self._prev_hedge_bal     = None
        self._prev_hedge_pos_c   = -1
        self._session_start_ts   = time.time()
        self._trades_log         = []
        _tid = self.trader.get("id", "default")
        self._trading_days       = _load_trading_days(_tid)
        self._trader_id          = _tid
        self._last_trading_date  = None
        self._trades_history        = _load_trades_history(_tid)
        self._last_scheduled_update = None
        self._had_positions         = False  # per rilevare chiusura posizioni
        self._lotto_ts              = 0      # timestamp cache lotti
        self._lotto_prop         = 0.01
        self._lotto_prop_base    = 0.01   # valore BASE (senza riduzione DD)
        self._val_punto_prop     = None
        self._lotto_hedge        = 0.01
        self._lotto_hedge_base   = 0.01   # valore BASE (senza riduzione DD)
        self._lotto_ts           = 0
        self._dd_just_triggered  = False  # evita doppia chiusura dopo DD STOP
        # --- NUOVO: protezione posizioni orfane ---
        self._first_cycle_done   = False  # primo ciclo dopo avvio: orphan check
        self._unbalanced_count   = 0      # watchdog: cicli consecutivi sbilanciati
        self._orphan_dialog_open = False  # evita popup ripetuti
        # --- NUOVO: warning DD giornaliero al 90% ---
        self._dd_warning_shown   = False  # popup DD 90% mostrato oggi
        self._dd_warning_date    = ""     # data ultimo reset (YYYY-MM-DD)
        self._dd_check_counter   = 0      # contatore cicli per limitare le chiamate
        # --- NUOVO v4.8.6: popup BRUCIATA quando si sfora il LIMITE UFFICIALE prop ---
        self._dd_limit_dialog_open = False  # evita popup ripetuti
        # --- NUOVO: cache P&L giornaliero PROP letto da MT5 ---
        # Aggiornata in thread ogni ~9s. Usata per barra DD daily e warning 90%.
        # Indipendente dal saldo di partenza → robusta al cambio broker.
        self._pnl_day_cache      = None   # None = non ancora letto, float = ultimo valore (PROP)
        self._pnl_day_cache_ts   = 0      # timestamp ultima lettura (per debug)
        self._hedge_pnl_day_cache = 0.0   # P&L giornaliero del conto HEDGE (per bot e mypanel)
        self._hedge_pnl_day_accum = 0.0   # accumulatore daily HEDGE: sommato ad ogni chiusura
        self._hedge_pnl_day_date  = None  # data (YYYY-MM-DD) della giornata corrente
        # Stesso meccanismo per il PROP: traccia P&L giornaliero accumulando i delta_balance
        # ad ogni chiusura PROP. Usato per DD daily e barre, evita la cache stale di MT5.
        self._prev_prop_bal       = None  # balance PROP precedente (per delta)
        self._prev_prop_pos_c     = -1    # posizioni PROP precedenti
        self._prop_pnl_day_accum  = 0.0   # accumulatore daily PROP
        self._prop_pnl_day_date   = None  # data corrente (YYYY-MM-DD)
        # --- NUOVO v4.8.7: contatori operazioni per resoconto Telegram fine giornata ---
        self._day_ops_positive    = 0     # numero operazioni in profit oggi
        self._day_ops_negative    = 0     # numero operazioni in loss oggi
        self._day_profit_pos      = 0.0   # somma profit operazioni positive
        self._day_loss_neg        = 0.0   # somma loss operazioni negative (negativo)
        self._recap_sent_date     = None  # data ultimo resoconto inviato (YYYY-MM-DD)
        # --- NUOVO: flag target raggiunto (chiusura preventiva da equity) ---
        self._target_closing     = False  # True quando target equity raggiunto, evita ri-trigger
        self._panic_just_pressed = False  # True dopo PANIC manuale, evita notifica Telegram
        self._notified_hedge_count = None  # None = baseline non fissata, int = numero notificati

        def loop():
            while self.running:
                t   = self.trader
                pp  = t.get("broker_prop_path",  "")
                hp  = t.get("broker_hedge_path", "")

                # -- Leggi PROP e HEDGE in parallelo --
                _res = [None, None]
                def _get_prop():  _res[0] = get_account_info(pp)
                def _get_hedge(): _res[1] = get_account_info(hp)
                th_p = threading.Thread(target=_get_prop,  daemon=True)
                th_h = threading.Thread(target=_get_hedge, daemon=True)
                th_p.start(); th_h.start()
                th_p.join(timeout=8); th_h.join(timeout=8)
                prop  = _res[0]
                hedge = _res[1]

                # -- Leggi spread PROP e HEDGE (ogni 3s insieme al refresh) --
                _sp = [None, None]
                _sim_p = t.get("simbolo_prop",  "XAUUSD")
                _sim_h = t.get("simbolo_hedge", "XAUUSD")
                def _get_sp(): _sp[0] = get_spread(pp, _sim_p)
                def _get_sh(): _sp[1] = get_spread(hp, _sim_h)
                th_sp = threading.Thread(target=_get_sp, daemon=True)
                th_sh = threading.Thread(target=_get_sh, daemon=True)
                th_sp.start(); th_sh.start()
                th_sp.join(timeout=4); th_sh.join(timeout=4)
                # Salva spread come attributi — aggiornati nell'UI dal main thread
                self._spread_prop  = _sp[0]
                self._spread_hedge = _sp[1]
                today = datetime.now().date()

                # -- Reset blocco DD a mezzanotte --
                if self._dd_blocked and self._dd_block_date != today:
                    self._dd_blocked       = False
                    self._dd_block_date    = None
                    self._dd_just_triggered = False
                    self.after(0, lambda: self.set_msg("Nuovo giorno - DD reset!", GREEN))

                # -- Reset se cambia broker --
                cur_pp = t.get("broker_prop_path", "")
                cur_hp = t.get("broker_hedge_path", "")
                if cur_pp != self._last_broker_prop or cur_hp != self._last_broker_hedge:
                    # Identifica quale è cambiato (skip al primissimo avvio quando _last è vuoto)
                    _was_initial = (not self._last_broker_prop and not self._last_broker_hedge)
                    _changed_pp = (cur_pp != self._last_broker_prop)
                    _changed_hp = (cur_hp != self._last_broker_hedge)
                    # NON azzerare hedge_pnl_cum — persiste sempre
                    # NON azzerare balance_start_day — si azzera solo al cambio giorno
                    self._prev_hedge_bal     = None
                    self._prev_hedge_pos_c   = -1
                    self._trade_count        = 0
                    self._last_broker_prop   = cur_pp
                    self._last_broker_hedge  = cur_hp
                    self._session_start_ts   = time.time()
                    self._trades_log         = []
                    self._notified_hedge_count = None  # reset notifiche Telegram al cambio sessione (None = baseline non ancora fissata)
                    # Reset DD daily PROP/HEDGE: account diverso = stato diverso
                    if not _was_initial:
                        log("RESET DD daily al cambio broker (PROP={} HEDGE={})".format(
                            _changed_pp, _changed_hp), "INFO")
                        self._prop_pnl_day_accum = 0.0
                        self._pnl_day_cache      = None  # forza ricalcolo da MT5
                        self._hedge_pnl_day_accum = 0.0
                        self._hedge_pnl_day_cache = 0.0
                        self._prev_prop_bal      = None
                        self._prev_prop_pos_c    = -1
                    _new_tid = t.get("id", "default")
                    self._trading_days       = _load_trading_days(_new_tid)
                    self._trader_id          = _new_tid
                    self._prev_prop_pos      = -1
                    self._prev_hedge_pos     = -1
                    self._dd_just_triggered  = False
                    self._lotto_ts           = 0
                    self._had_positions      = False
                    self._trades_history     = []
                    # NUOVO: reset orphan/watchdog al cambio broker
                    self._first_cycle_done   = False
                    self._unbalanced_count   = 0
                    self._orphan_dialog_open = False
                    # IMPORTANTE: NON azzerare _hedge_lots_cum al cambio broker.
                    # I lotti sono il contatore di fatturazione e devono persistere
                    # finché l'admin non clicca esplicitamente "RESET LOTTI" (=
                    # tipicamente a pagamento avvenuto).
                    # Resettiamo solo _prev_hedge_volume per evitare di sommare
                    # le posizioni eventualmente già aperte sul nuovo broker come "nuove".
                    self._prev_hedge_volume  = -1.0  # marker "non inizializzato"
                    # NUOVO: reset stato ordini per evitare falsi "Posizione già aperta"
                    self._order_in_progress  = False
                    t["positions"]           = 0
                    # NUOVO: forza disconnessione MT5 per evitare connessioni ibride
                    try: mt5.shutdown()
                    except: pass
                    _save_trades_history(self._trader_id, [])
                    # NUOVO: reset warning DD 90% al cambio broker
                    self._dd_warning_shown   = False
                    # NUOVO: popup avviso cambio broker (solo se NON è il setup iniziale)
                    if not _was_initial:
                        _firm = t.get("prop_firm", "scheda")
                        _which = "PROP e HEDGE" if (_changed_pp and _changed_hp) else \
                                 "PROP" if _changed_pp else "HEDGE"
                        def _ask_broker_changed(_f=_firm, _w=_which):
                            try:
                                messagebox.showwarning(
                                    "⚠ Broker modificato",
                                    "Hai cambiato il broker {} sulla scheda \"{}\".\n\n"
                                    "Prima di operare assicurati di:\n"
                                    "  • Avere MetaTrader 5 aperto e connesso\n"
                                    "  • Aver attivato Algo Trading (tasto verde)\n"
                                    "  • Verificato che il SIMBOLO usato dal broker "
                                    "sia corretto (es. XAUUSD, GOLD, GOLD#)\n"
                                    "  • Controllato i parametri di trading".format(_w, _f))
                            except: pass
                        self.after(0, _ask_broker_changed)

                # === RESET QUOTIDIANO ACCUMULATORS PROP/HEDGE alle 22:00 UTC ===
                # Standard prop firm: la giornata di trading inizia alle 22:00 UTC
                # (= 00:00 italiana inverno, 23:00 italiana estate DST).
                from datetime import datetime as _dt_r, timezone as _tz_r
                _now_utc_r = _dt_r.now(_tz_r.utc)
                _today_key_r = (_now_utc_r - timedelta(hours=22)).strftime("%Y-%m-%d")
                if self._prop_pnl_day_date != _today_key_r:
                    if self._prop_pnl_day_date is not None:
                        log("RESET DD daily PROP (22:00 UTC) | era {} accum={:.2f}".format(
                            self._prop_pnl_day_date, self._prop_pnl_day_accum), "INFO")
                    self._prop_pnl_day_accum = 0.0
                    self._prop_pnl_day_date  = _today_key_r
                    self._pnl_day_cache      = 0.0   # reset cache DD
                if self._hedge_pnl_day_date != _today_key_r:
                    if self._hedge_pnl_day_date is not None:
                        log("RESET DD daily HEDGE (22:00 UTC) | era {} accum={:.2f}".format(
                            self._hedge_pnl_day_date, self._hedge_pnl_day_accum), "INFO")
                        # === NUOVO v4.8.7: Invia resoconto Telegram fine giornata ===
                        # Solo se ci sono state operazioni e non già inviato per oggi
                        _prev_date = self._hedge_pnl_day_date
                        if (self._day_ops_positive + self._day_ops_negative) > 0 and self._recap_sent_date != _prev_date:
                            self._recap_sent_date = _prev_date
                            try:
                                cid_now = get_client_id()
                                firm = t.get("prop_firm", "?")
                                # Snapshot dati per il thread
                                _ops_pos = self._day_ops_positive
                                _ops_neg = self._day_ops_negative
                                _pos_sum = self._day_profit_pos
                                _neg_sum = self._day_loss_neg
                                _net     = self._hedge_pnl_day_accum
                                _prop_bal_n = prop["balance"] if prop else 0
                                _hedge_bal_n = hedge["balance"] if hedge else 0
                                _payload_recap = {
                                    "client_id":  cid_now,
                                    "prop_firm":  firm,
                                    "date":       _prev_date,
                                    "ops_pos":    _ops_pos,
                                    "ops_neg":    _ops_neg,
                                    "profit_pos": round(_pos_sum, 2),
                                    "loss_neg":   round(_neg_sum, 2),
                                    "net":        round(_net, 2),
                                    "prop_bal":   round(_prop_bal_n, 2),
                                    "hedge_bal":  round(_hedge_bal_n, 2),
                                }
                                def _send_recap(_p=_payload_recap):
                                    try:
                                        import urllib.request as _ur
                                        _data = json.dumps(_p).encode()
                                        _req = _ur.Request(SERVER_URL + "/api/notify_daily_recap",
                                                            data=_data, method="POST",
                                                            headers={"Content-Type":"application/json"})
                                        with _ur.urlopen(_req, timeout=5) as _resp:
                                            log("recap HTTP {} | ops={}+{} net={:.2f}".format(
                                                _resp.status, _p["ops_pos"], _p["ops_neg"], _p["net"]), "INFO")
                                    except Exception as _ex:
                                        log("recap POST error: {}".format(_ex), "ERROR")
                                threading.Thread(target=_send_recap, daemon=True).start()
                            except Exception as _e:
                                log("recap build error: " + str(_e), "ERROR")
                    # Reset contatori per la nuova giornata
                    self._day_ops_positive = 0
                    self._day_ops_negative = 0
                    self._day_profit_pos   = 0.0
                    self._day_loss_neg     = 0.0
                    self._hedge_pnl_day_accum = 0.0
                    self._hedge_pnl_day_date  = _today_key_r
                    self._hedge_pnl_day_cache = 0.0  # reset cache HEDGE day

                # === TRACKING P&L GIORNALIERO PROP (via delta_balance) ===
                # Stessa logica usata per HEDGE: accumulo delta ad ogni chiusura PROP.
                # Più affidabile della cache history MT5 che si aggiorna in ritardo.
                if prop:
                    n_p_now = prop["positions"]
                    if self._prev_prop_pos_c > 0 and n_p_now < self._prev_prop_pos_c and self._prev_prop_bal is not None:
                        delta_p = prop["balance"] - self._prev_prop_bal
                        self._prop_pnl_day_accum += delta_p
                        # Aggiorno la cache PROP usata da DD daily e barre
                        self._pnl_day_cache    = self._prop_pnl_day_accum
                        self._pnl_day_cache_ts = time.time()
                        log("PROP close | delta={:.2f} accum={:.2f}".format(
                            delta_p, self._prop_pnl_day_accum), "INFO")
                    self._prev_prop_bal   = prop["balance"]
                    self._prev_prop_pos_c = n_p_now

                # -- Aggiorna P&L cumulativo HEDGE --
                if hedge:
                    n_h = hedge["positions"]
                    # Log SEMPRE quando il numero di posizioni cambia (apertura o chiusura)
                    if n_h != self._prev_hedge_pos_c:
                        log("HEDGE pos change | from {} to {} | bal {:.2f} -> {:.2f}".format(
                            self._prev_hedge_pos_c, n_h,
                            self._prev_hedge_bal if self._prev_hedge_bal else 0,
                            hedge["balance"]), "INFO")
                    if self._prev_hedge_pos_c > 0 and n_h < self._prev_hedge_pos_c and self._prev_hedge_bal is not None:
                        delta = hedge["balance"] - self._prev_hedge_bal
                        self._hedge_pnl_cum += delta
                        _save_hedge_cum(self._trader_id, self._hedge_pnl_cum)
                        # Accumulo P&L giornaliero HEDGE (reset 22:00 UTC gestito globalmente sopra)
                        self._hedge_pnl_day_accum += delta
                        self._hedge_pnl_day_cache = self._hedge_pnl_day_accum
                        # === NUOVO v4.8.7: Contatori operazioni per resoconto giornaliero ===
                        num_closed_now = self._prev_hedge_pos_c - n_h
                        profit_pp = delta / num_closed_now if num_closed_now > 0 else delta
                        for _ in range(num_closed_now):
                            if profit_pp >= 0:
                                self._day_ops_positive += 1
                                self._day_profit_pos   += profit_pp
                            else:
                                self._day_ops_negative += 1
                                self._day_loss_neg     += profit_pp  # negativo
                        # Calcolo lotti chiusi (volume "scomparso" tra ciclo precedente e attuale)
                        cur_vol_now = float(hedge.get("total_volume", 0) or 0)
                        prev_vol_for_close = getattr(self, "_prev_hedge_volume", cur_vol_now)
                        if prev_vol_for_close < 0:
                            prev_vol_for_close = cur_vol_now
                        lots_closed = max(prev_vol_for_close - cur_vol_now, 0.0)
                        # === NOTIFICA TELEGRAM CHIUSURA HEDGE ===
                        # NON inviata se chiusura PANIC (evita spam quando chiudi tutto in emergenza).
                        if not getattr(self, "_panic_just_pressed", False):
                            try:
                                cid_now = get_client_id()
                                firm = t.get("prop_firm", "?")
                                sym  = t.get("simbolo_hedge", "?")
                                pnl_d = getattr(self, '_hedge_pnl_day_cache', 0) or 0.0
                                # Numero posizioni chiuse in questo ciclo
                                num_closed = self._prev_hedge_pos_c - n_h
                                # Profit per posizione (approssimato: divido il delta totale)
                                profit_per_pos = delta / num_closed if num_closed > 0 else delta
                                lots_per_pos   = lots_closed / num_closed if num_closed > 0 else lots_closed
                                log("notify HEDGE close | n_closed={} delta_bal={:.2f} per_pos={:.2f} lots={:.2f}".format(
                                    num_closed, delta, profit_per_pos, lots_per_pos), "INFO")
                                for _ in range(num_closed):
                                    payload = {
                                        "client_id": cid_now,
                                        "prop_firm": firm,
                                        "symbol":    sym,
                                        "profit":    round(profit_per_pos, 2),
                                        "lots":      round(lots_per_pos, 2),
                                        "reason":    "TP" if profit_per_pos >= 0 else "SL",
                                        "pnl_day":   pnl_d,
                                    }
                                    _payload = payload
                                    def _send_notify(_p=_payload):
                                        try:
                                            import urllib.request as _ur
                                            _data = json.dumps(_p).encode()
                                            _req  = _ur.Request(SERVER_URL + "/api/notify_trade",
                                                                 data=_data, method="POST",
                                                                 headers={"Content-Type":"application/json"})
                                            with _ur.urlopen(_req, timeout=5) as _resp:
                                                log("notify HTTP {} per profit={}".format(
                                                    _resp.status, _p.get("profit")), "INFO")
                                        except Exception as _ex:
                                            log("notify POST error: {}".format(_ex), "ERROR")
                                    threading.Thread(target=_send_notify, daemon=True).start()
                            except Exception as _e:
                                log("notify hedge close error: " + str(_e), "ERROR")
                    self._prev_hedge_bal   = hedge["balance"]
                    self._prev_hedge_pos_c = n_h

                # -- Aggiorna cumulativo LOTTI HEDGE (al rilevamento di nuove aperture) --
                # Logica: confronto il volume hedge aperto adesso col volume aperto prima.
                # Se aumenta -> sono state aperte nuove posizioni, somma il delta al cumulativo.
                # Se diminuisce (chiusure) -> non tocco il cumulativo (è monotono crescente).
                # Se _prev_hedge_volume == -1 -> primo ciclo, inizializzo senza sommare
                if hedge:
                    cur_vol = float(hedge.get("total_volume", 0) or 0)
                    if self._prev_hedge_volume < 0:
                        # Primo ciclo dopo avvio o cambio broker: sincronizzo senza sommare
                        self._prev_hedge_volume = cur_vol
                    elif cur_vol > self._prev_hedge_volume + 0.001:  # tolleranza per floating point
                        delta_vol = cur_vol - self._prev_hedge_volume
                        self._hedge_lots_cum += delta_vol
                        _save_hedge_lots(self._trader_id, self._hedge_lots_cum)
                        log("Lotti HEDGE +{:.2f} (cumulativo: {:.2f})".format(
                            delta_vol, self._hedge_lots_cum))
                        self._prev_hedge_volume = cur_vol
                    else:
                        self._prev_hedge_volume = cur_vol

                # -- Aggiorna storico solo quando le posizioni si azzerano --
                n_prop  = prop["positions"]  if prop  else 0
                n_hedge = hedge["positions"] if hedge else 0
                _had_positions = getattr(self, '_had_positions', False)
                _now_zero = (n_prop == 0 and n_hedge == 0)
                if _had_positions and _now_zero:
                    # Posizioni appena chiuse — aggiorna storico
                    new_hedge_trades = get_closed_trades(cur_hp, self._session_start_ts)
                    new_prop_trades  = get_closed_trades(cur_pp, self._session_start_ts)
                    self._trades_log  = new_hedge_trades
                    self._trade_count = len(new_hedge_trades)
                    all_rows = []
                    for pt in new_prop_trades:
                        all_rows.append({
                            "conto":      "PROP",
                            "open_time":  pt.get("open_time", "—"),
                            "close_time": pt.get("time", "—"),
                            "date":       pt.get("date", "—"),
                            "type":       pt.get("type", "—"),
                            "profit":     pt.get("profit", 0),
                            "volume":     pt.get("volume", 0),
                        })
                    for ht in new_hedge_trades:
                        all_rows.append({
                            "conto":      "HEDGE",
                            "open_time":  ht.get("open_time", "—"),
                            "close_time": ht.get("time", "—"),
                            "date":       ht.get("date", "—"),
                            "type":       ht.get("type", "—"),
                            "profit":     ht.get("profit", 0),
                            "volume":     ht.get("volume", 0),
                        })
                    all_rows.sort(key=lambda x: x.get("close_time", ""))
                    self._trades_history = all_rows
                    _save_trades_history(self._trader_id, all_rows)
                    # Aggiorna P&L cumulativo HEDGE
                    if hedge and self._prev_hedge_bal is not None:
                        self._hedge_pnl_cum += hedge["balance"] - self._prev_hedge_bal
                    # Reset flag panic dopo aver processato (la notifica vera è gestita
                    # da un blocco indipendente che gira ad ogni ciclo)
                    self._panic_just_pressed = False
                self._had_positions = (n_prop > 0 or n_hedge > 0)
                if prop  is not None: self._prev_hedge_bal = hedge["balance"] if hedge else self._prev_hedge_bal

                # === BLOCCO LEGACY notifica via get_closed_trades RIMOSSO ===
                # Sostituito da rilevamento via cambio balance HEDGE (vedi sopra),
                # più affidabile perché non dipende da mt5.history_deals_get
                # che a volte ritorna cache vecchia.
                self._had_positions = (n_prop > 0 or n_hedge > 0)
                if prop  is not None: self._prev_hedge_bal = hedge["balance"] if hedge else self._prev_hedge_bal

                # -- Aggiornamento storico schedulato (08:00 e 20:00) --
                _now_hour = datetime.now().hour
                _now_slot = "{}-{}".format(today, _now_hour)
                if _now_slot != self._last_scheduled_update:
                    self._last_scheduled_update = _now_slot
                    _sched_hedge = get_closed_trades(cur_hp, self._session_start_ts)
                    _sched_prop  = get_closed_trades(cur_pp, self._session_start_ts)
                    _sched_rows  = []
                    for pt in _sched_prop:
                        _sched_rows.append({"conto":"PROP","open_time":pt.get("open_time","—"),"close_time":pt.get("time","—"),"date":pt.get("date","—"),"type":pt.get("type","—"),"profit":pt.get("profit",0),"volume":pt.get("volume",0)})
                    for ht in _sched_hedge:
                        _sched_rows.append({"conto":"HEDGE","open_time":ht.get("open_time","—"),"close_time":ht.get("time","—"),"date":ht.get("date","—"),"type":ht.get("type","—"),"profit":ht.get("profit",0),"volume":ht.get("volume",0)})
                    _sched_rows.sort(key=lambda x: x.get("close_time",""))
                    if _sched_rows:
                        self._trades_history = _sched_rows
                        _save_trades_history(self._trader_id, _sched_rows)

                # -- Conta giorni di trading da operazioni PROP --
                # Un giorno viene contato se c'è almeno una posizione aperta su PROP
                if prop and prop["positions"] > 0:
                    today_str = today.strftime("%d/%m/%Y")
                    if today_str not in self._trading_days:
                        self._trading_days.add(today_str)
                        _save_trading_days(self._trader_id, self._trading_days)

                # -- Aggiorna balance start giorno (fix v4.8.9) --
                # Ricostruisce il balance vero di inizio giornata leggendo
                # la history MT5 dei deal chiusi dalle 22:00 UTC.
                # FIX v4.8.9: NON sovrascrivere mai un balance_start valido con
                # il balance corrente (fallback). Il vecchio codice forzava la
                # ricostruzione ogni ciclo quando balance_start ≈ balance,
                # e se MT5 history tornava None, resettava balance_start al
                # balance corrente → DD daily sempre 0.
                # Ora: ricostruiamo al cambio giorno + max 1 retry se il primo
                # tentativo aveva dato fallback. Mai sovrascrivere con fallback
                # se un valore valido è già presente.
                _need_reconstruct = (self._balance_start_date != today)
                # Retry: se abbiamo usato il fallback (flag), riproviamo
                # a leggere la history per ottenere un valore corretto
                if not _need_reconstruct and getattr(self, '_balance_start_is_fallback', False):
                    _need_reconstruct = True

                if prop and _need_reconstruct:
                    bal_now = prop["balance"]
                    closed_pnl_today = None
                    try:
                        closed_pnl_today = get_daily_closed_pnl(pp)
                    except Exception as _e:
                        log("balance_start: errore lettura history MT5: {}".format(_e), "WARNING")
                    if closed_pnl_today is not None:
                        new_start = bal_now - closed_pnl_today
                        _old_start = getattr(self, '_balance_start_day', None)
                        if _old_start is None or self._balance_start_date != today:
                            self._balance_start_day = new_start
                            log("balance_start ricostruito | bal_now={:.2f} closed_today={:.2f} start={:.2f}".format(
                                bal_now, closed_pnl_today, self._balance_start_day), "INFO")
                        self._balance_start_date = today
                        self._balance_start_is_fallback = False  # history OK, stop retry
                    else:
                        # Fallback: usa balance corrente SOLO se non abbiamo
                        # ancora un valore valido per oggi
                        if self._balance_start_day is None or self._balance_start_date != today:
                            self._balance_start_day = bal_now
                            log("balance_start fallback | bal_now={:.2f} (history non disponibile)".format(bal_now), "WARNING")
                        self._balance_start_date = today
                        self._balance_start_is_fallback = True  # segna per retry

                # -- Numero posizioni correnti --
                n_prop  = prop["positions"]  if prop  else 0
                n_hedge = hedge["positions"] if hedge else 0

                # -- DD STOP automatico (usa cache MT5) --
                # FIX v4.8.6: Baseline DD = min(balance_inizio_giornata, size)
                # Se balance >= size, la baseline è il size (DD applicato a 50K fissi)
                # Se balance < size, la baseline è il balance attuale (DD scende con te)
                # Es: size 50K, balance 49K → baseline 49K, soglia STOP = 49K - 2.5K = 46.5K
                if prop and not self._dd_blocked:
                    sz          = float(t.get("size", 100000))
                    dd_stop_pct = float(t.get("dd_stop_pct", 4.6))
                    dd_stop_val = sz * dd_stop_pct / 100
                    equity_now  = prop["balance"] + prop.get("pnl", 0)
                    # Baseline: il minore tra balance inizio giornata e size
                    if self._balance_start_day is not None:
                        baseline = min(self._balance_start_day, sz)
                    else:
                        baseline = sz
                    # Perdita = quanto sotto la baseline siamo arrivati (mai negativa)
                    perdita_day = max(baseline - equity_now, 0)
                    if perdita_day >= dd_stop_val:
                        log("DD GIORNALIERO STOP | scheda=" + str(t.get("prop_firm","?")), "WARNING")
                        self._dd_blocked        = True
                        self._dd_block_date     = today
                        self._dd_just_triggered = True
                        _pp2 = pp; _hp2 = hp
                        self.after(0, lambda d=dd_stop_pct: self.set_msg("⛔ DD STOP {:.1f}% - Chiudo tutto!".format(d), RED))
                        threading.Thread(
                            target=lambda _p=_pp2, _h=_hp2: (chiudi_posizioni(_p), chiudi_posizioni(_h)),
                            daemon=True).start()
                        # *** NON archivia automaticamente — lo fa l'utente manualmente ***

                # --- NUOVO v4.8.6: Check LIMITE UFFICIALE DD giornaliero ---
                # Se l'equity scende sotto la soglia ufficiale (es. 5%), la prop firm
                # te la dichiara BRUCIATA. Mostro popup: "Archivia come BRUCIATA?"
                if prop and not self._dd_limit_dialog_open:
                    sz_real = float(t.get("size", 100000))
                    dd_real_pct = float(t.get("dd_giornaliero_pct", 5))
                    dd_real_val = sz_real * dd_real_pct / 100
                    equity_real = prop["balance"] + prop.get("pnl", 0)
                    if self._balance_start_day is not None:
                        baseline_real = min(self._balance_start_day, sz_real)
                    else:
                        baseline_real = sz_real
                    perdita_real = max(baseline_real - equity_real, 0)
                    # Soglia ufficiale prop (es. 5% di 50K = 2.5K)
                    if perdita_real >= dd_real_val:
                        self._dd_limit_dialog_open = True
                        log("DD GIORNALIERO LIMITE UFFICIALE | scheda=" + str(t.get("prop_firm","?")), "WARNING")
                        _firm = t.get("prop_firm", "?")
                        _perdita = perdita_real
                        _limit = dd_real_val
                        def _ask_bruciata(_f=_firm, _p=_perdita, _l=_limit):
                            try:
                                msg = ("⚠ DD GIORNALIERO LIMITE SUPERATO\n\n"
                                       "Scheda:  {f}\n"
                                       "Perdita giornaliera: ${p:,.2f}\n"
                                       "Limite ufficiale:    ${l:,.2f}\n\n"
                                       "La prop firm potrebbe dichiararla BRUCIATA.\n\n"
                                       "Archiviare la scheda come BRUCIATA?").format(f=_f, p=_p, l=_l)
                                if messagebox.askyesno("ARBEXA — Scheda Bruciata", msg):
                                    self._do_archive("BRUCIATA", auto=False)
                            except: pass
                            finally:
                                # Resta True per evitare popup ripetuti finché l'app gira
                                pass
                        self.after(100, _ask_bruciata)

                # ============================================================
                # -- Chiusura automatica PROP/HEDGE (logica robusta) --
                # ============================================================
                # Stato "sbilanciato" = una posizione su un conto, zero sull'altro
                # Richiede letture MT5 valide su entrambi i conti.
                both_valid    = (prop is not None and hedge is not None)
                is_unbalanced = both_valid and (
                    (n_prop > 0 and n_hedge == 0) or
                    (n_prop == 0 and n_hedge > 0)
                )

                # ---- (A) ORPHAN CHECK all'avvio (primo ciclo con dati validi) ----
                # Se all'avvio dell'app trova UNA SOLA posizione aperta, mostra popup
                # all'utente che decide se chiuderla. Risolve il caso "PC spento e
                # una posizione si è chiusa nel frattempo".
                if (not self._first_cycle_done) and both_valid:
                    self._first_cycle_done = True
                    if is_unbalanced and not self._orphan_dialog_open:
                        self._orphan_dialog_open = True
                        _pp_o = pp; _hp_o = hp
                        _np_o = n_prop; _nh_o = n_hedge
                        _firm = t.get("prop_firm", "?")
                        def _ask_orphan(_pp=_pp_o, _hp=_hp_o, _np=_np_o, _nh=_nh_o, _f=_firm):
                            try:
                                if _np > 0 and _nh == 0:
                                    msg = ("⚠ POSIZIONE ORFANA RILEVATA\n\n"
                                           "Scheda: {}\n"
                                           "PROP: {} posizione/i aperta/e\n"
                                           "HEDGE: 0 posizioni\n\n"
                                           "L'altra si è probabilmente chiusa "
                                           "mentre il PC era spento.\n\n"
                                           "Vuoi chiudere la posizione PROP rimasta?").format(_f, _np)
                                    target_path = _pp
                                    side = "PROP"
                                else:
                                    msg = ("⚠ POSIZIONE ORFANA RILEVATA\n\n"
                                           "Scheda: {}\n"
                                           "PROP: 0 posizioni\n"
                                           "HEDGE: {} posizione/i aperta/e\n\n"
                                           "L'altra si è probabilmente chiusa "
                                           "mentre il PC era spento.\n\n"
                                           "Vuoi chiudere la posizione HEDGE rimasta?").format(_f, _nh)
                                    target_path = _hp
                                    side = "HEDGE"
                                if messagebox.askyesno("ARBEXA — Posizione orfana", msg):
                                    self.set_msg("Chiudo orfana " + side + "...", GOLD)
                                    threading.Thread(
                                        target=lambda _x=target_path: chiudi_posizioni(_x),
                                        daemon=True).start()
                                else:
                                    self.set_msg("Orfana " + side + " ignorata", GRAY)
                            finally:
                                self._orphan_dialog_open = False
                        self.after(0, _ask_orphan)
                    # Aggiorna prev e SALTA i check successivi per questo ciclo
                    self._prev_prop_pos  = n_prop
                    self._prev_hedge_pos = n_hedge
                    self._last_prop  = prop
                    self._last_hedge = hedge
                    time.sleep(3)
                    continue

                # ---- (B) Chiusura automatica IMMEDIATA (logica originale) ----
                # Quando l'app è attiva e rileva la transizione da 1→0 su un conto.
                if (both_valid
                        and self._prev_prop_pos >= 0
                        and not self._dd_just_triggered):

                    if self._prev_prop_pos > 0 and n_prop == 0 and self._prev_hedge_pos > 0:
                        _hp3 = hp
                        self.after(0, lambda: self.set_msg("PROP chiusa - chiudo HEDGE...", GOLD))
                        self._trade_count += 1
                        threading.Thread(target=lambda _h=_hp3: chiudi_posizioni(_h), daemon=True).start()

                    elif self._prev_hedge_pos > 0 and n_hedge == 0 and self._prev_prop_pos > 0:
                        _pp3 = pp
                        self.after(0, lambda: self.set_msg("HEDGE chiusa - chiudo PROP...", GOLD))
                        self._trade_count += 1
                        threading.Thread(target=lambda _p=_pp3: chiudi_posizioni(_p), daemon=True).start()

                # ---- (D) WARNING DD GIORNALIERO al 90% + CACHE PNL DAY ----
                # Calcola P&L totale di OGGI (chiuse + floating) sul conto PROP.
                # Aggiorna la cache _pnl_day_cache usata da barra DD e DD STOP.
                # Se la perdita raggiunge il 90% del DD daily ammesso → popup di avviso.
                # Solo una volta al giorno. Reset automatico al cambio data.
                # Il "giorno di trading" segue lo standard prop firm: reset a 22:00 UTC
                # (= 00:00 italiana ora legale, 23:00 italiana ora solare).
                from datetime import datetime as _dt
                _utc_now = _dt.utcnow()
                # "Data trading" = data dell'ultimo 22:00 UTC passato
                if _utc_now.hour >= 22:
                    _trade_day_start = _utc_now.replace(hour=22, minute=0, second=0, microsecond=0)
                else:
                    _trade_day_start = (_utc_now - timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
                _today_str = _trade_day_start.strftime("%Y-%m-%d")
                if self._dd_warning_date != _today_str:
                    # Nuovo giorno di trading: reset flag warning E cache
                    self._dd_warning_date  = _today_str
                    self._dd_warning_shown = False
                    self._dd_limit_dialog_open = False  # reset popup BRUCIATA
                    self._pnl_day_cache    = None  # forza ricalcolo

                # Faccio il check ogni ~30s assoluti per non sovraccaricare MT5
                self._dd_check_counter += 1
                _last_dd_check = getattr(self, '_last_dd_check_ts', 0)
                _now = time.time()
                if pp and self._dd_check_counter >= 3 and (_now - _last_dd_check) >= 30:
                    self._dd_check_counter = 0
                    self._last_dd_check_ts = _now
                    try:
                        size = float(t.get("size", 100000) or 100000)
                        dd_day_pct = float(t.get("dd_giornaliero_pct", 5) or 5)
                        dd_max_loss = size * (dd_day_pct / 100.0)  # in $
                        soglia_warn = dd_max_loss * 0.90  # 90% del DD
                        # Calcola P&L giornaliero da MT5 PROP + HEDGE (in thread per non bloccare)
                        _pp_chk    = pp
                        _hp_chk    = hp
                        _show_warn = not self._dd_warning_shown
                        def _check_dd(_p=_pp_chk, _h=_hp_chk, _soglia=soglia_warn,
                                       _max=dd_max_loss, _firm=t.get("prop_firm", "?"),
                                       _show=_show_warn):
                            try:
                                pnl_day = get_daily_pnl(_p)
                                # NB: Il P&L giornaliero PROP (self._pnl_day_cache) ora viene
                                # accumulato in tempo reale ad ogni chiusura PROP nel loop
                                # principale (vedi _prop_pnl_day_accum). Qui usiamo il valore
                                # solo come fallback se l'accumulator non è ancora stato
                                # inizializzato (es. prima chiusura della giornata).
                                if pnl_day is not None and self._pnl_day_cache is None:
                                    self._pnl_day_cache    = pnl_day
                                    self._pnl_day_cache_ts = time.time()
                                # P&L giornaliero HEDGE NON più calcolato qui:
                                # adesso viene accumulato in tempo reale dai delta_balance
                                # delle chiusure HEDGE (vedi _hedge_pnl_day_accum nel loop principale).
                                # Questo evita di sovrascrivere il valore corretto con la cache
                                # vecchia di mt5.history_deals_get.
                                if pnl_day is None:
                                    return
                                # Warning solo se non già mostrato e perdita >= 90% DD
                                if _show and pnl_day <= -_soglia:
                                    self._dd_warning_shown = True
                                    pct = abs(pnl_day) / _max * 100 if _max > 0 else 0
                                    def _show_dd_popup():
                                        try:
                                            messagebox.showwarning(
                                                "⚠ Attenzione DD Giornaliero",
                                                "Scheda: {}\n\n"
                                                "Perdita giornaliera: ${:,.2f}\n"
                                                "Limite DD daily:     ${:,.2f}\n"
                                                "Utilizzo: {:.1f}% del DD\n\n"
                                                "Sei VICINO al limite di drawdown "
                                                "giornaliero. Considera di smettere "
                                                "di operare per oggi per evitare "
                                                "di bruciare la challenge.".format(
                                                    _firm, pnl_day, _max, pct))
                                        except: pass
                                    self.after(0, _show_dd_popup)
                            except: pass
                        threading.Thread(target=_check_dd, daemon=True).start()
                    except: pass

                # ---- (C) WATCHDOG: rete di sicurezza per chiusure fallite ----
                # Se per 5 cicli consecutivi (~15s) lo stato resta sbilanciato e
                # non è scattato DD STOP, forza la chiusura del lato superstite.
                # Soglia alta (5) per ridurre falsi positivi durante transazioni MT5.
                if both_valid and not self._dd_just_triggered:
                    if is_unbalanced:
                        self._unbalanced_count += 1
                        if self._unbalanced_count >= 5:
                            try:
                                if n_prop > 0 and n_hedge == 0:
                                    _pp4 = pp
                                    self.after(0, lambda: self.set_msg(
                                        "⚠ WATCHDOG: HEDGE orfana - forzo chiusura PROP", RED))
                                    def _safe_close_prop(_p=_pp4):
                                        try: chiudi_posizioni(_p)
                                        except: pass
                                    threading.Thread(target=_safe_close_prop, daemon=True).start()
                                elif n_hedge > 0 and n_prop == 0:
                                    _hp4 = hp
                                    self.after(0, lambda: self.set_msg(
                                        "⚠ WATCHDOG: PROP orfana - forzo chiusura HEDGE", RED))
                                    def _safe_close_hedge(_h=_hp4):
                                        try: chiudi_posizioni(_h)
                                        except: pass
                                    threading.Thread(target=_safe_close_hedge, daemon=True).start()
                            except: pass
                            self._unbalanced_count = 0  # reset dopo intervento
                    else:
                        self._unbalanced_count = 0  # stato sano: azzera contatore

                # Reset flag DD dopo 2 cicli (6 secondi) — abbastanza tempo per chiudere
                if self._dd_just_triggered and n_prop == 0 and n_hedge == 0:
                    self._dd_just_triggered = False

                # Aggiorna contatori solo con dati validi
                if prop  is not None: self._prev_prop_pos  = n_prop
                if hedge is not None: self._prev_hedge_pos = n_hedge

                # -- Salva per heartbeat --
                self._last_prop  = prop
                self._last_hedge = hedge

                # -- Cache lotti (ogni 30s) --
                if time.time() - self._lotto_ts > 30:
                    try:
                        lp, dbg_p = calcola_lotto(pp, t.get("simbolo_prop","GOLD"),    t.get("stop_prop",3000),   t.get("distanza",1000))
                        lh, _     = calcola_lotto(hp, t.get("simbolo_hedge","XAUUSD"), t.get("target_hedge",300), t.get("distanza",1000))
                        # Salva i valori BASE (calcolati dal broker) E i valori operativi
                        self._lotto_prop_base  = lp
                        self._lotto_hedge_base = lh
                        self._lotto_prop  = lp
                        self._lotto_hedge = lh
                        self._lotto_ts    = time.time()
                        # Estrai valore punto da debug string "1pt=X.XXXX lotto=Y"
                        try:
                            vp = float(dbg_p.split("1pt=")[1].split(" ")[0])
                            self._val_punto_prop = vp
                        except: pass
                    except: pass

                # -- Riduzione lotti in base al DD totale (-6% → -10%) --
                # FIX BUG: la riduzione parte SEMPRE dai valori BASE freschi,
                # non dal valore già ridotto (che si auto-decrementava esponenzialmente)
                try:
                    sz       = float(t.get("size", 0) or 0)
                    pb       = float(prop["balance"] if prop else t.get("prop_bal", sz))
                    dd_tot   = float(t.get("dd_totale_pct", 10) or 10)
                    # Base values (sempre valori freschi dal broker)
                    base_lp = getattr(self, '_lotto_prop_base', self._lotto_prop) or self._lotto_prop
                    base_lh = getattr(self, '_lotto_hedge_base', self._lotto_hedge) or self._lotto_hedge
                    if sz > 0:
                        dd_usato_pct = (sz - pb) / sz * 100
                        dd_soglia    = dd_tot * 0.6   # 60% del DD totale = soglia inizio riduzione
                        if dd_usato_pct > dd_soglia:
                            progress = min((dd_usato_pct - dd_soglia) / (dd_tot - dd_soglia), 1.0)
                            # PROP: riduzione max 4% (da 1.0 a 0.96)
                            coeff_p  = 1.0 - progress * 0.04
                            # HEDGE: riduzione max 62.5% (da 1.0 a 0.375)
                            coeff_h  = 1.0 - progress * 0.625
                            # Applico riduzione SU BASE, non sui valori già ridotti
                            self._lotto_prop  = round(base_lp * coeff_p, 2)
                            self._lotto_hedge = round(base_lh * coeff_h, 2)
                            if self._lotto_prop  < 0.01: self._lotto_prop  = 0.01
                            if self._lotto_hedge < 0.01: self._lotto_hedge = 0.01
                        else:
                            # Nessuna riduzione: ripristina i valori base
                            self._lotto_prop  = base_lp
                            self._lotto_hedge = base_lh
                except: pass

                # -- Aggiorna UI (tutto via after(0) per thread safety) --
                try:
                    sz  = float(t.get("size", 100000))
                    _lp = self._lotto_prop
                    _lh = self._lotto_hedge

                    if prop:
                        bal = prop["balance"]; pnl = prop["pnl"]
                        bal_start = self._balance_start_day or bal
                        equity = bal + pnl  # equity live = balance + floating P&L
                        # FIX v4.8.6: DD DAILY usa baseline corretta (min tra balance start e size)
                        # Le barre si muovono live perché usano equity (non balance)
                        baseline_d = min(bal_start, sz)
                        dd_d  = sz * float(t.get("dd_giornaliero_pct",5)) / 100
                        variazione_d = equity - baseline_d  # >0 = guadagno, <0 = perdita
                        uso_d = -variazione_d  # positivo = perdita, negativo = guadagno
                        rem_d = dd_d - max(uso_d, 0)
                        # ratio: 1.0 = nessuna perdita, 0.0 = DD massimo raggiunto
                        ratio_d = max(0.0, min(1.0, (dd_d - max(uso_d, 0)) / dd_d)) if dd_d > 0 else 1.0
                        c_d   = gradient_color(ratio_d)
                        # FIX v4.8.6: DD TOTALE usa equity (si muove live con il prezzo)
                        dd_t  = sz * float(t.get("dd_totale_pct",10)) / 100
                        uso_t = max(sz - equity, 0)
                        rem_t = max(dd_t - uso_t, 0)
                        ratio_t = max(0.0, min(1.0, rem_t / dd_t)) if dd_t > 0 else 1.0
                        c_t   = gradient_color(ratio_t)
                        # FIX v4.8.6: TARGET usa equity (si muove live con il prezzo)
                        tgt   = sz * float(t.get("target_pct",10)) / 100
                        guad  = max(equity - sz, 0)
                        ratio_g = max(0.0, min(1.0, guad / tgt)) if tgt > 0 else 0.0
                        manca = max(tgt - guad, 0)
                        c_g   = gradient_color(ratio_g)
                        tdays = len(self._trading_days)
                        dd_gjpct = t.get("dd_giornaliero_pct",5)
                        dd_tpct  = t.get("dd_totale_pct",10)
                        tgt_pct  = t.get("target_pct",10)
                        fase     = t.get("fase","Fase 1")
                        fase_txt = {"Fase 1":"✅ FASE 1 PASSATA!","Fase 2":"✅ FASE 2 PASSATA!"}.get(fase,"✅ FUNDED RAGGIUNTO!")

                        # Calcola stamp PASSATA/BRUCIATA
                        # Per il TARGET, controlla EQUITY (balance + floating) non solo balance,
                        # così le posizioni aperte in profitto vengono riconosciute subito.
                        target_bal = sz * (1 + float(t.get("target_pct",10))/100)
                        dd_bal     = sz * (1 - float(t.get("dd_totale_pct",10))/100)
                        equity_now = bal + pnl  # bal è il balance, pnl è floating
                        stamp = ""
                        if sz > 0 and bal >= target_bal: stamp = "PASSATA"
                        elif sz > 0 and bal <= dd_bal and bal > 0: stamp = "BRUCIATA"

                        # NUOVO: chiusura preventiva quando l'EQUITY raggiunge il target
                        # (anche se il balance non è ancora aggiornato perché la posizione è aperta).
                        # Una volta chiuse, al ciclo successivo il balance arriverà al target
                        # e partirà l'archiviazione completa nel blocco sotto.
                        if (sz > 0 and not stamp and equity_now >= target_bal and
                                n_prop > 0 and not getattr(self, "_target_closing", False)):
                            self._target_closing = True
                            _pp_t = pp; _hp_t = hp
                            self.after(0, lambda: self.set_msg(
                                "🎯 TARGET RAGGIUNTO — chiudo posizioni!", GOLD))
                            def _close_on_target(_p=_pp_t, _h=_hp_t):
                                try: chiudi_posizioni(_p)
                                except: pass
                                try: chiudi_posizioni(_h)
                                except: pass
                            threading.Thread(target=_close_on_target, daemon=True).start()

                        # Archiviazione automatica — se appena diventata PASSATA/BRUCIATA
                        prev_stamp = getattr(self, "_current_stamp", "")
                        if stamp and stamp != prev_stamp and not getattr(self, "_auto_archiving", False):
                            self._auto_archiving = True
                            self._target_closing  = False  # reset flag dopo archiviazione
                            _esito = stamp
                            _pp5 = pp; _hp5 = hp
                            def _auto_archive(esito=_esito, _p=_pp5, _h=_hp5):
                                # Chiudi sempre tutte le posizioni su PASSATA e BRUCIATA
                                chiudi_posizioni(_p)
                                chiudi_posizioni(_h)
                                time.sleep(5)
                                self.after(0, lambda: self._do_archive(esito, auto=True))
                            threading.Thread(target=_auto_archive, daemon=True).start()
                        self._current_stamp = stamp

                        # Target raggiunto — la chiusura è gestita dal blocco stamp PASSATA sopra
                        if ratio_g < 1.0:
                            self._target_closed = False
                            self._target_closing = False  # reset se sotto target

                        def _ui_prop(bal=bal,pnl=pnl,tdays=tdays,
                                     uso_d=uso_d,rem_d=rem_d,ratio_d=ratio_d,c_d=c_d,
                                     uso_t=uso_t,rem_t=rem_t,ratio_t=ratio_t,c_t=c_t,
                                     guad=guad,manca=manca,ratio_g=ratio_g,c_g=c_g,
                                     fase_txt=fase_txt,_lp=_lp,_lh=_lh,stamp=stamp,
                                     dd_gjpct=dd_gjpct,dd_tpct=dd_tpct,tgt_pct=tgt_pct):
                            try:
                                self.lbl_prop_bal.configure(text="${:,.2f}".format(bal))
                                self.lbl_prop_pnl.configure(
                                    text=("+" if pnl>=0 else "")+"${:,.2f}".format(pnl),
                                    text_color=GREEN if pnl>=0 else RED)
                                self.lbl_status.configure(text="LIVE",text_color=GREEN)
                                self.lbl_trading_days.configure(text="📅 {} gg".format(tdays))
                                self.lbl_dd_day_pct.configure(text="{:.1f}%  rimane: ${:,.0f}".format(dd_gjpct,rem_d),text_color=WHITE)
                                self.lbl_dd_day_rem.configure(text=("perdita: ${:,.0f}".format(uso_d) if uso_d > 0 else "guadagno: ${:,.0f}".format(-uso_d)),text_color=WHITE)
                                self.bar_dd_day.configure(progress_color=c_d); self.bar_dd_day.set(ratio_d)
                                self.lbl_dd_tot_pct.configure(text="{:.1f}%  rimane: ${:,.0f}".format(dd_tpct,rem_t),text_color=WHITE)
                                self.lbl_dd_tot_rem.configure(text="usato: ${:,.0f}".format(uso_t),text_color=WHITE)
                                self.bar_dd_tot.configure(progress_color=c_t); self.bar_dd_tot.set(ratio_t)
                                if ratio_g >= 1.0:
                                    self.lbl_status.configure(text="🎯 TARGET — CHIUSO!",text_color=GOLD)
                                    self.lbl_tgt_pct.configure(text=fase_txt,text_color=GREEN)
                                    self.lbl_tgt_rem.configure(text="")
                                    self.bar_tgt.set(1.0); self.bar_tgt.configure(progress_color=GREEN)
                                else:
                                    self.lbl_tgt_pct.configure(text="{:.1f}%  fatto: ${:,.0f}".format(tgt_pct,guad),text_color=WHITE)
                                    self.lbl_tgt_rem.configure(text="manca: ${:,.0f}".format(manca),text_color=WHITE)
                                    self.bar_tgt.configure(progress_color=c_g); self.bar_tgt.set(ratio_g)
                                self.lbl_lotti.configure(text="Lotto PROP: {}  |  HEDGE: {}".format(_lp,_lh))
                                # Aggiorna boost display se attivo
                                boost_on = getattr(self, '_boost_var', None)
                                if boost_on and boost_on.get():
                                    try:
                                        self.lbl_boost_lotti.configure(
                                            text="→ PROP: {}  HEDGE: {}".format(round(_lp*2,2), round(_lh*2,2)))
                                    except: pass
                                # Timbro PASSATA/BRUCIATA
                                if stamp:
                                    sc = GREEN if stamp=="PASSATA" else RED
                                    self.lbl_stamp.configure(text="● "+stamp, text_color=sc)
                                    self._btn_archivia.configure(
                                        fg_color="#0D2B1F" if stamp=="PASSATA" else "#2B0D0D",
                                        text_color=sc)
                                    self._btn_archivia.pack(side="right")
                                else:
                                    self.lbl_stamp.configure(text="")
                                    self._btn_archivia.pack_forget()
                                # Semafori spread — aggiornati qui sul main thread
                                def _sc(s):
                                    if s is None: return GRAY
                                    if s < 30:   return GREEN
                                    if s <= 60:  return GOLD
                                    return RED
                                def _sv(s):
                                    return "—" if s is None else str(s)
                                try:
                                    sp = getattr(self, '_spread_prop', None)
                                    sh = getattr(self, '_spread_hedge', None)
                                    self.lbl_spread_prop.configure(text_color=_sc(sp), text="● "+_sv(sp))
                                    self.lbl_spread_hedge.configure(text_color=_sc(sh), text="● "+_sv(sh))
                                except: pass
                            except: pass
                        self.after(0, _ui_prop)
                    else:
                        self.after(0, lambda: (
                            self.lbl_prop_bal.configure(text="---"),
                            self.lbl_prop_pnl.configure(text="MT5 OFFLINE",text_color=RED),
                            self.lbl_status.configure(text="⚠ MT5 OFFLINE",text_color=RED)
                        ))

                    if hedge:
                        pnl_h = hedge["pnl"]
                        pnl_d = self._hedge_pnl_cum + pnl_h
                        tc    = self._trade_count
                        hbal  = hedge["balance"]
                        def _ui_hedge(pnl_h=pnl_h,pnl_d=pnl_d,tc=tc,hbal=hbal):
                            try:
                                self.lbl_hedge_bal.configure(text="${:,.2f}".format(hbal))
                                self.lbl_hedge_pnl.configure(
                                    text=("+" if pnl_h>=0 else "")+"${:,.2f}".format(pnl_h),
                                    text_color=GREEN if pnl_h>=0 else RED)
                                self.lbl_netto.configure(
                                    text="HEDGE P&L: "+("+" if pnl_d>=0 else "")+"${:,.2f}".format(pnl_d)+"  |  OPS: {}".format(tc),
                                    text_color=GREEN if pnl_d>=0 else RED)
                            except: pass
                        self.after(0, _ui_hedge)
                    else:
                        self.after(0, lambda: (
                            self.lbl_hedge_bal.configure(text="---"),
                            self.lbl_hedge_pnl.configure(text="MT5 OFFLINE",text_color=RED)
                        ))
                except Exception as e:
                    self.after(0, lambda e=e: self.set_msg("Err: {}".format(str(e)[:50]), RED))

                # === REFRESH RATE FISSO ===
                # 3s come v4.8.3 stabile. L'adaptive variable rate creava
                # comportamenti imprevedibili quando MT5 era lento.
                time.sleep(3)

        threading.Thread(target=loop, daemon=True).start()

    def destroy(self):
        self.running = False
        super().destroy()

# ------------------------------------------------
#  APP PRINCIPALE
# ------------------------------------------------

class ArbexaApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Arbexa")
        # Dimensione iniziale compatta — verrà ridimensionata da _resize_to_cards
        # in base al numero di schede caricate.
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            ww = 720
            wh = min(int(sh * 0.85), 1000)
            wx = (sw - ww) // 2
            wy = (sh - wh) // 2
            self.geometry(f"{ww}x{wh}+{max(wx,0)}+{max(wy,0)}")
        except:
            self.geometry("720x900")
        self.configure(fg_color=BG)
        self.minsize(680, 700)
        self.cards       = []
        self._overlay    = None
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build()
        # Avvia heartbeat server
        start_heartbeat(get_client_id(), get_days_left())
        log(f"AVVIO | client_id={get_client_id()} license_days={get_days_left()}")
        # Polling comandi remoti ogni 2 secondi
        self._poll_commands()
        # Onboarding al primissimo avvio: scatta se non ci sono schede configurate
        # OPPURE se è davvero la prima volta (file marker assente)
        try:
            no_schede = len(get_traders()) == 0
        except:
            no_schede = False
        if _is_first_run() or no_schede:
            self.after(800, self._show_first_run_onboarding)

    def _show_first_run_onboarding(self):
        """Popup di benvenuto + apertura automatica della scheda Configurazione."""
        try:
            _mark_first_run_done()  # marca subito così non si ripete più
            messagebox.showinfo(
                "Benvenuto in ARBEXA",
                "Benvenuto!\n\n"
                "Per iniziare devi creare la tua prima scheda "
                "inserendo i dati della prop firm e dei broker.\n\n"
                "Ricorda di controllare anche la sezione "
                "PARAMETRI prima di operare.\n\n"
                "Cliccando OK ti aprirò il pannello di Configurazione.")
            # Apre il pannello aggiunta scheda
            self.show_add_panel()
        except: pass

    def _on_close(self):
        """Chiude tutto e termina il processo — evita che rimanga in background."""
        try:
            self.destroy()
        except: pass
        import os
        os._exit(0)

    def _build(self):
        # TOPBAR
        top = ctk.CTkFrame(self, fg_color="#0d0d0d", corner_radius=0, height=54)
        top.pack(fill="x")
        top.pack_propagate(False)
        ctk.CTkLabel(top, text="ARBEXA",
                     font=("Segoe UI", 18, "bold"), text_color=BLUE).pack(side="left", padx=(20,2))
        ctk.CTkLabel(top, text="by vins",
                     font=("Segoe UI", 11, "italic"), text_color=GOLD).pack(side="left", padx=(0,10))
        # Versione
        _ver = "v" + APP_VERSION
        ctk.CTkLabel(top, text=_ver,
                     font=("Segoe UI", 9), text_color=GRAY).pack(side="left", padx=(0, 6))
        # ID Cliente integrato in topbar (più visibile, ID e copia ingranditi)
        cid = get_client_id()
        id_topbar = ctk.CTkFrame(top, fg_color=CARD2, corner_radius=8,
                                 border_width=1, border_color=GOLD)
        id_topbar.pack(side="left", padx=(8, 4), pady=8)
        ctk.CTkLabel(id_topbar, text="ID:",
                     font=("Segoe UI", 11), text_color=GRAY).pack(side="left", padx=(10, 5), pady=6)
        ctk.CTkLabel(id_topbar, text=cid,
                     font=("Segoe UI", 14, "bold"), text_color=GOLD).pack(side="left", pady=6)
        def _copy_id_topbar(_cid=cid):
            self.clipboard_clear()
            self.clipboard_append(_cid)
            btn_copy_top.configure(text="✓ COPIATO")
            self.after(1500, lambda: btn_copy_top.configure(text="📋 COPIA"))
        btn_copy_top = ctk.CTkButton(id_topbar, text="📋 COPIA", width=90, height=30,
                                      fg_color="#1E4D7A", hover_color="#2A6BA8",
                                      font=("Segoe UI", 11, "bold"), text_color=WHITE,
                                      command=_copy_id_topbar)
        btn_copy_top.pack(side="left", padx=(6, 6), pady=4)
        ctk.CTkButton(top, text="PANIC ALL",
                      fg_color="#5C0000", hover_color="#8B0000",
                      text_color=RED, font=("Segoe UI", 12, "bold"),
                      width=110, height=34,
                      command=self.panic_all).pack(side="right", padx=10, pady=10)
        # Bottone log: apre la cartella in Esplora file
        ctk.CTkButton(top, text="📋 LOG", width=80, height=34,
                      fg_color=CARD2, hover_color=BORDER,
                      text_color=GRAY, font=("Segoe UI", 11, "bold"),
                      corner_radius=8,
                      command=self._open_logs_folder).pack(side="right", padx=(0, 4), pady=10)
        # Bottone archivio
        ctk.CTkButton(top, text="📁 ARCHIVIO", width=90, height=34,
                      fg_color=CARD2, hover_color=BORDER,
                      text_color=GOLD, font=("Segoe UI", 11, "bold"),
                      corner_radius=8,
                      command=self._show_archive).pack(side="right", padx=(0, 4), pady=10)
        # Box messaggi
        self.lbl_msg_notif = ctk.CTkButton(
            top, text="✉", width=34, height=34,
            fg_color=CARD2, hover_color=BORDER,
            text_color=GRAY, font=("Segoe UI", 11, "bold"),
            corner_radius=8,
            command=self._show_messages)
        self.lbl_msg_notif.pack(side="right", padx=(0, 4), pady=10)
        self._blink_state = False
        days = get_days_left()
        days_text = "Licenza: " + str(days) + "gg" if days is not None else ""
        days_color = RED if days is not None and days <= 7 else GOLD
        self.lbl_licenza = ctk.CTkLabel(top, text=days_text,
                     font=("Segoe UI", 10), text_color=days_color)
        self.lbl_licenza.pack(side="right", padx=4)
        self.lbl_live = ctk.CTkLabel(top, text="SISTEMA PRONTO",
                                      font=("Segoe UI", 11, "bold"), text_color=GREEN)
        self.lbl_live.pack(side="right", padx=10)

        # STATS
        stats = ctk.CTkFrame(self, fg_color="transparent")
        stats.pack(fill="x", padx=20, pady=(12, 6))
        stats.columnconfigure(0, weight=1)
        self.lbl_tot_net = self._stat(stats, "PROFITTO NETTO", "$0.00", GOLD, 0)

        # ID CLIENTE: spostato nella topbar in alto (vedi sopra), libera spazio per le schede

        # SCROLL con le cards
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        self.btn_add = ctk.CTkButton(
            self.scroll, text="+ AGGIUNGI SCHEDA",
            fg_color="transparent", border_width=2, border_color=BORDER,
            text_color=GRAY, hover_color=CARD2,
            font=("Segoe UI", 13, "bold"),
            width=CARD_W, height=CARD_H, corner_radius=14,
            command=self.show_add_panel
        )
        self._overlay = None
        self._load_cards()
        self._update_totals()

    def _stat(self, parent, label, val, color, col):
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=10,
                         border_width=1, border_color=BORDER)
        f.grid(row=0, column=col, padx=5, sticky="nsew")
        ctk.CTkLabel(f, text=label, font=("Segoe UI", 11, "bold"), text_color=GRAY).pack(pady=(10, 2))
        lbl = ctk.CTkLabel(f, text=val, font=("Segoe UI", 32, "bold"), text_color=color)
        lbl.pack(pady=(0, 12))
        return lbl

    def _load_cards(self):
        # Preserva lo stato runtime delle card (DD daily, accumulators, contatori)
        # prima di ricostruirle da zero. Senza questo, salvare la scheda azzera il DD.
        _state_backup = {}
        _STATE_KEYS = (
            "_prop_pnl_day_accum", "_prop_pnl_day_date",
            "_hedge_pnl_day_accum", "_hedge_pnl_day_date", "_hedge_pnl_day_cache",
            "_pnl_day_cache", "_pnl_day_cache_ts",
            "_hedge_pnl_cum", "_hedge_lots_cum",
            "_prev_prop_bal", "_prev_prop_pos_c",
            "_prev_hedge_bal", "_prev_hedge_pos_c", "_prev_hedge_volume",
            "_session_start_ts", "_trades_log", "_trades_history",
            "_trading_days", "_last_trading_date",
            "_dd_warning_shown", "_target_closing",
            "_notified_hedge_count", "_notify_log_counter",
            "_heartbeat_counter",
        )
        for card in self.cards:
            tid = card.trader.get("id")
            if tid:
                _state_backup[tid] = {k: getattr(card, k, None) for k in _STATE_KEYS}
            # Ferma il refresh thread prima di distruggere il widget
            try:
                card.running = False
            except: pass
            try:
                card.destroy()
            except: pass
        self.cards = []
        traders = get_traders()
        cols = 3
        for i, t in enumerate(traders):
            card = TraderCard(self.scroll, t,
                              on_delete=self._del_trader,
                              on_edit=self.show_edit_panel)
            card.grid(row=i // cols, column=i % cols, padx=8, pady=8, sticky="nw")
            # Ripristina lo stato runtime se la card esisteva già
            tid = t.get("id")
            if tid and tid in _state_backup:
                for k, v in _state_backup[tid].items():
                    if v is not None:
                        try:
                            setattr(card, k, v)
                        except: pass
            self.cards.append(card)
        total = len(traders)
        self.btn_add.grid(row=total // cols, column=total % cols,
                          padx=8, pady=8, sticky="nw")
        # Forza update layout PRIMA di calcolare resize
        try: self.update_idletasks()
        except: pass
        # Ridimensiona finestra (delay maggiore per dare tempo alla scrollarea di renderizzare)
        self.after(150, self._resize_to_cards)

    def _resize_to_cards(self):
        """Adatta dinamicamente la larghezza della finestra al numero di card visibili.
        Cresce all'aggiunta, si restringe alla rimozione, sempre centrata sullo schermo."""
        try:
            n_cards = len(self.cards)
            # Slot totali (cards + bottone aggiungi)
            slots = n_cards + 1
            cols = min(slots, 3)  # massimo 3 colonne, poi va a capo (usa scroll vertical)
            # Larghezza singolo slot: card width + padding
            slot_w = CARD_W + 24
            # Margini laterali finestra (sidebar/padding)
            margin = 80
            target_w = cols * slot_w + margin

            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()

            # Cap minimo (per topbar leggibile) e massimo (in base allo schermo)
            min_w = max(CARD_W + 80, 720)
            if sw >= 3000:
                cap_w = 2400
            elif sw >= 2000:
                cap_w = 2000
            else:
                cap_w = int(sw * 0.92)
            target_w = max(min_w, min(target_w, cap_w))

            # Altezza: lascio quella attuale o adatta in base alle righe
            rows = (slots + cols - 1) // cols
            target_h = min(int(sh * 0.85), max(750, rows * (CARD_H + 30) + 220))

            # Centra
            x = max(0, (sw - target_w) // 2)
            y = max(0, (sh - target_h) // 2)
            self.geometry(f"{target_w}x{target_h}+{x}+{y}")
        except Exception:
            pass

    # -- Overlay panel helpers --

    def _show_overlay(self, trader=None, start_tab="config"):
        """Mostra il ConfigPanel come overlay sopra tutto."""
        self._close_overlay()

        def on_save(data):
            self._close_overlay()
            if trader:
                traders = get_traders()
                for i, t in enumerate(traders):
                    if t["id"] == trader["id"]:
                        traders[i] = data
                        break
                save_traders(traders)
            else:
                traders = get_traders()
                traders.append(data)
                save_traders(traders)
            self._load_cards()

        panel = ConfigPanel(self, on_save=on_save,
                            on_cancel=self._close_overlay,
                            trader=trader, start_tab=start_tab)
        panel.place(relx=0.5, rely=0.5, anchor="center",
                    relwidth=0.55, relheight=0.88)
        panel.lift()
        self._overlay = panel

    def _close_overlay(self):
        if getattr(self, "_overlay", None):
            try:
                self._overlay.destroy()
            except: pass
            self._overlay = None

    def show_add_panel(self):
        # Apre sempre il tab Configurazione (anche al primo avvio).
        # Il marker first_run viene comunque scritto la prima volta che viene
        # aperto il pannello, così il popup di benvenuto non si ripete.
        if _is_first_run():
            _mark_first_run_done()
        self._show_overlay(trader=None, start_tab="config")

    def show_edit_panel(self, trader, open_tab=None):
        # open_tab="parametri" → apre direttamente il tab parametri (con password)
        # default: apre "config" (configurazione scheda standard)
        start_tab = "params" if open_tab == "parametri" else "config"
        self._show_overlay(trader=trader, start_tab=start_tab)

    def _del_trader(self, tid):
        if not messagebox.askyesno("Elimina", "Eliminare questa scheda?"):
            return
        traders = [t for t in get_traders() if t["id"] != tid]
        save_traders(traders)
        self._load_cards()

    def _update_totals(self):
        def task():
            tot_p = tot_h = tot_costo = tot_hedge_cum = 0.0
            for card in self.cards:
                hist = getattr(card, "_trades_history", [])
                for tr in hist:
                    p = float(tr.get("profit", 0) or 0)
                    if tr.get("conto") == "PROP":   tot_p += p
                    elif tr.get("conto") == "HEDGE": tot_h += p
                prop  = getattr(card, "_last_prop",  None)
                hedge = getattr(card, "_last_hedge", None)
                if prop  and prop.get("pnl")  is not None: tot_p += float(prop["pnl"])
                if hedge and hedge.get("pnl") is not None: tot_h += float(hedge["pnl"])
                # Costo prop dal trader dict
                t = card.trader
                tot_costo += float(t.get("costo_prop", 0) or 0)
                # Hedge cumulativo persistente (somma di tutte le operazioni chiuse + live)
                hedge_cum = getattr(card, '_hedge_pnl_cum', 0.0)
                hedge_live = float(hedge["pnl"]) if hedge and hedge.get("pnl") is not None else 0.0
                tot_hedge_cum += hedge_cum + hedge_live
            try:
                # Netto = hedge P&L cumulativo (tutte le operazioni) - costo prop
                netto = tot_hedge_cum - tot_costo
                col = GREEN if netto >= 0 else RED
                self.lbl_tot_net.configure(
                    text=("+" if netto >= 0 else "") + "${:,.2f}".format(netto),
                    text_color=col)
            except: pass
        threading.Thread(target=task, daemon=True).start()
        self.after(3000, self._update_totals)

    def panic_all(self):
        def task():
            n = 0
            for t in get_traders():
                n += chiudi_posizioni(t.get("broker_prop_path",  ""))
                n += chiudi_posizioni(t.get("broker_hedge_path", ""))
            self.lbl_live.configure(text="PANIC: " + str(n) + " chiuse!", text_color=RED)
        threading.Thread(target=task, daemon=True).start()

    def _open_logs_folder(self):
        """Apre Esplora file nella cartella dei log (in AppData).
        Crea la cartella se non esiste, così il bottone funziona anche al primo avvio."""
        try:
            log_dir = os.path.join(DATA_DIR, "logs")
            os.makedirs(log_dir, exist_ok=True)
            # Su Windows: apre Esplora file
            try:
                os.startfile(log_dir)
            except AttributeError:
                # Fallback per altri OS (Linux/Mac, anche se non supportati)
                import subprocess
                if sys.platform == "darwin":
                    subprocess.Popen(["open", log_dir])
                else:
                    subprocess.Popen(["xdg-open", log_dir])
        except Exception as e:
            try: messagebox.showerror("Errore", f"Impossibile aprire la cartella log:\n{e}")
            except: pass

    def _poll_commands(self):
        """Controlla ogni 2s se ci sono comandi remoti dal server."""
        global _pending_command, _pending_params, _pending_trader_index, _server_license_days
        # Aggiorna label giorni licenza dal server
        if _server_license_days is not None:
            try:
                days = _server_license_days
                days_color = RED if days <= 7 else GOLD
                self.lbl_licenza.configure(
                    text="Licenza: {}gg".format(days),
                    text_color=days_color)
            except: pass
        cmd  = _pending_command
        tidx = _pending_trader_index
        if cmd:
            _pending_command      = None
            _pending_trader_index = None
            # Seleziona le card target
            if tidx is not None and 0 <= tidx < len(self.cards):
                target_cards = [self.cards[tidx]]
            else:
                target_cards = self.cards
            if cmd == "panic":
                self.lbl_live.configure(text="⚠ PANIC REMOTO!", text_color=RED)
                self.after(300000, lambda: self.lbl_live.configure(text="SISTEMA PRONTO", text_color=GREEN))
                for card in target_cards:
                    try: card.panic_trader()
                    except: pass
            elif cmd == "buy":
                self.lbl_live.configure(text="📡 BUY REMOTO", text_color=GREEN)
                self.after(300000, lambda: self.lbl_live.configure(text="SISTEMA PRONTO", text_color=GREEN))
                for card in target_cards:
                    try: threading.Thread(target=card._execute_buy_direct, daemon=True).start()
                    except: pass
            elif cmd == "sell":
                self.lbl_live.configure(text="📡 SELL REMOTO", text_color=RED)
                self.after(300000, lambda: self.lbl_live.configure(text="SISTEMA PRONTO", text_color=GREEN))
                for card in target_cards:
                    try: threading.Thread(target=card._execute_sell_direct, daemon=True).start()
                    except: pass
            elif cmd == "reset_hedge_lots":
                # Reset remoto cumulativo lotti hedge (da pannello admin/distributore)
                # Esecuzione silenziosa: il cliente non deve vedere alcun messaggio
                for card in target_cards:
                    try:
                        card._hedge_lots_cum = 0.0
                        _save_hedge_lots(card._trader_id, 0.0)
                    except: pass
            elif cmd == "reset_hedge_pnl_cum":
                # Reset remoto cumulativo P&L hedge (chiusura ciclo per cambio costo_prop)
                # Triggered automaticamente dal server quando rileva costo_prop modificato
                for card in target_cards:
                    try:
                        card._hedge_pnl_cum = 0.0
                        _save_hedge_cum(card._trader_id, 0.0)
                    except: pass

        # Applica parametri remoti se presenti (priorità server)
        params = _pending_params
        if params:
            _pending_params = None
            # Usa trader_index se presente nei params
            pidx = params.get("trader_index")
            if pidx is not None and 0 <= int(pidx) < len(self.cards):
                param_cards = [self.cards[int(pidx)]]
            else:
                param_cards = self.cards
            for card in param_cards:
                try:
                    t = card.trader
                    # Mappa diretta tutti i campi
                    mapping = {
                        "prop_firm":          ("prop_firm",          str),
                        "size":               ("size",               float),
                        "stop_prop":          ("stop_prop",          float),
                        "target_hedge":       ("target_hedge",       float),
                        "simbolo_prop":       ("simbolo_prop",       str),
                        "simbolo_hedge":      ("simbolo_hedge",      str),
                        "distanza":           ("distanza",           int),
                        "costo_prop":         ("costo_prop",         float),
                        "fase":               ("fase",               str),
                        "dd_giornaliero_pct": ("dd_giornaliero_pct", float),
                        "dd_stop_pct":        ("dd_stop_pct",        float),
                        "dd_totale_pct":      ("dd_totale_pct",      float),
                        "target_pct":         ("target_pct",         float),
                    }
                    for key, (tkey, cast) in mapping.items():
                        if key in params and params[key] is not None:
                            t[tkey] = cast(params[key])
                    # Aggiorna anche i campi visibili nell'edit
                    fields = {
                        "e_firm":      ("prop_firm",          str),
                        "e_size":      ("size",               str),
                        "e_stop":      ("stop_prop",          str),
                        "e_target_h":  ("target_hedge",       str),
                        "e_simbolo_p": ("simbolo_prop",       str),
                        "e_simbolo_h": ("simbolo_hedge",      str),
                        "e_distanza":  ("distanza",           str),
                        "e_dd_day":    ("dd_giornaliero_pct", str),
                        "e_dd_stop":   ("dd_stop_pct",        str),
                        "e_dd_tot":    ("dd_totale_pct",      str),
                        "e_tgt":       ("target_pct",         str),
                    }
                    for attr, (tkey, _) in fields.items():
                        try:
                            if hasattr(card, attr) and tkey in t:
                                widget = getattr(card, attr)
                                widget.delete(0, "end")
                                widget.insert(0, str(t[tkey]))
                        except Exception as _fe:
                            print(f"[PARAMS] errore campo {attr}: {_fe}")
                    if hasattr(card, 'fase_var') and "fase" in t:
                        try: card.fase_var.set(t["fase"])
                        except Exception as _fve: print(f"[PARAMS] errore fase: {_fve}")
                    # Applica auto_ratio se presente
                    if "auto_ratio" in params and hasattr(card, '_auto_ratio_var') and card._auto_ratio_var:
                        try:
                            card._auto_ratio_var.set(bool(params["auto_ratio"]))
                            card._on_auto_ratio_change()
                        except: pass
                    # Applica ratio manuale se auto_ratio è OFF
                    if "ratio_prop" in params and hasattr(card, 'ratio_prop_var') and card.ratio_prop_var:
                        try:
                            if not params.get("auto_ratio", True):
                                rp_val = params["ratio_prop"]
                                card.ratio_prop_var.set(rp_val)
                                card._update_hedge_ratio(rp_val)
                                # *** FIX: aggiorna anche il dict trader così _calc_dynamic_ratio lo legge ***
                                rp_parts = rp_val.split(":")
                                rh_val = "{}:{}".format(rp_parts[1], rp_parts[0]) if len(rp_parts)==2 else params.get("ratio_hedge","3:1")
                                card.trader["ratio_prop"]  = rp_val
                                card.trader["ratio_hedge"] = rh_val
                        except: pass
                    # Salva i trader aggiornati direttamente dalle card
                    updated_traders = [c.trader for c in self.cards]
                    save_traders(updated_traders)
                    with _heartbeat_lock:
                        _heartbeat_data["params_received"] = True
                    self.lbl_live.configure(text="⚙ PARAMETRI AGGIORNATI", text_color=GOLD)
                    self.after(3000, lambda: self.lbl_live.configure(text="SISTEMA PRONTO", text_color=GREEN))
                except Exception as _pe:
                    print(f"[PARAMS] errore applicazione: {_pe}")
        # Aggiorna dati traders nel heartbeat — tutti i dati live (EA non usato)
        try:
            traders_snap = []
            for card in self.cards:
                t     = card.trader
                prop  = getattr(card, '_last_prop',  None)
                hedge = getattr(card, '_last_hedge', None)
                traders_snap.append({
                    "prop_firm":          t.get("prop_firm", ""),
                    "size":               t.get("size", 0),
                    "fase":               t.get("fase", "Fase 1"),
                    "dd_giornaliero_pct": float(t.get("dd_giornaliero_pct", 5)),
                    "dd_totale_pct":      float(t.get("dd_totale_pct", 10)),
                    "target_pct":         float(t.get("target_pct", 10)),
                    "simbolo_prop":       t.get("simbolo_prop", ""),
                    "simbolo_hedge":      t.get("simbolo_hedge", ""),
                    "costo_prop":         float(t.get("costo_prop", 0) or 0),
                    # Dati live da MT5
                    "prop_bal":           prop["balance"]   if prop  else 0,
                    "prop_pnl":           prop["pnl"]       if prop  else 0,
                    "hedge_bal":          hedge["balance"]  if hedge else 0,
                    "hedge_pnl":          hedge["pnl"]      if hedge else 0,
                    "positions":          (prop["positions"] if prop else 0),
                    "hedge_pnl_cum":      round(getattr(card, '_hedge_pnl_cum', 0.0), 2),
                    "hedge_pnl_day":      round(getattr(card, '_hedge_pnl_day_cache', 0.0) or 0.0, 2),
                    "hedge_lots_cum":     round(getattr(card, '_hedge_lots_cum', 0.0), 2),
                    # Broker e conto MT5
                    "prop_account":       prop.get("account", "")  if prop  else "",
                    "prop_broker":        prop.get("broker", "")   if prop  else "",
                    "hedge_account":      hedge.get("account", "") if hedge else "",
                    "hedge_broker":       hedge.get("broker", "")  if hedge else "",
                    "prop_bal_day_start": card._balance_start_day if hasattr(card, '_balance_start_day') and card._balance_start_day else (prop["balance"] if prop else 0),
                    "prop_pnl_day":       getattr(card, '_pnl_day_cache', None),  # P&L giornaliero netto (escl. deposit/prelievi) — None se non ancora letto
                    "trades_log":         getattr(card, '_trades_log', []),
                    # Dati per preview ordini da web (mypanel/admin/distrib)
                    "stop_prop":          float(t.get("stop_prop", 1000) or 1000),
                    "target_hedge":       float(t.get("target_hedge", 100) or 100),
                    "ratio_prop":         t.get("ratio_prop", "1:4"),
                    "ratio_hedge":        t.get("ratio_hedge", "4:1"),
                    "lotto_prop":         round(getattr(card, '_lotto_prop', 0.01) or 0.01, 2),
                    "lotto_hedge":        round(getattr(card, '_lotto_hedge', 0.01) or 0.01, 2),
                    "auto_ratio":         bool(t.get("auto_ratio", True)),
                    "last_status":        getattr(card, '_last_status_msg', ""),
                    "last_status_color":  getattr(card, '_last_status_color', ""),
                    "last_status_ts":     getattr(card, '_last_status_ts', ""),
                })
            update_heartbeat_traders(traders_snap)
        except: pass

        # Aggiorna notifica messaggi
        n = _msg_counter["n"]
        if n > 0:
            self.lbl_msg_notif.configure(
                text=f"📩 {n}",
                text_color="#FFFFFF",
                fg_color="#CC0033",
                border_width=2,
                border_color="#FF3355")
            self._blink_messages()
        else:
            self.lbl_msg_notif.configure(
                text="✉",
                text_color=GRAY,
                fg_color=CARD2,
                border_width=0)

        self.after(2000, self._poll_commands)

    def _show_archive(self):
        """Finestra archivio schede — scarica dal server."""
        win = ctk.CTkToplevel(self)
        win.title("📁 Archivio Schede")
        win.geometry("720x600")
        win.configure(fg_color=BG)
        win.grab_set()

        ctk.CTkLabel(win, text="📁 ARCHIVIO SCHEDE",
                     font=("Segoe UI", 15, "bold"),
                     text_color=GOLD).pack(pady=(18, 2))
        ctk.CTkLabel(win, text="Schede archiviate automaticamente al raggiungimento di target o DD max",
                     font=("Segoe UI", 10), text_color=GRAY).pack(pady=(0, 12))
        ctk.CTkFrame(win, fg_color=BORDER, height=1).pack(fill="x", padx=20)

        sc = ctk.CTkScrollableFrame(win, fg_color="transparent")
        sc.pack(fill="both", expand=True, padx=20, pady=10)

        lbl_loading = ctk.CTkLabel(sc, text="Caricamento...",
                                    font=("Segoe UI", 12), text_color=GRAY)
        lbl_loading.pack(pady=30)

        def fetch():
            try:
                cid = get_client_id()
                pw  = get_client_password()
                req = urllib.request.Request(
                    SERVER_URL + "/api/archive/" + cid,
                    headers={"Content-Type": "application/json",
                             "X-Client-Password": pw or ""},
                    method="GET")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                win.after(0, lambda: render(data))
            except Exception as e:
                win.after(0, lambda: lbl_loading.configure(
                    text="Errore: {}".format(str(e)), text_color=RED))

        def render(data):
            for w in sc.winfo_children():
                w.destroy()
            if not data.get("ok"):
                ctk.CTkLabel(sc, text="Errore nel recupero archivio",
                             font=("Segoe UI", 13), text_color=RED).pack(pady=40)
                return
            archive = data.get("archive") or []
            if not archive:
                ctk.CTkLabel(sc, text="Nessuna scheda archiviata",
                             font=("Segoe UI", 13), text_color=GRAY).pack(pady=40)
                return
            # Mostra dal più recente
            for entry in reversed(archive):
                esito    = entry.get("esito", "—")
                color    = GREEN if esito == "PASSATA" else RED
                dt_raw   = entry.get("archived_at", "")
                try:
                    dt = datetime.strptime(dt_raw[:19], "%Y-%m-%dT%H:%M:%S").strftime("%d/%m/%Y %H:%M")
                except:
                    dt = dt_raw[:10] if dt_raw else "—"
                firm     = entry.get("prop_firm", "—")
                size     = float(entry.get("size", 0))
                fase     = entry.get("fase", "—")
                costo    = float(entry.get("costo_prop", 0))
                h_cum    = float(entry.get("hedge_cum", 0))
                guad     = float(entry.get("guadagno_hedge", 0))
                p_cum    = float(entry.get("prop_cum", 0))

                card = ctk.CTkFrame(sc, fg_color=CARD, corner_radius=12,
                                    border_width=2, border_color=color)
                card.pack(fill="x", pady=6)

                # Header
                hdr = ctk.CTkFrame(card, fg_color="transparent")
                hdr.pack(fill="x", padx=14, pady=(10, 4))
                ctk.CTkLabel(hdr, text="● " + esito,
                             font=("Segoe UI", 13, "bold"),
                             text_color=color).pack(side="left")
                ctk.CTkLabel(hdr, text=dt,
                             font=("Segoe UI", 10), text_color=GRAY).pack(side="left", padx=10)
                ctk.CTkLabel(hdr, text="{} — ${}".format(firm, "{:,.0f}".format(size)),
                             font=("Segoe UI", 11, "bold"),
                             text_color=WHITE).pack(side="left", padx=6)
                ctk.CTkLabel(hdr, text=fase,
                             font=("Segoe UI", 10), text_color=BLUE).pack(side="right")

                # Dati
                row = ctk.CTkFrame(card, fg_color=CARD2, corner_radius=8)
                row.pack(fill="x", padx=14, pady=(2, 10))
                for i in range(4):
                    row.columnconfigure(i, weight=1)

                def stat_cell(parent, lbl, val, col, val_color=WHITE):
                    f = ctk.CTkFrame(parent, fg_color="transparent")
                    f.grid(row=0, column=col, padx=8, pady=8, sticky="nsew")
                    ctk.CTkLabel(f, text=lbl, font=("Segoe UI", 9),
                                 text_color=GRAY).pack()
                    ctk.CTkLabel(f, text=val, font=("Segoe UI", 13, "bold"),
                                 text_color=val_color).pack()

                def fmtv(v):
                    return ("+" if v >= 0 else "") + "${:,.2f}".format(v)

                stat_cell(row, "COSTO PROP", "-€{:,.2f}".format(costo), 0, RED)
                stat_cell(row, "PROP CUM",   fmtv(p_cum), 1, GREEN if p_cum >= 0 else RED)
                stat_cell(row, "HEDGE CUM",  fmtv(h_cum), 2, GREEN if h_cum >= 0 else RED)
                stat_cell(row, "GUADAGNO HEDGE", fmtv(guad), 3, GREEN if guad >= 0 else RED)

        threading.Thread(target=fetch, daemon=True).start()

        ctk.CTkButton(win, text="✕ CHIUDI", width=140, height=34,
                      fg_color=CARD2, hover_color=BORDER,
                      text_color=WHITE, font=("Segoe UI", 11, "bold"),
                      command=win.destroy).pack(pady=(0, 14))

    def _blink_messages(self):
        """Lampeggia il pulsante messaggi finché ci sono non letti."""
        if _new_messages <= 0:
            return
        self._blink_state = not self._blink_state
        try:
            self.lbl_msg_notif.configure(
                fg_color="#CC0033" if self._blink_state else "#8B0000")
        except: pass
        self.after(500, self._blink_messages)

    def _show_messages(self):
        """Mostra popup con gli ultimi 3 messaggi e segna come letti."""
        msgs = _pending_messages[-3:] if _pending_messages else []

        win = ctk.CTkToplevel(self)
        win.title("Messaggi")
        win.geometry("420x400")
        win.configure(fg_color=BG)
        win.grab_set()

        ctk.CTkLabel(win, text="✉ MESSAGGI RICEVUTI",
                     font=("Segoe UI", 13, "bold"),
                     text_color="#9B59B6").pack(pady=(16, 8))
        ctk.CTkFrame(win, fg_color="#9B59B6", height=1).pack(fill="x", padx=20, pady=0)

        sc = ctk.CTkScrollableFrame(win, fg_color="transparent", height=180)
        sc.pack(fill="both", expand=True, padx=16, pady=8)

        if not msgs:
            ctk.CTkLabel(sc, text="Nessun messaggio ricevuto.",
                         font=("Segoe UI", 11), text_color=GRAY).pack(pady=20)
        else:
            for m in reversed(msgs):
                frame = ctk.CTkFrame(sc, fg_color=CARD, corner_radius=8)
                frame.pack(fill="x", pady=4)
                ctk.CTkLabel(frame, text=m.get("ts", ""),
                             font=("Segoe UI", 9), text_color=GRAY).pack(anchor="w", padx=10, pady=(6,0))
                ctk.CTkLabel(frame, text=m.get("text", ""),
                             font=("Segoe UI", 11), text_color=WHITE,
                             wraplength=360, justify="left").pack(anchor="w", padx=10, pady=(2,6))

        # Box per rispondere
        reply_frame = ctk.CTkFrame(win, fg_color="transparent")
        reply_frame.pack(fill="x", padx=16, pady=4)
        e_reply = ctk.CTkEntry(reply_frame, placeholder_text="Scrivi qui la tua risposta...",
                               height=34, fg_color=CARD, border_color=BORDER)
        e_reply.pack(side="left", fill="x", expand=True, padx=(0, 6))

        def send_reply():
            text = e_reply.get().strip()
            if not text: return
            e_reply.delete(0, "end")
            def _do_send():
                try:
                    cid = get_client_id()
                    pw = get_password()
                    data = json.dumps({"text": text}).encode('utf-8')
                    req = urllib.request.Request(
                        SERVER_URL + "/api/reply_message/" + cid,
                        data=data,
                        headers={"Content-Type": "application/json", "X-Client-Password": pw or ""},
                        method="POST")
                    # Aggiornamento locale UI
                    global _pending_messages
                    from datetime import datetime
                    _pending_messages.append({
                        "id": "temp_" + str(int(datetime.now().timestamp())),
                        "text": text,
                        "ts": datetime.now().strftime("%d/%m %H:%M"),
                        "from": "client",
                        "read": True,
                        "read_exe": True
                    })
                    
                    def _on_success():
                        win.destroy()
                        self._open_messages()
                        messagebox.showinfo("Inviato", "Risposta inviata con successo all'amministratore!", parent=self)
                        
                    self.after(0, _on_success)
                except Exception as e:
                    self.after(0, lambda: messagebox.showerror("Errore", "Impossibile inviare: " + str(e), parent=win))
            threading.Thread(target=_do_send, daemon=True).start()

        ctk.CTkButton(reply_frame, text="INVIA", width=60, height=34,
                      fg_color="#0A3D1F", hover_color="#145214",
                      text_color=GREEN, font=("Segoe UI", 11, "bold"),
                      command=send_reply).pack(side="right")

        # Email assistenza
        ctk.CTkFrame(win, fg_color=BORDER, height=1).pack(fill="x", padx=20, pady=(8,0))
        ctk.CTkLabel(win, text="📧 Assistenza: info@arbexa.pro",
                     font=("Segoe UI", 10), text_color=BLUE).pack(pady=4)

        ctk.CTkButton(win, text="✓ CHIUDI", width=160, height=34,
                      fg_color=CARD2, hover_color=BORDER,
                      text_color=WHITE, font=("Segoe UI", 11, "bold"),
                      command=win.destroy).pack(pady=(0, 12))

        # Segna come letti sul server
        def mark_read():
            try:
                cid = get_client_id()
                req = urllib.request.Request(
                    SERVER_URL + "/api/mark_read_exe/" + cid,
                    data=b'{}',
                    headers={"Content-Type": "application/json"},
                    method="POST")
                urllib.request.urlopen(req, timeout=5)
                global _new_messages
                _new_messages = 0
                _msg_counter["n"] = 0
            except: pass
        threading.Thread(target=mark_read, daemon=True).start()

#  LOGIN
# ------------------------------------------------

PASSWORD = "sniper2025"  # <-- CAMBIA QUI LA TUA PASSWORD

class LoginApp(ctk.CTkToplevel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.withdraw()  # nascondo subito — mostro dopo _build()
        self.title("Arbexa")
        self.geometry("400x640")
        self.resizable(False, False)
        self.configure(fg_color=BG)
        self.days_left = get_days_left()
        self.client_id = get_client_id()
        self._build()
        self.deiconify()  # mostro solo quando tutto è pronto
        # Se scaduta localmente, chiedi al server
        if self.days_left <= 0:
            self.after(500, self._verify_server)

    def _verify_server(self, force_clean=False):
        """Controlla il server — ha priorità sul file locale.
        force_clean: se True, resetta expiry locale prima di chiedere al server.
        Include fingerprint per consentire al server di attivare il trial 2gg."""
        if force_clean:
            # Reset expiry locale per forzare update dal server
            try:
                lic = _load_license()
                if "expiry" in lic:
                    del lic["expiry"]
                _save_license(lic)
            except: pass
        if hasattr(self, "lbl_err"):
            try:
                self.lbl_err.configure(text="Sincronizzazione con il server...", text_color=GOLD)
            except: pass
        def task():
            try:
                cid     = self.client_id
                payload = json.dumps({
                    "client_id":       cid,
                    "license_days":    0,
                    "traders":         [],
                    "params_received": False,
                    "fingerprint":     _get_fingerprint(),
                }).encode("utf-8")
                req = urllib.request.Request(
                    SERVER_URL + "/api/heartbeat",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST")
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode())
                    expiry = data.get("license_expiry", "")
                    days   = data.get("license_days", 0)
                    blocked = data.get("blocked", False)
                    # Salva expiry SE: server non blocca E ha mandato expiry valida
                    if expiry and not blocked:
                        update_local_expiry(expiry)
                        self.days_left = days
                        self.after(0, self._refresh_ui)
                    elif blocked:
                        self.after(0, lambda: self.lbl_err.configure(
                            text="Account bloccato — contatta il distributore",
                            text_color=RED))
                    else:
                        self.after(0, lambda: self.lbl_err.configure(
                            text="Nessuna licenza attiva sul server",
                            text_color=RED))
            except Exception as e:
                self.after(0, lambda: self.lbl_err.configure(
                    text="Errore connessione al server",
                    text_color=RED))
        threading.Thread(target=task, daemon=True).start()

    def _refresh_ui(self):
        """Ricostruisce la UI dopo verifica server."""
        for w in self.winfo_children():
            w.destroy()
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="ARBEXA",
                     font=("Segoe UI", 22, "bold"),
                     text_color=BLUE).pack(pady=(24, 2))
        ctk.CTkLabel(self, text="V9  |  ACCESSO RISERVATO",
                     font=("Segoe UI", 10), text_color=GRAY).pack(pady=(0, 10))

        # ID Cliente
        id_frame = ctk.CTkFrame(self, fg_color=CARD2, corner_radius=8)
        id_frame.pack(padx=30, fill="x", pady=(0, 10))
        ctk.CTkLabel(id_frame, text="ID Cliente",
                     font=("Segoe UI", 9), text_color=GRAY).pack(pady=(6, 0))
        ctk.CTkLabel(id_frame, text=self.client_id,
                     font=("Segoe UI", 13, "bold"), text_color=GOLD).pack(pady=(0, 4))
        ctk.CTkButton(id_frame, text="📋 COPIA ID", width=160, height=26,
                      corner_radius=6, fg_color="#0a1a3a", hover_color="#1a2a5a",
                      text_color="#4488FF", font=("Segoe UI", 10),
                      command=lambda: [self.clipboard_clear(), self.clipboard_append(self.client_id)]).pack(pady=(0, 6))

        # Stato licenza
        if self.days_left > 0:
            lic_color = GREEN if self.days_left > 7 else GOLD
            lic_text  = "Licenza attiva - {} giorni rimanenti".format(self.days_left)
        else:
            lic_color = RED
            lic_text  = "LICENZA SCADUTA"

        ctk.CTkLabel(self, text=lic_text,
                     font=("Segoe UI", 11, "bold"),
                     text_color=lic_color).pack(pady=(0, 12))

        if self.days_left > 0:
            # === ENTRATA LIBERA — niente password ===
            # Auto-skip alla dashboard dopo breve verifica visiva dello stato licenza
            ctk.CTkLabel(self, text="Accesso in corso...",
                         font=("Segoe UI", 11, "italic"),
                         text_color=GREEN).pack(pady=20)
            self.after(400, self._auto_login)
        else:
            # Licenza scaduta o assente — mostra campo attivazione
            ctk.CTkLabel(self, text="Inserisci il codice di attivazione",
                         font=("Segoe UI", 11), text_color=GRAY).pack(pady=(4,0))
            self.entry_code = ctk.CTkEntry(self, width=240, height=40,
                                            corner_radius=10, font=("Segoe UI", 13),
                                            placeholder_text="Codice licenza")
            self.entry_code.pack(pady=(4, 4))
            self.entry_code.bind("<Return>", lambda e: self._activate())
            self.lbl_err = ctk.CTkLabel(self, text="",
                                         font=("Segoe UI", 11, "bold"), text_color=RED)
            self.lbl_err.pack(pady=2)
            ctk.CTkButton(self, text="ATTIVA LICENZA", width=240, height=42,
                          corner_radius=10, fg_color="#1A3A5C", hover_color="#1E4D7A",
                          font=("Segoe UI", 14, "bold"),
                          command=self._activate).pack(pady=6)
            # Bottone "Sincronizza con server" — utile se la licenza è stata attivata da admin
            ctk.CTkButton(self, text="🔄 SINCRONIZZA CON SERVER", width=240, height=32,
                          corner_radius=8, fg_color="transparent",
                          border_color=GOLD, border_width=1,
                          text_color=GOLD, hover_color="#1A1A1A",
                          font=("Segoe UI", 11, "bold"),
                          command=lambda: self._verify_server(force_clean=True)).pack(pady=(8, 4))
            ctk.CTkLabel(self, text="Se l'admin ha già attivato la licenza,\nclicca qui per scaricare lo stato aggiornato.",
                         font=("Segoe UI", 9), text_color=GRAY,
                         justify="center").pack(pady=(0, 4))
            self.entry_code.focus()

    def _activate(self):
        code = self.entry_code.get().strip()
        if not code:
            self.lbl_err.configure(text="Inserisci il codice!")
            return
        self.lbl_err.configure(text="Verifica in corso...", text_color=GOLD)
        def task():
            ok, msg = apply_renewal_code(code)
            if ok:
                self.days_left = get_days_left()
                self.after(0, self._refresh_ui)
            else:
                self.after(0, lambda: self.lbl_err.configure(
                    text="Codice non valido", text_color=RED))
        threading.Thread(target=task, daemon=True).start()

    def _check_pw(self):
        if self.entry_pw.get() == PASSWORD:
            # Salva password se "Ricordami" attivo
            try:
                if hasattr(self, "_remember_var") and self._remember_var.get():
                    import hashlib
                    lic = _load_license()
                    lic["remember_pw"] = True
                    lic["saved_pw_hash"] = hashlib.sha256(PASSWORD.encode()).hexdigest()
                    _save_license(lic)
            except: pass
            self.destroy()
            app = ArbexaApp()
            app.mainloop()
        else:
            self.lbl_err.configure(text="Password errata!")
            self.entry_pw.delete(0, "end")
            self.after(2000, lambda: self.lbl_err.configure(text=""))

    def _auto_login(self):
        """Auto-login con password salvata."""
        self.destroy()
        app = ArbexaApp()
        app.mainloop()

    def _reload(self):
        self.destroy()
        LoginApp().mainloop()

if __name__ == "__main__":
    # ── SINGLE INSTANCE: Mutex Windows (robusto) + fallback WMIC ─────────────
    import subprocess as _sp
    import ctypes as _ct2

    # Metodo 1: Mutex Windows — il più affidabile, funziona sempre
    _MUTEX_NAME = "ArbeXa_SingleInstance_Mutex_v9"
    _mutex_handle = None
    try:
        _mutex_handle = _ct2.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        _last_err = _ct2.windll.kernel32.GetLastError()
        if _last_err == 183:  # ERROR_ALREADY_EXISTS
            # Un'altra istanza è già in esecuzione — chiudi silenziosamente
            if _mutex_handle:
                _ct2.windll.kernel32.CloseHandle(_mutex_handle)
            sys.exit(0)
    except Exception:
        pass  # Se mutex fallisce, continua con WMIC

    # Metodo 2: WMIC — uccide eventuali istanze zombie rimaste
    _my_pid = os.getpid()
    try:
        _out = _sp.check_output(
            'wmic process where "name=\'arbexa.exe\'" get ProcessId /value',
            shell=True,
            stderr=_sp.DEVNULL,
            creationflags=_sp.CREATE_NO_WINDOW
        ).decode(errors="ignore")
        for _line in _out.splitlines():
            _line = _line.strip()
            if _line.lower().startswith("processid="):
                try:
                    _pid = int(_line.split("=")[1])
                    if _pid != _my_pid:
                        _sp.call(
                            f"taskkill /PID {_pid} /F",
                            shell=True,
                            stderr=_sp.DEVNULL,
                            stdout=_sp.DEVNULL,
                            creationflags=_sp.CREATE_NO_WINDOW
                        )
                except (ValueError, IndexError):
                    pass
    except Exception:
        # Se WMIC fallisce (es. in alcune config Win11) prova con taskkill diretto
        try:
            _sp.call(
                f"taskkill /F /FI \"IMAGENAME eq arbexa.exe\" /FI \"PID ne {_my_pid}\"",
                shell=True,
                stderr=_sp.DEVNULL,
                stdout=_sp.DEVNULL,
                creationflags=_sp.CREATE_NO_WINDOW
            )
        except Exception:
            pass
    # Breve pausa per dare tempo ai processi uccisi di rilasciare file/porte
    time.sleep(0.5)
    # ── fine single instance ──────────────────────────────────────────────────

    # ── DPI AWARENESS — testo nitido su schermi ad alta risoluzione ──────────
    try:
        import ctypes as _ct
        # Per Python 3.8+ e Windows 10+: Per-Monitor DPI Aware V2
        _ct.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            _ct.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    # Disabilita DPI scaling automatico di CustomTkinter (gestiamo noi)
    ctk.deactivate_automatic_dpi_awareness()

    # Root nascosto — unico CTk per tutta l'app
    root = ctk.CTk()
    root.withdraw()

    # Primo avvio: mostra termini e condizioni
    if not has_accepted_terms():
        terms = TermsWindow(root)
        terms.grab_set()
        root.wait_window(terms)
        if not terms._accepted:
            _allow_sleep()
            root.destroy()
            sys.exit(0)

    login = LoginApp(root)
    login.grab_set()
    root.wait_window(login)
    _allow_sleep()
