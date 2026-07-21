> **简体中文** | [English](README.en.md)

# grok-build-auth

面向 **x.ai / Grok 公开 Web 认证链路** 的协议研究客户端：用纯 HTTP 复现  
`注册 → SSO → OAuth PKCE（Grok Build / CLI scope）→ 导出本地 auth JSON`  
整条链路，便于协议分析、互操作性研究与本地集成测试。

默认：注册走纯 HTTP（`curl_cffi`）；Turnstile **后台 token 池默认开**；邮箱 **多渠道 registry + 后台邮箱池默认开**（`-e auto` 用上所有已配置渠道：优先高吞吐、限速溢出补量；单渠道自动 solo）。

**流水线拆分（默认）**：`-t` 只管**注册到 SSO**；SSO 落盘后立刻释放注册线程，由独立 **OAuth worker 池**（`--oauth-workers`，默认 `max(-t,2)`）优先跑 **session-reuse 快路径**，失败再回退 Device Flow。目标成功数 `-n` 按 **BUILD 导出** 计数。

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![Use](https://img.shields.io/badge/use-research%20%2F%20authorized%20only-red)](#法律边界)

---

> [!CAUTION]
> **使用本项目即视为同意 [`NOTICE`](NOTICE) 的全部条款。**  
> 项目按 **AS IS** 提供、无任何担保、维护者不负任何责任。  
> **仅限**：你拥有的系统 / 合法 CTF / 授权 bug bounty in-scope 资产 / 安全研究与教学。  
> **严禁**：欺诈、批量造号转售、黑产代注册、未授权目标、故意违反第三方 ToS。  
> 一切法律责任由使用者自负。不接受条款就**不要使用、不要 clone、删除全部副本**。

---

## 法律边界

| | 说明 |
|---|---|
| **允许** | 自有账号与本地环境；明确授权范围内的安全研究；CTF / 课堂 / 学术协议研究；离线阅读源码 |
| **禁止** | 欺诈滥用、批量造号转售、代注册牟利、未授权自动化攻击、规避平台安全机制用于非法目的 |
| **责任** | 账号封禁、额度损失、民事 / 刑事 / 行政责任等全部由**使用者**承担 |
| **关系** | 与 xAI、Grok、Cloudflare、CLIProxyAPI、临时邮箱等服务商**无隶属、无授权、无赞助** |

完整条款见 [`NOTICE`](NOTICE)。License 是 [MIT](LICENSE)，但 **MIT 不是免责的全部**。

不确定是否合法 —— **不要运行**。先问律师，或先联系目标平台安全团队。

---

## 这是什么

研究型协议客户端，不是官方 SDK。主要能力：

| 阶段 | 内容 |
|---|---|
| **注册** | `accounts.x.ai` 邮箱验证码（gRPC-web）+ Turnstile + Next.js Server Action 建号 |
| **SSO** | 从建号响应 / set-cookie 链提取 session JWT，供 OAuth 复用 |
| **OAuth** | 独立 worker 池；优先 `oauth_protocol` SSO **session-reuse**（PKCE + CookieSetter + consent，可重试）；失败再回退 `sso2auth` Device Flow；纯 HTTP |
| **导出** | 写出与 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 兼容的本地 `type=xai` auth 文件（Grok Build 通道） |

值得看的点：

- **协议优先**：注册 / OAuth 默认纯 HTTP（`curl_cffi` 指纹会话）
- **注册 / OAuth 解耦**：`-t` = 注册并发；`--oauth-workers` = Build 并发；SSO 后不堵注册槽位
- **OAuth 快路径优先**：session-reuse 带传输重试（`OAUTH_TRANSPORT_RETRIES`）；默认仍允许 Device 兜底（`OAUTH_ALLOW_DEVICE=1`）
- **Turnstile 池 + 尽早 force-render**：后台 mint；进页后尽早刷 CF（默认跳过邮箱路径按钮，`TURNSTILE_CLICK_EMAIL=0`）
- **邮箱池 + 多渠道**：`mail_channels` registry；**prefer + overflow**
- **随 `-t` 自动调参**：token/mail 池深度与 minter 随注册并发缩放
- **传输毛刺重试**：SSL EOF / timeout 归入可重试错误（无代理池也有 `TRANSPORT_RETRY`）
- **精简落盘**：默认写 `sso_output/` + `cliproxyapi_auth/`

---

## 架构

```mermaid
flowchart TB
    subgraph entry [run.py]
      A["-n 成功目标 / -t 注册并发 / --oauth-workers"]
    end

    subgraph pools [后台池 默认 on]
      TP[TurnstileTokenPool]
      MP[MailboxPool]
      R[ChannelRouter]
      MP --> R
      R -->|prefer| TM[tempmail]
      R -->|overflow| YY[yyds]
      R -->|overflow| CF[cloudflare]
    end

    subgraph reg [注册线程池 -t]
      B[signup client.py]
      C[SSO sso_output]
    end

    subgraph oauthp [OAuth worker 池 默认 on]
      Q[SSO 任务队列]
      D{session-reuse<br/>oauth_protocol}
      F[sso2auth Device Flow]
      E[token 交换]
    end

    subgraph browser [Turnstile]
      S[drission early force-render]
    end

    A --> TP
    A --> MP
    TP -->|mint| S
    A --> B
    TP -->|token| B
    MP -->|inbox| B
    B --> C
    C -->|释放注册线程| Q
    Q --> D
    D -->|OK| E
    D -->|失败且允许| F
    F --> E
    E --> G[cliproxyapi_auth/*.json]
```

`-n` 默认按 **BUILD 导出成功**（`cliproxyapi_auth`）计数；`--no-oauth` 时按 SSO 计数。  
导出 CPA auth 需要 OAuth 的 `access_token` / `refresh_token`（快路径或 Device Flow）。

---

## 现状与门槛

这不是「零配置即用」的产品。至少需要：

- Python 3.9+
- Turnstile：本机浏览器后端（默认 Drission + Chrome 有头；可选 Camoufox / Playwright）
- 邮箱：默认 `-e auto` —— 启用**所有已配置**渠道（tempmail 始终可用；yyds / cloudflare 有凭证才进池）；也可 `-e tempmail|yyds|cloudflare` 强制单渠道
- （可选）HTTP(S) 代理
- （可选）本地已安装的 CLIProxyAPI，用于加载导出的 auth 目录

平台条款、风控、接口变更会导致链路随时失效；维护者**无义务**持续适配。

---

## 上手

### 安装

```bash
git clone https://github.com/<you>/grok-build-auth.git
cd grok-build-auth
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
# 可选：Camoufox 后端还要拉浏览器二进制
# pip install camoufox && camoufox fetch
cp .env.example .env
# 可选编辑 .env；通常只需代理，无需 TEMPMAIL key
```

### 配置

| 变量 | 必须 | 说明 |
|---|---|---|
| `TURNSTILE_SOLVER` | 否 | `auto`（默认）/ `drission` / `camoufox` / `browser` / `safari` — 见 [Turnstile 后端](#turnstile-后端) |
| `TURNSTILE_HEADLESS` | 否 | drission/camoufox 默认 `0`（有头）；playwright 默认 `1`；camoufox 可 `virtual` |
| `TURNSTILE_TIMEOUT` | 否 | 单次 mint 硬超时秒数（**drission 默认 30**；camoufox/browser 默认 60） |
| `TURNSTILE_POOL` | 否 | 后台 token 池（**默认开**；`0` 关闭） |
| `TURNSTILE_POOL_SIZE` | 否 | 池硬上限（**默认随 `-t`**：`= max(2, -t)`，上限 32） |
| `TURNSTILE_POOL_TARGET` | 否 | 空闲预存（默认 `min(2, size)`；够用即停产） |
| `TURNSTILE_POOL_MINTERS` | 否 | mint 线程数（**默认随 `-t`**：`ceil(-t/4)` 上限 4；Safari 固定 1） |
| `TURNSTILE_TOKEN_MAX_AGE` | 否 | 池内 token 最大年龄秒（默认 200） |
| `TURNSTILE_PAUSE_FILE` | 否 | 存在则暂停 mint/点击（默认 `/tmp/grok-turnstile.pause`） |
| `TURNSTILE_PARALLEL` | 否 | **仅池关闭时** mint 并发（默认随 `-t`，上限 8） |
| `TURNSTILE_MINIMIZED` | 否 | drission 有头时默认 `1`：最小化窗口 |
| `TURNSTILE_OFFSCREEN` | 否 | drission 有头时默认 `1`：离屏窗口备份 |
| `TURNSTILE_BROWSER_CHANNEL` | 否 | 仅 playwright；有系统 Chrome 时自动选 `chrome` |
| `TURNSTILE_INTERACTIVE` | 否 | 仅 playwright：`1` = 手动点选（强制有头） |
| `TURNSTILE_BROWSER_REUSE` | 否 | `1` = 热复用浏览器（默认 1；drission 暖页复用） |
| `TEMPMAIL_API_KEY` | 否 | Tempmail.lol Plus/Ultra（**免费层无需 key**） |
| `TEMPMAIL_FREE_CREATE_INTERVAL` | 否 | 无 key 时 create 最小间隔秒（默认 **3 ≈ 20/min**） |
| `YYDS_API_KEY` / `YYDS_JWT` | 否 | YYDS 邮箱（二选一；有则 `-e auto` 自动纳入） |
| `YYDS_API_BASE` | 否 | 默认 `https://maliapi.215.im/v1` |
| `YYDS_DOMAINS` | 否 | 域名白名单（空 = **全部已验证域名**，域名侧负载均衡） |
| `MAIL_BACKENDS` | 否 | 显式渠道列表，覆盖 `-e auto`（如 `tempmail,yyds`） |
| `MAIL_CHANNEL_WEIGHTS` | 否 | 优先级，如 `tempmail:100,yyds:40,cloudflare:60` |
| `MAIL_CHANNEL_CAPACITY` | 否 | 每渠道并发 create 容量，满则溢出 |
| `MAIL_POOL` | 否 | 后台邮箱预创建池（**默认开**；`0` 关） |
| `MAIL_POOL_SIZE` / `_TARGET` / `_MINTERS` | 否 | 同 turnstile 池语义（默认随 `-t`） |
| `MAIL_POOL_MAX_AGE` | 否 | 池内邮箱最大年龄秒（默认 600） |
| `MAIL_CODE_TIMEOUT` | 否 | 等验证码秒数，超时换箱（默认 30） |
| `MAIL_MAX_ATTEMPTS` | 否 | 静默邮箱最多换几次（默认 3） |
| `CLOUDFLARE_API_TOKEN` | `-e cloudflare` / auto 检测 | CF API token |
| `CLOUDFLARE_ACCOUNT_ID` | 同上 | CF 账户 |
| `CLOUDFLARE_D1_DB_ID` | 同上 | D1 库 ID |
| `ALIAS_MAIL_DOMAINS` | 同上 | 你控制的邮箱域名（逗号分隔） |
| `CLIPROXYAPI_AUTH_DIR` | 否 | 默认 `./cliproxyapi_auth` |
| `OAUTH_ASYNC` | 否 | **默认 `1`**：SSO 后交 OAuth 池；`0` / `--no-oauth-async` = 同线程串行 Build |
| `OAUTH_WORKERS` | 否 | OAuth 池线程数（也可用 `--oauth-workers`；默认 `max(-t, 2)`） |
| `OAUTH_TRANSPORT_RETRIES` | 否 | session-reuse 快路径传输重试次数（默认 **3**） |
| `OAUTH_ALLOW_DEVICE` | 否 | 快路径失败后是否 Device Flow（默认 **`1`**；`0` = 只快路径） |
| `TRANSPORT_RETRY` | 否 | **无代理池**时注册侧传输重试（默认 3；含 SSL EOF / timeout） |
| `VISIT_HOME` | 否 | `1` = 注册前 visit console home（**默认 0 跳过**，少 1 RTT） |
| `TURNSTILE_CLICK_EMAIL` | 否 | `1` = mint 前点「邮箱注册」（默认 **0**，尽早 force-render CF） |
| `TURNSTILE_NATIVE_POLL` | 否 | force-render 失败后 native 补刀秒数（默认 4；`0` 关） |
| `TURNSTILE_FORCE_POLL` | 否 | force-render 后轮询上限秒（默认 12） |
| `HTTPS_PROXY` / `HTTP_PROXY` | 否 | 单代理（无池文件时） |
| `PROXY_POOL_FILE` | 否 | 代理池文件，**每行一个 URL**；启动时探测出口地区 |
| `PROXY_POOL` | 否 | 内联小列表（逗号/换行）；大池用 FILE |
| `PROXY_REGION` | 否 | 目标地区码（`us`/`jp`/`hk`…）；探测后**只保留该地区轮换** |
| `PROXY_POOL_SCOPE` | 否 | `same_region`（**默认**）/ `all` |
| `PROXY_GEO_WORKERS` | 否 | 并发探测数（默认 16） |
| `PROXY_GEO_CACHE` | 否 | 探测缓存（默认 `./.proxy_geo_cache.json`） |
| `PROXY_RETRY` | 否 | 有代理池时单号传输失败换代理重试（默认 8；已覆盖 SSL EOF 等） |

**永远不要**把 `.env`、token 目录提交进 Git。详见 [`SECURITY.md`](SECURITY.md)。

### 运行（研究 / 自有账号场景）

```bash
# 零配置批量：-t 4 注册 + OAuth 池(默认 max(-t,2)) + token/mail 池 + -e auto
python run.py -n 20

# 单号冒烟
python run.py -n 1

# 注册并发与 OAuth 池分开调（推荐：OAuth workers ≥ 注册 -t）
python run.py -n 20 -t 4 --oauth-workers 6

# 改注册并发（token/mail 池随 -t；OAuth workers 默认 max(-t,2)）
python run.py -n 20 -t 8

# 强制单渠道（solo：可阻塞 wait/retry，无多渠溢出）
python run.py -n 10 -e yyds
python run.py -n 10 -e tempmail
python run.py -n 1 -e cloudflare

# 显式多渠道列表（覆盖 auto 检测）
MAIL_BACKENDS=tempmail,yyds python run.py -n 20 -t 8

# 调权重 / 容量（tempmail 优先；满 slot 才溢出 yyds）
MAIL_CHANNEL_WEIGHTS=tempmail:100,yyds:40 MAIL_CHANNEL_CAPACITY=tempmail:3,yyds:2 \
  python run.py -n 20 -t 8

# 关邮箱池（注册时现场 create；路由仍可用）
MAIL_POOL=0 python run.py -n 4 -t 2

# 固定 token 池参数（覆盖自动）
TURNSTILE_POOL_SIZE=6 TURNSTILE_POOL_MINTERS=2 python run.py -n 20 -t 4

# 关 token 池（退回每线程现场 mint；PARALLEL 默认= -t）
TURNSTILE_POOL=0 python run.py -n 4 -t 2

# 指定 Turnstile 后端
TURNSTILE_SOLVER=drission python run.py -n 10 -t 4
TURNSTILE_SOLVER=camoufox python run.py -n 1
TURNSTILE_SOLVER=browser  python run.py -n 1

# 代理池文件：每行一个代理 → 探测出口地区 → 只轮换指定地区
PROXY_POOL_FILE=./proxies.txt PROXY_REGION=us python run.py -n 10 -t 4

# 临时暂停 mint / HID 点击（边干活边跑）
touch /tmp/grok-turnstile.pause   # 暂停
rm    /tmp/grok-turnstile.pause   # 恢复

# 仅 SSO（不跑 Build）
python run.py -n 1 --no-oauth

# 关闭 OAuth 异步池：SSO 后同线程串行 Build（旧行为）
python run.py -n 4 -t 2 --no-oauth-async

# 强制全程 Device Flow（跳过 session-reuse；更慢）
python run.py -n 1 --no-oauth-protocol

# 只允许快路径、失败不 Device（便于排查 session-reuse）
OAUTH_ALLOW_DEVICE=0 python run.py -n 5 -t 2 --oauth-workers 4

python run.py -n 1 --cliproxyapi-auth-dir /path/to/CLIProxyAPI/data/auth
python run.py -n 1 --accounts-output-dir ./accounts_output
python run.py -n 1 --oauth-debug

# OAuth 后探测额度（默认关）
python run.py -n 1 --check-quota
python run.py -n 5 -t 4 --check-quota --failed-auth-dir ./cliproxyapi_auth_failed
```

### 运行产物

| 目录 | 默认 | 内容 |
|---|---|---|
| `sso_output/` | **写** | 每号 `sso_*.json`（邮箱/密码/SSO）；另追加纯 SSO 列表 `sso_tokens.txt`（每行一个 JWT） |
| `cliproxyapi_auth/` | **写**（未 `--no-oauth`） | CLIProxyAPI `type=xai` auth |
| `cliproxyapi_auth_failed/` | 仅 `--check-quota` | 探测无额度的 auth（可用 `--failed-auth-dir` 改路径） |
| `oauth_output/` | 可选 | 原始 OAuth 归档（`xai_oauth_login` 或显式 `output_dir`） |
| `accounts_output/` | 可选 | 流水线台账（`--accounts-output-dir`） |

### 辅助脚本

```bash
# 检查 cliproxyapi_auth 可用性 / Build 额度
python check_accounts.py cliproxyapi_auth/

# SSO → CPA（Device Flow）
python retry_oauth_from_sso.py
# SSO2AUTH_WORKERS=4 python retry_oauth_from_sso.py

# 交互式浏览器 OAuth
python xai_oauth_login.py

# oauth_output → CPA auth
python xai_oauth_export_cliproxyapi.py --cliproxyapi-auth-dir ./cliproxyapi_auth
```

### 导出文件形态（本地文件，非官方密钥）

```json
{
  "type": "xai",
  "auth_kind": "oauth",
  "access_token": "...",
  "refresh_token": "...",
  "base_url": "https://cli-chat-proxy.grok.com/v1",
  "headers": {
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-version": "0.2.93",
    "x-grok-client-identifier": "grok-shell"
  }
}
```

将 CLIProxyAPI 的 `auth-dir` 指向该目录后按 CPA 文档热加载即可（仅限合法自用场景）。

---

## Token 池（默认）

注册线程**只消费** token；后台 minter 用浏览器 mint。默认开启，降低「每号冷启动浏览器」和空闲超产风险。

### 为什么要池

| 模式 | 行为 | 适用 |
|---|---|---|
| **池 on（默认）** | 后台预产少量 token；注册 HTTP 路径直接取用 | 批量、边干活边跑 |
| **池 off** | 每个注册线程现场 `solve_turnstile`（`TURNSTILE_PARALLEL` 限流） | 调试单次 mint |

### 按需生产（够用即停）

- **空闲**：只保持 `target` 枚预存（默认 2），到量后日志 `pool mint pause (satisfied …)`
- **有人在等**：库存目标扩到 `min(size, waiting + target)`，盖住当前注册压力
- **慢 mint 完成后若已够用**：丢弃多余枚，避免灌满硬上限

### 随 `-t` 自动调参

未设置对应 env 时，由 `suggest_pool_params(-t)` 推算：

| 参数 | 自动规则 | 例 |
|---|---|---|
| `size` | `clamp(-t, 2..32)` | `-t4→4`，`-t8→8` |
| `target` | `min(2, size)` | 空闲只囤 2 |
| `minters` | `ceil(-t/4)` 上限 4；**Safari 固定 1** | `-t4→1`，`-t8→2`，`-t16→4` |
| 池关闭时 `PARALLEL` | `min(8, -t)` | 跟注册并发对齐 |

显式 `TURNSTILE_POOL_SIZE` / `_TARGET` / `_MINTERS` / `TURNSTILE_PARALLEL` 优先于自动。

### Drission 暖页 mint（推荐）

- 每 worker **只 navigate 一次** signup 页
- 之后同页 `force-render` + CDP 点击，暖 mint 约 **2.3–2.5s/枚**（冷启动首枚约 10–16s）
- 有头默认 **最小化 + 离屏**，尽量不抢系统焦点

### 与 Tempmail free 对齐

- CLI 默认 **`-t 4`**：贴近 free 层稳态吞吐
- 无 `TEMPMAIL_API_KEY` 时，`create` 进程级节流默认 **3s/次（≈20/min）**，减轻 429
- 有 Plus/Ultra key 时跳过 free create 节流；可自行提高 `-t`
- **多渠道时**：free pacing / 429 只让 tempmail「这一拍」不可用，yyds 等立即补量；slot 恢复后仍优先 tempmail（不是全切）

### 启动日志怎么读

```text
grok-build-auth: 20 accounts, 4 threads, email=auto, ... pool=on, mail-pool=on
  mail-channels:        tempmail,yyds (prefer+overflow; weights via MAIL_CHANNEL_WEIGHTS)
  turnstile-pool:       size=4 target=2 minters=1 max_age=200s (auto from -t=4)
  mail-pool:            size=4 target=2 minters=1 max_age=600s (auto from -t=4)
  [ts-pool] pool +1 len=837 q=1/4 want=2 wait=0
  [mail-pool] mail pool +1 [tempmail] xai…@… q=2/4
  [mail] mail channel tempmail rate: … (next_slot≈3.0s; overflow/retry)
  [mail] mail create via yyds: xai…@…
  [3/20] [#2] email [yyds]: xai…@…
  [3/20] [#2] Turnstile 837 chars from pool (age=6s q=1)
```

---

## 邮箱池与多渠道（默认）

注册线程**只消费**已建好的 `Mailbox(email, receiver, channel)`；后台 minter 按渠道路由预创建。默认开启，与 Turnstile 池并行预热。

### 渠道 registry（可扩展）

| 渠道 | 默认 weight | 何时 available | 说明 |
|---|---|---|---|
| **tempmail** | 100 | 始终（free 或 `TEMPMAIL_API_KEY`） | 通常最高 create RPM；优先 |
| **yyds** | 40 | `YYDS_API_KEY` 或 `YYDS_JWT` | 溢出补量；域名可全量 LB |
| **cloudflare** | 60 | `CLOUDFLARE_*` + `ALIAS_MAIL_DOMAINS` | 自建 D1 别名邮箱 |
| **自定义** | 自定 | `configured()` | `register_channel(ChannelSpec(...))`，CLI/`auto` 自动带上 |

```python
# 插件式扩展（无需改 run.py if/else）
from xconsole_client.mail_channels import ChannelSpec, register_channel

register_channel(ChannelSpec(
    name="mymail",
    weight=55,
    capacity=2,
    configured=lambda: bool(os.environ.get("MYMAIL_KEY")),
    create=lambda: (email, receiver),  # receiver.wait_for_code(timeout=…)
))
```

### Solo vs Multi

| 模式 | 触发 | 行为 |
|---|---|---|
| **Solo** | 只解析出 1 个渠道（`-e yyds` / 仅配一种 / `MAIL_BACKENDS=yyds`） | 可阻塞 wait/retry，兼容旧单后端 |
| **Multi** | `-e auto` 且 ≥2 个 available | **prefer + overflow**：高 weight 有 slot 就用；满/限速只挡这一拍，立刻用其它 ready 渠道补 |

限速（RPM / free pacing / 429）≠ 渠道死亡：恢复 slot 后继续优先高 weight。

### 池参数（随 `-t`）

| 参数 | 自动规则 | env |
|---|---|---|
| size | `clamp(-t, 2..32)` | `MAIL_POOL_SIZE` |
| target | `min(2, size)` | `MAIL_POOL_TARGET` |
| minters | `ceil(-t/4)` 上限 4 | `MAIL_POOL_MINTERS` |
| max_age | 600s | `MAIL_POOL_MAX_AGE` |
| 开关 | 默认 on | `MAIL_POOL=0` 关闭 |

---

## Turnstile 后端

注册主链路是纯 HTTP；**只有 Cloudflare Turnstile token 必须靠本机浏览器 mint**。  
用环境变量 `TURNSTILE_SOLVER` 选后端（也可用 `resolve_turnstile_solver(backend=...)`）。

### 怎么选

| `TURNSTILE_SOLVER` | 栈 | 默认有头？ | 何时用 |
|---|---|---|---|
| **`auto`（默认）** | 有 DrissionPage → **drission**；否则 → **browser** | 随所选后端 | 日常默认，不用改 |
| **`drission`** | DrissionPage + 本机 **Chrome** + `turnstilePatch/` | **是**（`0`） | **推荐主力**；暖页池 + 终端批量 |
| **`camoufox`** | **Camoufox** 反检测 Firefox（经 Playwright 启动） | **是**（`0`） | 想换 Firefox / 反检测；需额外 `camoufox fetch` |
| **`browser`** | Playwright Chromium/Chrome | **否**（`1`） | 没装 Drission 时的回退；本机 IP 上往往不如前两者稳 |
| **`safari`** | 系统 Safari（macOS） | 会抢焦点 | 手动/单路；池 minters 固定 1 |

别名：

- drission：`dp` / `clean` / `drissionpage`
- camoufox：`camou` / `camoufox-firefox`
- browser：`local` / `playwright` / `chromium` / `chrome` / `free`
- safari：`webkit-system` / `system-safari`

### 依赖

```bash
# 三种后端共用
pip install -r requirements.txt

# drission（默认路径）额外需要：本机已装 Google Chrome
# turnstilePatch/ 扩展已随仓库提供，无需手装

# camoufox 额外：
pip install camoufox
camoufox fetch          # 下载 Camoufox 浏览器二进制（约数百 MB，一次即可）
```

### 常用命令

```bash
# 默认 = auto → drission + 池 on + -t 4
python run.py -n 20

# 显式 Drission 批量
TURNSTILE_SOLVER=drission python run.py -n 10 -t 4

# Camoufox（有头更稳；无显示器可试 virtual）
TURNSTILE_SOLVER=camoufox python run.py -n 1
TURNSTILE_SOLVER=camoufox TURNSTILE_HEADLESS=virtual python run.py -n 1

# Playwright 回退（默认 headless）
TURNSTILE_SOLVER=browser python run.py -n 1
TURNSTILE_SOLVER=browser TURNSTILE_HEADLESS=0 python run.py -n 1
```

### 相关环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `TURNSTILE_SOLVER` | `auto` | 后端选择，见上表 |
| `TURNSTILE_HEADLESS` | drission/camoufox=`0`；browser=`1` | `0` 有头；`1` headless；camoufox 还可 `virtual` |
| `TURNSTILE_TIMEOUT` | drission=`30`；其它=`60` | 单次 mint 硬超时（秒） |
| `TURNSTILE_POOL` | **开** | `0` 关池 |
| `TURNSTILE_POOL_SIZE` | 随 `-t` | 池硬上限 |
| `TURNSTILE_POOL_TARGET` | `min(2,size)` | 空闲预存；够用停产 |
| `TURNSTILE_POOL_MINTERS` | 随 `-t` | 后台 mint 线程；Safari=1 |
| `TURNSTILE_PARALLEL` | 随 `-t`（仅池关） | 现场 mint 并发上限 8 |
| `TURNSTILE_MINIMIZED` | `1`（有头） | 最小化窗口 |
| `TURNSTILE_OFFSCREEN` | `1`（有头） | 离屏备份 |
| `TURNSTILE_BROWSER_REUSE` | `1` | 热复用 / 暖页 |
| `TURNSTILE_DEBUG` | 关 | `1` 打印 solver 详细日志 |
| `TURNSTILE_BROWSER_CHANNEL` | 自动 | 仅 browser：优先系统 Chrome |
| `TURNSTILE_INTERACTIVE` | 关 | 仅 browser：手动点选 |
| `HTTPS_PROXY` / `HTTP_PROXY` | 空 | 单代理 |
| `PROXY_POOL_FILE` | 空 | 每行一个代理；启动探测出口国 |
| `PROXY_REGION` | 自动/指定 | 指定后只轮换该国；未指定则取探测结果中最多的国家 |
| `PROXY_POOL_SCOPE` | `same_region` | `same_region` / `all` |
| `PROXY_GEO_WORKERS` | `16` | 并发探测 |
| `PROXY_GEO_CACHE` | `./.proxy_geo_cache.json` | 地区缓存（可提交忽略） |

说明：

1. 注册 / 验码 / SSO 走协议 HTTP；Turnstile 只在建号前 mint。OAuth 默认在独立 worker 池跑。  
2. **默认 token 池**：后台 mint、注册线程 `from pool`；池关才走 `TURNSTILE_PARALLEL`。  
3. drission 暖页约 2.4s/枚；默认 **尽早 force-render**（`TURNSTILE_CLICK_EMAIL=0`）。  
4. OAuth：**session-reuse 快路径优先**（可重试）→ Device Flow 兜底；`-t` 与 `--oauth-workers` 分开调。  
5. headless 更容易被 CF 拦；批量优先 **有头 + 最小化/离屏 + 暖复用 + 池**。

### 日志里怎么认后端 / 池

```text
# 池模式（默认）
[ts-pool] pool +1 len=837 q=2/4 want=2 wait=0
[#3] Turnstile 837 chars from pool (age=4s q=1)

# 池关闭时的 solver 名
[#1] Turnstile 730 chars via DrissionTurnstileSolver
[#1] Turnstile 730 chars via CamoufoxTurnstileSolver
[#1] Turnstile … via LocalBrowserTurnstileSolver
```

---

## 协议概要

**注册**

1. Warm-up + 动态抓取 Next.js action  
2. 从邮箱池取得 `Mailbox`（带 `channel`）→ 发码 → `receiver.wait_for_code`（渠道绑定）  
3. Turnstile（本机浏览器后端 / token 池，见 [Turnstile 后端](#turnstile-后端)）  
4. `create_account` + 提取 SSO → `sso_output/sso_*.json` + 追加 `sso_tokens.txt`  

**Build OAuth**（`run.py`：SSO 后默认进独立 OAuth 池）

1. 注册线程拿到 SSO 后 `SSO ready -> OAuth queue`，**立即释放**（默认 `OAUTH_ASYNC=1`）  
2. **快路径** `oauth_protocol`：SSO cookies → CookieSetter + consent → code → token（`OAUTH_TRANSPORT_RETRIES`）  
3. **回退** `sso2auth` Device Flow（`OAUTH_ALLOW_DEVICE=1` 时）→ 写 CPA JSON  
4. 默认写出 `cliproxyapi_auth/`；`-n` 按 BUILD 成功计数  
5. `--no-oauth-async`：同线程串行 Build；`--no-oauth-protocol`：只走 Device Flow  

接口与额度策略以平台实时行为为准，文档数值仅供研究参考。

---

## 目录结构

```text
.
├── NOTICE                         # 具有约束力的使用须知（必读）
├── LICENSE                        # MIT
├── README.md / README.en.md
├── SECURITY.md
├── run.py                         # 主入口
├── check_accounts.py              # auth 可用性 / Build 额度
├── retry_oauth_from_sso.py        # SSO → CPA Device Flow
├── xai_oauth_login.py             # 交互式浏览器 OAuth
├── xai_oauth_export_cliproxyapi.py
├── requirements.txt
├── .env.example
├── xconsole_client/               # 协议库（Python 包名，历史命名）
│   ├── client.py                  # 注册
│   ├── oauth_protocol.py          # 协议 OAuth（SSO session-reuse）
│   ├── sso2auth.py                # SSO Device Flow → CPA
│   ├── xai_oauth.py               # PKCE / 导出 / 浏览器登录
│   ├── mail_channels.py           # 邮箱渠道 registry + prefer/overflow 路由
│   ├── mailbox_pool.py            # 后台邮箱预创建池（默认 on）
│   ├── tempmail_transport.py      # Tempmail.lol（free create 节流）
│   ├── yyds_transport.py          # YYDS / maliapi 邮箱
│   ├── turnstile_pool.py          # 后台 token 池（默认 on，随 -t 自动）
│   ├── solver.py                  # Turnstile 工厂
│   ├── drission_solver.py         # Drission + turnstilePatch（暖页复用）
│   ├── camoufox_solver.py         # Camoufox
│   └── sso.py / mailbox.py / ...
├── turnstilePatch/                # Chrome 扩展（Drission 用）
└── alias_mail/                    # 可选：Cloudflare 邮箱助手

# 运行时（gitignore）
# sso_output/               默认写（sso_*.json + sso_tokens.txt）
# cliproxyapi_auth/         默认写（OAuth 成功）
# cliproxyapi_auth_failed/  仅 --check-quota 时
# oauth_output/             可选
# accounts_output/          可选
```

运行产物：默认 **`sso_output/` + `cliproxyapi_auth/`**。`--check-quota` 开启时无额度文件进 `cliproxyapi_auth_failed/`。`oauth_output/` 仅独立 OAuth 工具/显式指定时写；`accounts_output/` 仅 `--accounts-output-dir <path>` 时写。

---

## 已知限制

- 依赖第三方公开接口，部署变更可能导致链路失效
- Turnstile 仍是瓶颈之一：冷启动首枚约 10–16s；**暖页约 2.4s/枚**；默认池按需生产，避免空闲狂 mint
- headless / 脏 IP 更容易空 token；优先 `drission` 或 `camoufox` 有头
- Tempmail **free** 有 RPM 上限：默认 `-t 4` + create 3s 节流；多渠道时 yyds/CF **补量**（非全切）；冲更高吞吐可 Plus key 或提高 tempmail capacity
- 邮箱 / 代理 SSL 抖动会影响成功率；默认 30s 无码即换箱（换箱仍走路由/池）
- 并发过高可能触发平台风控；研究用途请保持克制
- 导出 CPA auth 需要完成 OAuth（协议或 Device Flow）
- 注册 Turnstile 使用本机浏览器后端；OAuth 使用协议 session-reuse 与 Device Flow

---

## 贡献

欢迎在**合法研究与授权场景**下贡献：

1. 协议变更后的适配（附抓包对比 / 复现步骤）
2. 文档与翻译完善
3. 测试与健壮性（超时、重试、错误分类）
4. 脱敏后的研究笔记（禁止提交真实 token / 邮箱 / cookie）

**不接受**意图用于未授权滥用、批量黑产、绕过平台安全策略的 PR / Issue。

安全问题请走私密渠道，见 [`SECURITY.md`](SECURITY.md)。

---

## 社区

| 渠道 | 用途 |
|---|---|
| GitHub Issues | bug 报告与 PR（主入口） |

---

## 致谢

- [curl_cffi](https://github.com/lexiforest/curl_cffi) — TLS / HTTP2 指纹会话  
- [DrissionPage](https://github.com/g1879/DrissionPage) / [Camoufox](https://github.com/daijro/camoufox) — 可选 Turnstile 浏览器后端  
- 相关公开 Web 标准：OAuth 2.0、PKCE、gRPC-web  

---

## 免责声明

> [!IMPORTANT]
> **使用本项目即视为你已完整阅读、完全理解、并明确接受 [`NOTICE`](NOTICE) 的全部条款。**  
> 不能接受 —— 不要使用本项目，删除所有副本。

**摘要（完整文本以 NOTICE 为准）：**

1. **AS IS**：无适销性、特定用途、持续兼容等任何担保。  
2. **仅限授权范围**：自有系统 / 合法 CTF / 授权研究；禁止欺诈、批量转售、未授权目标。  
3. **责任自负**：含账号封禁、民事 / 刑事 / 行政责任、第三方索赔等。  
4. **维护者无义务**回复 issue、修 bug、做协议适配或提供支持。  
5. **无隶属关系**：不代表 xAI、Grok、Cloudflare、CLIProxyAPI 或任何提及的第三方。  

License：[MIT](LICENSE) · 使用须知：[NOTICE](NOTICE) · 安全：[SECURITY.md](SECURITY.md)
