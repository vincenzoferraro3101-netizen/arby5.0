# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ArbeXa (formerly "Sniper Pro") v5.0.0: a prop-firm hedging tool. A Windows desktop app opens mirrored
positions on two MetaTrader 5 terminals (a "PROP" account and a "HEDGE" account) and reports to a Flask
server that provides licensing, an admin panel, distributor (reseller) panels, client panels and Telegram
notifications. Code comments, UI strings and variable names are in Italian; keep new code consistent with that.

The repo is a flat upload of a subset of the production files. There is no package layout, no build
system, no test suite and no `requirements.txt`. The history is a single "Add files via upload" commit.

## Components

| File | Role |
|---|---|
| `server.py` | Flask + Flask-SocketIO backend (~3100 lines, ~130 routes). Runs on port 5000. |
| `database.py` | JSON "database" for clients (`clients.json`), atomic write via temp file + rename, guarded by a lock. |
| `sniper_dashboard.py` | CustomTkinter desktop app (~4700 lines), built as `arbexa.exe`. Windows-only (ctypes DPI, mutex, WMIC). |
| `mt5_utils.py` | Thin wrapper over the `MetaTrader5` Python package: lot sizing, orders, close-all, closed trades, daily P&L, spread. |
| `panel.html` | Admin SPA served at `/admin`, auth via `X-Admin-Password` header on every API call. |
| `download.html`, `istruzioni.html`, `termini.html` | Static public pages served at `/download`, `/istruzioni`, `/termini`. |
| `sniperpro_nginx.conf` | Production nginx reverse proxy (sniperpro.online → 127.0.0.1:5000, buffering off, socket.io upgrade). |
| `config.json` | Example of the desktop app's per-user trader config (list of `traders`). |
| `version.txt` | Version string returned by `/api/version`; the desktop app compares it with `APP_VERSION`. |

### Files referenced but NOT in the repo

Do not "fix" these imports by inventing modules; they exist only on the VPS / build machine:

- `core/constants.py` — imported by `sniper_dashboard.py` for colors, fonts, `BASE_DIR`, `DATA_DIR` and all
  data-file paths (`CONFIG_FILE`, `LICENSE_FILE`, `HEDGE_CUM_FILE`, ...). The desktop app will not import without it.
- `mypanel.html` (client panel at `/mypanel`, filled by plain `str.replace` of `{client_id}`-style placeholders),
  `menu.html`, `static/` (logo), `downloads/` (the `.exe` served at `/download/<filename>`).

## Running

Dependencies (infer from imports; there is no requirements file):

```
pip install flask flask-socketio            # server
pip install customtkinter MetaTrader5       # desktop app (Windows only)
```

Server, locally or on the VPS:

```
python server.py            # socketio.run(app, host=0.0.0.0, port=5000)
```

Desktop app (Windows, with `core/constants.py` present):

```
python sniper_dashboard.py
```

There are no lint, build or test commands. Validate Python changes with at least:

```
python -m py_compile server.py database.py mt5_utils.py
```

`sniper_dashboard.py` cannot be compiled-checked here without `core/constants.py` and the Windows-only packages;
`py_compile` still works for syntax, imports fail at runtime.

### Server environment variables

`ARBEXA_SECRET` (Flask session), `ARBEXA_ADMIN_PW`, `ARBEXA_LICENSE_PW`, `ARBEXA_LKEY` (license signing key),
`TELEGRAM_BOT_TOKEN`. All have hardcoded defaults in `server.py`; the Telegram token and `GESTIONALE_PW` are
hardcoded in source. Do not copy those values into docs, commits or new files.

### Paths

`server.py` and `database.py` both pick `BASE_DIR = /root/sniper` if that directory exists (the VPS), otherwise
the script's own directory. All JSON state files, HTML pages and `downloads/` are resolved relative to `BASE_DIR`.

## Architecture: how the pieces talk

**Desktop → server heartbeat (the core loop).** Every 5 s the app POSTs `/api/heartbeat` with
`client_id`, `license_days`, the full `traders` list and a machine `fingerprint`. The response carries back
`license_days`/`license_expiry` (server is the source of truth), `blocked`, chat `messages`, `params` and at most
one `command`. There is no push channel to the desktop; everything the admin or distributor does reaches the
app only through the next heartbeat response.

**Commands.** `/api/command/<cid>` (admin or distributor) sets `pending_command` + `pending_trader_index` +
`pending_command_ts` on the client; it expires after `CMD_TTL = 30` s (`_get_valid_command`). The desktop app
understands `panic`, `buy`, `sell`, `reset_hedge_lots`, `reset_hedge_pnl_cum`. The server itself emits
`reset_hedge_pnl_cum` when it detects a `costo_prop` change in a heartbeat, after archiving the previous cycle
into `broker_history.json`. Add new commands in both `send_command`'s consumers and the `cmd == ...` chain in
`sniper_dashboard.py` (~line 4000).

**State is in-memory dicts persisted to JSON.** At import time `server.py` loads `clients`, `distributors`,
`trial_fingerprints`, `broker_history`, `telegram_links` into module-level globals and mutates them in place.
Persistence paths:

- `clients`: either `save_client(cid, obj)` immediately, or `mark_dirty(cid)` to be flushed by the 10 s
  `_batch_save_loop` thread. Heartbeat uses the dirty path; admin actions save immediately.
- Other dicts: each has its own `save_*()` that dumps the whole file.
- `database.py` is only used for clients; the rest is open/json.dump inline in `server.py`.

A restart loses anything not yet flushed. Deleted client IDs are kept in `_deleted_clients` so a stale
heartbeat does not resurrect them.

**Three roles, three auth schemes, all header based** (no Flask-Login, sessions only for distributor panel pw):

- Admin: `X-Admin-Password == ADMIN_PASSWORD`.
- Distributor: `X-Distributor-Id` + `X-Distributor-Password`, and the client must be in
  `distributors[did]["clients"]`. Distributor routes are prefixed `dist_*` / `distributor_*` and usually mirror an
  admin route with the extra assignment check.
- Client: `client_password` stored as salted SHA-256 (`_hash_pw`), with a legacy plain-text fallback and
  lazy migration in `_check_pw` / `_migrate_pw`.

Many routes accept admin OR distributor credentials with the same `if admin_pw == ... elif dist_id ...`
block; copy that pattern when adding a route that both roles need.

**Licensing.** `generate_license(cid, days)` = base64(XOR(`cid|expiry|sha256(LKEY+raw)[:16]`, LKEY)).
The desktop app activates via `/api/activate_license`; trial (2 days) is granted server-side based on the
machine fingerprint stored in `trial_fingerprints.json`. `license_expiry` (ISO date) on the client object
wins over `license_days`; `_calc_license_days` derives days from it.

**Cumulative hedge accounting.** Each trader keeps `hedge_pnl_cum` / `hedge_lots_cum` (client-side cycle totals,
reset when the prop account cost changes) and `admin_cumulative_hedge` / `admin_cumulative_lots` (server-only,
accumulates positive deltas across resets, used for distributor billing in `_calc_distributor_amount`).
Trades are de-duplicated with `_cum_key` (ticket, else symbol+time+profit). Changing how cumulatives merge affects
billing; check `merge_client_cumulative`, `merge_cumulative` and the heartbeat block together.

**Telegram.** Outgoing via `send_telegram` (plain `urllib` to the Bot API). Incoming via `/api/telegram/webhook`,
which dispatches `_tg_*` command handlers for clients and distributors linked through `/api/telegram/link`
(link codes from `gen_link_code`). `notify_admins` sends to every client flagged `is_admin`. A daemon thread
`_daily_recap_loop` sends recaps to linked clients at a fixed UTC hour (the comment says 22:00, the code checks 20:00).

**MT5 access.** `mt5_utils.py` deliberately does `mt5.initialize(path)` + `mt5.shutdown()` on every call, with a lock
only around `initialize`. This is what lets the PROP and HEDGE terminals be driven from two threads without
hanging; do not introduce a long-lived MT5 session or a global lock around whole calls.

**Front end.** All HTML is hand-written single-file pages with inline CSS/JS; no bundler, no framework. Admin
panel JS keeps the password in `APW` and sends it on every `fetch`. `panel.html` and `/distributor/<id>` go
through `render_template_string`, so literal `{{ }}` or `{% %}` in those files are interpreted by Jinja.
Socket.IO is only used server→browser (`socketio.emit("gestionale_update", ...)`) for the `/gestionale` page.

## Conventions worth keeping

- Exceptions are swallowed with bare `except: pass` almost everywhere; a failing heartbeat or Telegram call must
  never crash a thread. Follow that in background threads, but return a JSON error with a status code in routes.
- API responses are `{"ok": bool, ...}`; errors use `{"ok": False, "error": "<Italian message>"}` with 400/403/404.
- Money values are rounded to 2 decimals at write time; numeric fields from JSON are coerced with
  `float(x or 0)` because clients may send strings or nulls.
- Trader indexes are positional (`trader_index` / `idx` into `clients[cid]["traders"]`), not IDs. Deleting a
  trader shifts indexes.
- Every public page is Italian; keep user-facing strings in Italian.
- Version bumps: update `version.txt` (server) and `APP_VERSION` in `sniper_dashboard.py` together.
