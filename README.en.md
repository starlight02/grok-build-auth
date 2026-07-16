> [简体中文](README.md) | **English**

# grok-build-auth

A **protocol-research client** for publicly observable **x.ai / Grok web authentication** flows. It reimplements, over pure HTTP:

`signup → SSO → OAuth PKCE (Grok Build / CLI scopes) → local auth JSON export`

for protocol analysis, interoperability research, and **authorized** local integration testing.

Default path: signup/OAuth over pure HTTP (`curl_cffi`). Signup Turnstile uses a local browser backend (`auto`→Drission+turnstilePatch; optional Camoufox / Playwright) with a **background token pool on by default** (depth/minters auto-scale with `-t`). Mailbox defaults to Tempmail free (steady state around `-t 4`). OAuth: protocol session-reuse, Device Flow fallback.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Use](https://img.shields.io/badge/use-research%20%2F%20authorized%20only-red)](#legal-boundary)

---

> [!CAUTION]
> **Using this project constitutes acceptance of all terms in [`NOTICE`](NOTICE).**  
> Provided **AS IS**, with **no warranties**. Maintainers accept **no liability**.  
> **Allowed only** on systems you own / legitimate CTF / authorized bug-bounty in-scope assets / security research & education.  
> **Prohibited:** fraud, bulk account farming for resale, unauthorized targets, intentional ToS abuse.  
> You bear all legal responsibility. If you do not accept the terms, **do not use, do not clone, delete every copy**.

---

## Legal boundary

| | |
|---|---|
| **Allowed** | Your own accounts and environments; clearly authorized security research; CTF / academic protocol study; offline source reading |
| **Prohibited** | Fraud, bulk signup for resale, unlicensed automation against unauthorized targets, intentional platform abuse |
| **Liability** | Account bans, quota loss, civil / criminal / administrative outcomes — **all on the user** |
| **Affiliation** | **Not** affiliated with, endorsed by, or sponsored by xAI, Grok, Cloudflare, CLIProxyAPI, or mailbox vendors |

Full terms: [`NOTICE`](NOTICE). License is [MIT](LICENSE), but **MIT is not the entire disclaimer**.

If you are unsure whether your use is lawful — **do not run**. Ask a lawyer first, or contact the target platform’s security team.

---

## What this is

A research-oriented protocol client, **not** an official SDK.

| Stage | Content |
|---|---|
| **Signup** | Email code (gRPC-web) + Turnstile + Next.js Server Action on `accounts.x.ai` |
| **SSO** | Session JWT extraction for OAuth session reuse |
| **OAuth** | Fast path: `oauth_protocol` SSO session-reuse (PKCE + cookie-setter + consent); fallback: `sso2auth` Device Flow; pure HTTP end-to-end |
| **Export** | Local `type=xai` auth files compatible with [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (Grok Build channel) |

Highlights:

- **Protocol-first** pure HTTP (`curl_cffi`) for signup / OAuth
- **Turnstile pool**: background mint, signup threads only consume; **on by default**; demand-driven (stop when stock covers demand)
- **Auto-tune from `-t`**: pool size / mint concurrency scale with registration threads (env can pin values)
- **OAuth dual path**: SSO session-reuse (`oauth_protocol`); Device Flow fallback (`sso2auth`)
- **Lean outputs**: default writes `sso_output/` + `cliproxyapi_auth/`

CPA export needs OAuth `access_token` / `refresh_token` (protocol path or Device Flow).

---

## Architecture

```mermaid
flowchart LR
    A[run.py<br/>-n / -t] --> P[Turnstile pool<br/>turnstile_pool]
    P -->|mint| S[browser backend<br/>drission / camoufox / browser]
    A --> B[signup client.py<br/>mail code + consume token]
    P -->|token| B
    B --> C[SSO sso.py]
    C --> D{OAuth fast path<br/>oauth_protocol<br/>SSO session-reuse}
    D -->|OK| E[token exchange]
    D -->|fail| F[sso2auth<br/>Device Flow]
    F --> E
    E --> G[cliproxyapi_auth/*.json]
```

---

## Requirements

This is **not** a zero-config product. At minimum you need:

- Python 3.9+
- Turnstile: local browser backend (default Drission + headed Chrome; optional Camoufox / Playwright)
- Mailbox: Tempmail.lol **free tier (no API key)** by default; optional Plus/Ultra key or your Cloudflare D1 alias mailbox
- Optional HTTP(S) proxy
- Optional local CLIProxyAPI install to load exported auth files

Platform terms, risk controls, and API changes may break the flow at any time. Maintainers have **no duty** to keep it working.

---

## Getting started

### Install

```bash
git clone https://github.com/<you>/grok-build-auth.git
cd grok-build-auth
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# optional Camoufox backend:
# pip install camoufox && camoufox fetch
cp .env.example .env
# put only your own secrets in .env — never commit it
```

### Configure

See [`.env.example`](.env.example). Never commit `.env` or runtime token directories. See [`SECURITY.md`](SECURITY.md).

| Variable | Required | Notes |
|---|---|---|
| `TURNSTILE_SOLVER` | no | `auto` (default) / `drission` / `camoufox` / `browser` / `safari` — see [Turnstile backends](#turnstile-backends) |
| `TURNSTILE_HEADLESS` | no | drission/camoufox default `0` (headed); playwright default `1`; camoufox may use `virtual` |
| `TURNSTILE_TIMEOUT` | no | hard wall-clock seconds per mint (**drission default 30**; camoufox/browser default 60) |
| `TURNSTILE_POOL` | no | background token pool (**on by default**; `0` disables) |
| `TURNSTILE_POOL_SIZE` | no | hard max buffered tokens (**auto from `-t`**: `max(2, -t)`, cap 32) |
| `TURNSTILE_POOL_TARGET` | no | idle ready stock (default `min(2, size)`; stop minting when satisfied) |
| `TURNSTILE_POOL_MINTERS` | no | mint threads (**auto from `-t`**: `ceil(-t/4)` cap 4; Safari forced to 1) |
| `TURNSTILE_TOKEN_MAX_AGE` | no | max age of pooled tokens in seconds (default 200) |
| `TURNSTILE_PAUSE_FILE` | no | if present, pause mint/click (default `/tmp/grok-turnstile.pause`) |
| `TURNSTILE_PARALLEL` | no | mint slots **only when pool is off** (default follows `-t`, cap 8) |
| `TURNSTILE_MINIMIZED` | no | drission headed default `1`: minimize via Drission/CDP |
| `TURNSTILE_OFFSCREEN` | no | drission headed default `1`: off-screen window backup |
| `TURNSTILE_BROWSER_CHANNEL` | no | playwright only; auto-selects system `chrome` when available |
| `TURNSTILE_INTERACTIVE` | no | playwright only: `1` = manual click (forces headed) |
| `TURNSTILE_BROWSER_REUSE` | no | `1` = warm browser reuse (default 1; drission warm-page) |
| `TEMPMAIL_API_KEY` | no | Tempmail.lol Plus/Ultra (**free tier needs no key**) |
| `TEMPMAIL_FREE_CREATE_INTERVAL` | no | free-tier create min interval seconds (default **3 ≈ 20/min**) |
| `MAIL_CODE_TIMEOUT` | no | seconds to wait for code before rotating inbox (default 30) |
| `MAIL_MAX_ATTEMPTS` | no | max fresh inboxes when mail is silent (default 3) |
| `CLOUDFLARE_API_TOKEN` | for `-e cloudflare` | CF API token |
| `CLOUDFLARE_ACCOUNT_ID` | same | CF account |
| `CLOUDFLARE_D1_DB_ID` | same | D1 database ID |
| `ALIAS_MAIL_DOMAINS` | same | domains you control (comma-separated) |
| `CLIPROXYAPI_AUTH_DIR` | no | default `./cliproxyapi_auth` |
| `HTTPS_PROXY` / `HTTP_PROXY` | no | proxy |

### Run (research / accounts you own)

```bash
# Zero-config batch: default -t 4 + token pool on + Tempmail free pacing
# pool size/minters follow -t; mint pauses when stock is enough
python run.py -n 20

# Single-account smoke
python run.py -n 1

# Change concurrency (pool auto-retunes; -t 8 → size=8 minters=2)
python run.py -n 20 -t 8

# Pin pool knobs (override auto)
TURNSTILE_POOL_SIZE=6 TURNSTILE_POOL_MINTERS=2 python run.py -n 20 -t 4

# Disable pool (per-thread mint; PARALLEL defaults to -t)
TURNSTILE_POOL=0 python run.py -n 4 -t 2

# Pick a Turnstile backend
TURNSTILE_SOLVER=drission python run.py -n 10 -t 4
TURNSTILE_SOLVER=camoufox python run.py -n 1
TURNSTILE_SOLVER=browser  python run.py -n 1

# Pause mint / HID clicks while you use the machine
touch /tmp/grok-turnstile.pause   # pause
rm    /tmp/grok-turnstile.pause   # resume

python run.py -n 1 -e cloudflare
python run.py -n 1 --no-oauth
python run.py -n 1 --no-oauth-protocol
python run.py -n 1 --cliproxyapi-auth-dir /path/to/CLIProxyAPI/data/auth
python run.py -n 1 --accounts-output-dir ./accounts_output
python run.py -n 1 --oauth-debug
python run.py -n 1 --check-quota
python run.py -n 5 -t 4 --check-quota --failed-auth-dir ./cliproxyapi_auth_failed
```

### Runtime outputs

| Dir | Default | Purpose |
|---|---|---|
| `sso_output/` | **on** | per-account `sso_*.json` (email/password/SSO) + append-only `sso_tokens.txt` (one JWT per line) |
| `cliproxyapi_auth/` | **on** (unless `--no-oauth`) | CLIProxyAPI-ready auth JSON |
| `cliproxyapi_auth_failed/` | only with `--check-quota` | zero-quota auth (override with `--failed-auth-dir`) |
| `oauth_output/` | off | raw OAuth archive (standalone tools / explicit `output_dir`) |
| `accounts_output/` | off | pipeline ledger (`--accounts-output-dir`) |

Helpers:
- `check_accounts.py` — auth usability / Build quota
- `retry_oauth_from_sso.py` — SSO → CPA Device Flow
- `xai_oauth_login.py` — interactive browser OAuth
- `xai_oauth_export_cliproxyapi.py` — export oauth_output → CPA auth

---

## Token pool (default)

Signup threads **only consume** tokens; background minters mint via a local browser. On by default to avoid cold-starting a browser per account and over-minting while idle.

### Why a pool

| Mode | Behavior | Use when |
|---|---|---|
| **Pool on (default)** | Background keeps a small stock; signup HTTP path acquires tokens | Batches / run while working |
| **Pool off** | Each signup thread calls `solve_turnstile` under `TURNSTILE_PARALLEL` | Debugging a single mint |

### Demand-driven minting

- **Idle**: keep only `target` ready tokens (default 2); log `pool mint pause (satisfied …)`
- **Waiters present**: desire expands to `min(size, waiting + target)`
- **Slow mint finishes after demand is covered**: drop the surplus instead of filling the hard cap

### Auto-tune from `-t`

When the matching env vars are unset, `suggest_pool_params(-t)` applies:

| Param | Auto rule | Examples |
|---|---|---|
| `size` | `clamp(-t, 2..32)` | `-t4→4`, `-t8→8` |
| `target` | `min(2, size)` | idle stock of 2 |
| `minters` | `ceil(-t/4)` cap 4; **Safari forced to 1** | `-t4→1`, `-t8→2`, `-t16→4` |
| `PARALLEL` (pool off) | `min(8, -t)` | matches registration concurrency |

Explicit `TURNSTILE_POOL_SIZE` / `_TARGET` / `_MINTERS` / `TURNSTILE_PARALLEL` win over auto.

### Drission warm-page mint (recommended)

- Each worker **navigates signup once**
- Later mints reuse the page (`force-render` + CDP click) ≈ **2.3–2.5s/token** (first cold mint ≈ 10–16s)
- Headed defaults: **minimized + off-screen** to avoid stealing OS focus

### Tempmail free alignment

- CLI default **`-t 4`** targets free-tier steady throughput
- Without `TEMPMAIL_API_KEY`, inbox `create` is paced at **3s** (≈20/min) process-wide
- Plus/Ultra key skips free create pacing; raise `-t` if needed

### How to read startup logs

```text
grok-build-auth: 20 accounts, 4 threads, ... turnstile=auto, pool=on
  turnstile-pool: size=4 target=2 minters=1 max_age=200s (auto from -t=4)
  [ts-pool] pool +1 len=837 q=1/4 want=2 wait=0
  [ts-pool] pool mint pause (satisfied q=2/4 want=2 waiting=0)
  [3/20] [#2] Turnstile 837 chars from pool (age=6s q=1)
```

---

## Turnstile backends

Signup is pure HTTP; **only the Cloudflare Turnstile token must be minted by a local browser**.  
Select a backend with `TURNSTILE_SOLVER` (or `resolve_turnstile_solver(backend=...)`).

### Which one?

| `TURNSTILE_SOLVER` | Stack | Headed by default? | When to use |
|---|---|---|---|
| **`auto` (default)** | DrissionPage installed → **drission**; else → **browser** | follows backend | daily default |
| **`drission`** | DrissionPage + system **Chrome** + `turnstilePatch/` | **yes** (`0`) | **recommended** for warm-page pool batches |
| **`camoufox`** | **Camoufox** anti-detect Firefox (via Playwright) | **yes** (`0`) | Firefox / anti-detect; needs `camoufox fetch` |
| **`browser`** | Playwright Chromium/Chrome | **no** (`1`) | fallback without Drission |
| **`safari`** | system Safari (macOS) | steals focus | manual/single path; pool minters fixed at 1 |

Aliases:

- drission: `dp` / `clean` / `drissionpage`
- camoufox: `camou` / `camoufox-firefox`
- browser: `local` / `playwright` / `chromium` / `chrome` / `free`
- safari: `webkit-system` / `system-safari`

### Dependencies

```bash
pip install -r requirements.txt

# drission (default path): system Google Chrome required
# turnstilePatch/ ships in-repo

# camoufox extra:
pip install camoufox
camoufox fetch
```

### Commands

```bash
# default = auto → drission + pool on + -t 4
python run.py -n 20

TURNSTILE_SOLVER=drission python run.py -n 10 -t 4
TURNSTILE_SOLVER=camoufox python run.py -n 1
TURNSTILE_SOLVER=camoufox TURNSTILE_HEADLESS=virtual python run.py -n 1
TURNSTILE_SOLVER=browser python run.py -n 1
TURNSTILE_SOLVER=browser TURNSTILE_HEADLESS=0 python run.py -n 1
```

### Related env vars

| Variable | Default | Notes |
|---|---|---|
| `TURNSTILE_SOLVER` | `auto` | backend picker |
| `TURNSTILE_HEADLESS` | drission/camoufox=`0`; browser=`1` | `0` headed; `1` headless; camoufox also `virtual` |
| `TURNSTILE_TIMEOUT` | drission=`30`; others=`60` | hard timeout per mint |
| `TURNSTILE_POOL` | **on** | `0` disables pool |
| `TURNSTILE_POOL_SIZE` | from `-t` | hard buffer cap |
| `TURNSTILE_POOL_TARGET` | `min(2,size)` | idle stock; pause when satisfied |
| `TURNSTILE_POOL_MINTERS` | from `-t` | background mint threads; Safari=1 |
| `TURNSTILE_PARALLEL` | from `-t` (pool off only) | live mint concurrency, cap 8 |
| `TURNSTILE_MINIMIZED` | `1` (headed) | minimize window |
| `TURNSTILE_OFFSCREEN` | `1` (headed) | off-screen backup |
| `TURNSTILE_BROWSER_REUSE` | `1` | warm reuse / warm page |
| `TURNSTILE_DEBUG` | off | `1` = verbose solver logs |
| `TURNSTILE_BROWSER_CHANNEL` | auto | browser only: prefer system Chrome |
| `TURNSTILE_INTERACTIVE` | off | browser only: manual click |
| `HTTPS_PROXY` / `HTTP_PROXY` | empty | proxy for browser + protocol HTTP |

Notes:

1. Signup / mail / SSO / OAuth use protocol HTTP; Turnstile mint is only needed before create-account.  
2. **Token pool is default**: background mint; signup threads take `from pool`. `TURNSTILE_PARALLEL` applies only when pool is off.  
3. Drission warm-page ≈ 2.4s/token; first cold mint is slower.  
4. OAuth: SSO session-reuse first, Device Flow on failure.  
5. Prefer **headed + minimized/off-screen + warm reuse + pool** for batches.

### How to tell which path ran

```text
# pool mode (default)
[ts-pool] pool +1 len=837 q=2/4 want=2 wait=0
[#3] Turnstile 837 chars from pool (age=4s q=1)

# pool off
[#1] Turnstile 730 chars via DrissionTurnstileSolver
```

---

## Contributing

Contributions welcome **only** for lawful research and authorized use:

1. Protocol adaptations with repro steps / capture diffs  
2. Docs and translations  
3. Robustness (timeouts, retries, error taxonomy)  
4. Redacted research notes — **no** real tokens, emails, or cookies  

PRs/issues that request help with unauthorized abuse or bulk farming will be closed.

Security: private channel only — [`SECURITY.md`](SECURITY.md).

---

## Community

| Channel | Purpose |
|---|---|
| GitHub Issues | Bug reports and PRs (primary entry) |

---

## Disclaimer

> [!IMPORTANT]
> Using this project means you have read, understood, and accepted **all** of [`NOTICE`](NOTICE).  
> If you cannot accept — do not use; delete all copies.

**Summary (NOTICE controls):** AS IS; authorized scope only; user bears all liability; maintainers have no support or adaptation duty; no affiliation with xAI / Grok / Cloudflare / CLIProxyAPI or other named vendors.

License: [MIT](LICENSE) · Notice: [NOTICE](NOTICE) · Security: [SECURITY.md](SECURITY.md)
