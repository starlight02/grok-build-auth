> [简体中文](README.md) | **English**

# grok-build-auth

A **protocol-research client** for publicly observable **x.ai / Grok web authentication** flows. It reimplements, over pure HTTP:

`signup → SSO → OAuth PKCE (Grok Build / CLI scopes) → local auth JSON export`

for protocol analysis, interoperability research, and **authorized** local integration testing.

Default path: signup/OAuth over pure HTTP (`curl_cffi`). **Turnstile uses a local browser** (system Chrome `--headless=new` by default; set `TURNSTILE_HEADLESS=0` / `TURNSTILE_INTERACTIVE=1` for headed / manual click).

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
| **OAuth** | `auth.x.ai` PKCE + cookie-setter + consent; CreateSession fallback |
| **Export** | Local `type=xai` auth files compatible with [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (Grok Build channel) |

Highlights:

- **Protocol-first** pure HTTP (`curl_cffi`) for signup / OAuth  
- **Turnstile**: system Chrome headless by default (`TURNSTILE_SOLVER=browser`); headed / interactive via env  
- **SSO reuse** can skip a second Turnstile on OAuth  
- **Lean outputs**: default writes only `sso_output/` + `cliproxyapi_auth/`  

SSO **alone cannot** become a CPA auth file; OAuth tokens are required.

---

## Architecture

```mermaid
flowchart LR
    A[run.py] --> B[signup client.py]
    B --> C[SSO sso.py]
    C --> D[OAuth oauth_protocol.py]
    D --> E[token exchange]
    E --> F[cliproxyapi_auth/*.json]
```

---

## Requirements

- Python 3.9+
- Turnstile: local Google Chrome (default headless; optional headed/manual)  
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
cp .env.example .env
# put only your own secrets in .env — never commit it
```

### Configure

See [`.env.example`](.env.example). Never commit `.env` or runtime token directories. See [`SECURITY.md`](SECURITY.md).

| Variable | Required | Notes |
|---|---|---|
| `TURNSTILE_SOLVER` | no | `browser` only (system Chrome / Playwright Chromium) |
| `TURNSTILE_HEADLESS` | no | default `1` = Chrome `--headless=new`; `0` = headed window |
| `TURNSTILE_BROWSER_CHANNEL` | no | auto-selects `chrome` when headless + Chrome installed |
| `TURNSTILE_INTERACTIVE` | no | `1` = manual Turnstile click (forces headed) |
| `TURNSTILE_BROWSER_REUSE` | no | `1` = warm per-thread browser (default 1) |
| `TURNSTILE_TIMEOUT` | no | per-solve timeout seconds (default 60) |
| `TEMPMAIL_API_KEY` | no | Tempmail.lol Plus/Ultra (**free tier needs no key**) |
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
# Full pipeline: signup + SSO + Build OAuth → cliproxyapi_auth/
# Signup + protocol OAuth concurrent; Turnstile / browser OAuth fallback serialized
python run.py -n 1
python run.py -n 5 -t 3
python run.py -n 1 -e cloudflare
python run.py -n 1 --no-oauth
python run.py -n 1 --cliproxyapi-auth-dir /path/to/CLIProxyAPI/data/auth
python run.py -n 1 --accounts-output-dir ./accounts_output   # optional ledger
python run.py -n 1 --oauth-debug
# After OAuth, probe Build quota (off by default): keep only usable files
python run.py -n 1 --check-quota
python run.py -n 5 -t 4 --check-quota --failed-auth-dir ./cliproxyapi_auth_failed
```

### Runtime outputs

| Dir | Default | Purpose |
|---|---|---|
| `sso_output/` | **on** | email + password + SSO JWT |
| `cliproxyapi_auth/` | **on** (unless `--no-oauth`) | CLIProxyAPI-ready auth JSON |
| `cliproxyapi_auth_failed/` | only with `--check-quota` | zero-quota auth (override with `--failed-auth-dir`) |
| `oauth_output/` | off | raw OAuth archive (standalone tools / explicit `output_dir`) |
| `accounts_output/` | off | pipeline ledger (`--accounts-output-dir`) |

Helpers:
- `check_accounts.py` — **only** auth usability / Build quota checker
- `xai_oauth_login.py`, `xai_oauth_export_cliproxyapi.py` — standalone OAuth helpers

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
| [**LINUX DO**](https://linux.do/) | Technical discussion, protocol research feedback, long-term notes |
| GitHub Issues | Bug reports and PRs (primary entry) |

---

## Disclaimer

> [!IMPORTANT]
> Using this project means you have read, understood, and accepted **all** of [`NOTICE`](NOTICE).  
> If you cannot accept — do not use; delete all copies.

**Summary (NOTICE controls):** AS IS; authorized scope only; user bears all liability; maintainers have no support or adaptation duty; no affiliation with xAI / Grok / Cloudflare / CLIProxyAPI or other named vendors.

License: [MIT](LICENSE) · Notice: [NOTICE](NOTICE) · Security: [SECURITY.md](SECURITY.md)
