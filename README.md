# 🔍 CTI Pastebin Monitorr

An asynchronous Pastebin leak monitor built for **Cyber Threat Intelligence (CTI)**. The script continuously scrapes [pastebin.com/archive](https://pastebin.com/archive) looking for pastes that contain keywords you define — credentials, domains, sensitive data, infrastructure strings, etc. — and automatically saves matches to disk.

---

## 📋 Table of Contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Running in background with screen](#running-in-background)
- [File structure](#file-structure)
- [Saved results](#saved-results)
- [Available parameters](#available-parameters)
- [WAF / Rate Limit protections](#waf-rate-limit)
- [Performance tuning](#performance-tuning)

---

## <a id="how-it-works"></a>⚙️ How it works

Each cycle runs a 4-step pipeline:

```
┌─────────────────────────────────────────────────────────────────┐
│                          SCAN CYCLE                             │
│                                                                 │
│  [1] GET /archive  ──►  [2] Title Filter                        │
│     (every X secs)          (fast match, no download needed)    │
│                                        │                        │
│                                        ▼                        │
│                         [3] GET /raw/{ID}  (with jitter)        │
│                                        │                        │
│                                        ▼                        │
│                         [4] Deep Content Filter                 │
│                                        │                        │
│                                        ▼                        │
│                       💾 Save to results / if match found      │
└─────────────────────────────────────────────────────────────────┘
```

| Step | What it does |
|------|-------------|
| **1 — Metadata collection** | Fetches `/archive` and extracts ID + Title from all recent pastes via regex |
| **2 — Title filter** | Matches the title against keywords (zero download cost) |
| **3 — Content collection** | Downloads raw content of each new paste with random jitter between requests |
| **4 — Deep filter** | Matches the full content against all keywords |

Already-processed IDs are kept in memory (`set`, O(1) lookup) and persisted to `checked.txt` so state survives restarts.

---

## <a id="requirements"></a>🖥️ Requirements

- Python **3.10+**
- pip
- Linux server (recommended for use with `screen`)

---

## <a id="installation"></a>📦 Installation

```bash
# Clone the repository
git clone https://github.com/JaackSkellington/Pastebin_Monitor.git
cd cti-pastebin-monitor

# (Recommended) Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install aiohttp lxml
```

> `asyncio`, `re`, `logging`, `argparse` and `signal` are part of Python's standard library — no installation needed.

---

## <a id="configuration"></a>⚙️ Configuration

### 1. Define your keywords

Edit `keywords.txt` — one keyword per line, case-insensitive. Lines starting with `#` are comments and are ignored.

```
# keywords.txt

# Generic credentials
password
api_key
secret_key
access_token

# Infrastructure
-----BEGIN RSA PRIVATE KEY-----
aws_access_key
AKIA
mongodb://
postgresql://

# Personal data
ssn
credit card

# Your organization (examples)
# mycompany.com
# @mycompany
```

> **Tip:** The more specific the keyword, the fewer false positives. Prefer `@mycompany.com` over just `company`.

---

## <a id="usage"></a> 🚀 Usage

### Basic run

```bash
python pastebin_monitor.py
```

### With custom parameters

```bash
python pastebin_monitor.py \
  --keywords my_terms.txt \
  --seen     state.txt \
  --interval 90
```

### Help

```bash
python pastebin_monitor.py --help
```

---

## <a id="running-in-background"></a>🖥️ Running in background with screen
To keep the monitor running continuously on a Linux server even after closing your terminal, use `screen`:

```bash
# 1. Create a named session
screen -S cti_pastebin

# 2. Start the monitor inside the session
python pastebin_monitor.py

# 3. Detach from the session WITHOUT killing the process
#    Press:  Ctrl+A  →  then  D
#    (do NOT press Ctrl+C — that kills the script)

# 4. You can now close your terminal. The script keeps running.
```

#### Managing the session later

```bash
# List all active sessions
screen -ls

# Re-attach to the session
screen -r cti_pastebin

# Stop the script gracefully (from inside the session)
# Press Ctrl+C — graceful shutdown is triggered
```

#### Quick screen keyboard reference

| Action | Keys |
|--------|------|
| Detach (keep running) | `Ctrl+A` → `D` |
| Stop the script | `Ctrl+C` (inside the session) |
| Scroll log up | `Ctrl+A` → `[` → arrow keys |
| Exit scroll mode | `Q` |

---

## <a id="file-structure"></a>📁 File structure

```
cti-pastebin-monitor/
│
├── pastebin_monitor.py   # Main script
├── keywords.txt          # Your monitoring keywords
│
├── checked.txt           # Auto-generated — already processed IDs
│                         # (delete this file to reprocess everything from scratch)
│
└── results/              # Auto-generated — pastes with keyword matches
    ├── facebook_com_password_Ab3Xy9Kz.txt
    ├── aws_key_AKIA_M7nPqR2w.txt
    └── ...
```

---

## <a id="saved-results"></a>📄 Saved results

Each file in `results/` follows this naming pattern:

```
{keyword1}_{keyword2}_{keywordN}_{PASTE_ID}.txt
```

**Example:** a paste with ID `Ab3Xy9Kz` containing `facebook.com` and `password`:
```
results/facebook_com_password_Ab3Xy9Kz.txt
```

Each file contains a documented header followed by the raw paste content:

```
# ─────────────────────────────────────────────
# CTI Pastebin Monitor — Match Detected
# ─────────────────────────────────────────────
# URL        : https://pastebin.com/Ab3Xy9Kz
# ID         : Ab3Xy9Kz
# Title      : Facebook credentials 2024
# Keywords   : facebook.com, password
# Matched in : both
# Captured   : 2026-05-08T19:32:11Z
# ─────────────────────────────────────────────

[raw paste content here]
```

The **Matched in** field has three possible values:

| Value | Meaning |
|-------|---------|
| `title` | Keyword found only in the title |
| `content` | Keyword found only in the content |
| `both` | Keyword found in both title and content |

> A paste matching N different keywords generates **only 1 file** — all matched keywords are recorded in the header and filename.

---

## <a id="available-parameters"></a>🔧 Available parameters

| Parameter | Short | Default | Description |
|-----------|-------|---------|-------------|
| `--keywords` | `-k` | `keywords.txt` | File containing the search keywords |
| `--seen` | `-s` | `checked.txt` | File for persisting already-processed IDs |
| `--interval` | `-i` | `120` | Seconds between `/archive` scans |

---

## <a id="waf-rate-limit"></a>🛡️ WAF / Rate Limit protections

Pastebin uses Cloudflare with aggressive rate limiting. The script implements the following protections:

| Protection | Implementation |
|------------|---------------|
| **Jitter** | Random 4–12s wait between raw content downloads |
| **Rotating User-Agent** | 4 different user-agents (Chrome, Safari, Firefox, Edge) |
| **Backoff on 403/429** | 5-minute pause when a block is received |
| **Concurrency semaphore** | Maximum 3 simultaneous downloads |
| **Per-request timeout** | 20 seconds — prevents hangs |
| **Graceful shutdown** | `SIGINT`/`SIGTERM` stop the loop without corrupting state |

### Tuning the values

If you are receiving frequent 429 blocks, edit the constants at the top of `pastebin_monitor.py`:

```python
# Jitter between raw content requests (seconds)
JITTER_MIN: float = 4.0   # ← increase if getting 429 frequently
JITTER_MAX: float = 12.0

# Pause on 403 / 429
BACKOFF_403_429: int = 300  # 5 minutes (increase if needed)
```

---

## <a id="performance-tuning"></a>📊 Performance tuning

| Scenario | Recommendation |
|----------|---------------|
| Clean server IP | Default settings work well |
| Frequently blocked IP | Increase `JITTER_MIN/MAX` to 8–20s, set `Semaphore` to 1 |
| More aggressive monitoring | Reduce `--interval` to 60s (use with caution) |
| Large keyword list (50+) | No impact — filter is O(n) over content |
| Reprocess everything from scratch | Delete `checked.txt` before restarting |

---
