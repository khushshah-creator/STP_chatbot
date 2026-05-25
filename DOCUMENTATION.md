# STP Monitor Chatbot — System Documentation

> **Purpose:** This document explains the complete architecture, data flow, and components of the STP (Sewage Treatment Plant) Monitor Chatbot so any new team member can quickly understand, run, and extend it.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Repository Structure](#3-repository-structure)
4. [Backend — `main.py`](#4-backend--mainpy)
5. [Prompts — `prompts.py`](#5-prompts--promptspy)
6. [Frontend Components](#6-frontend-components)
7. [End-to-End Request Flow](#7-end-to-end-request-flow)
8. [Database Schema Reference](#8-database-schema-reference)
9. [Intent Categories](#9-intent-categories)
10. [Environment & Configuration](#10-environment--configuration)
11. [Running the Project Locally](#11-running-the-project-locally)
12. [Key Design Decisions & Rules](#12-key-design-decisions--rules)

---

## 1. Overview

The STP Monitor Chatbot lets plant operators **ask natural-language questions** about their sewage treatment plant data and get **instant, plain-English answers** — without writing any SQL.

**Example interactions:**
- *"Show energy consumed by pump 2 today"* → queries PostgreSQL → returns "Pump 2 consumed 12.34 kWh today."
- *"How many times did pump 1 start last week?"* → uses `LAG()` window function → returns start count.
- *"What is the current flow rate?"* → returns latest reading instantly.

**Technology stack:**

| Layer | Technology |
|---|---|
| AI/LLM | Google Gemini API (`google-genai` SDK) |
| Backend API | FastAPI (Python) + Uvicorn |
| Database | PostgreSQL (via `psycopg2`) |
| Frontend | Vanilla HTML + CSS + JavaScript |
| Config | `.env` file via `python-dotenv` |

---

## 2. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        BROWSER (port 8000)                  │
│                                                             │
│  ┌──────────────┐   ┌─────────────────┐   ┌─────────────┐  │
│  │  Left Panel  │   │   Chat Window   │   │ Right Panel │  │
│  │  Config +    │   │  Messages +     │   │ Intent JSON │  │
│  │  Examples    │   │  Input Box      │   │ SQL + Tokens│  │
│  └──────────────┘   └────────┬────────┘   └─────────────┘  │
└───────────────────────────────┼─────────────────────────────┘
                                │ HTTP POST /query
                                ▼
┌─────────────────────────────────────────────────────────────┐
│               FastAPI Backend (port 8001)  main.py          │
│                                                             │
│  Step 1: POST /parse-intent                                 │
│    └─► Gemini (INTENT_SYSTEM_PROMPT)                        │
│         └─► Returns structured JSON (intent, pumps, etc.)   │
│                                                             │
│  Step 2: POST /generate-sql                                 │
│    └─► Gemini (SQL_SYSTEM_PROMPT [+ FLOW_ANALYSIS_ADDENDUM])│
│         └─► Returns raw PostgreSQL SELECT query             │
│                                                             │
│  Step 2b: SQL Safety Validator                              │
│    └─► Blocks INSERT/UPDATE/DELETE/DROP etc.                │
│                                                             │
│  Step 3: Execute SQL on PostgreSQL                          │
│    └─► run_query_in_db() → rows (max 1500)                  │
│                                                             │
│  Step 4: Gemini (ANSWER_SYSTEM_PROMPT)                      │
│    └─► Converts raw rows → human-readable answer            │
└─────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────┐
│               PostgreSQL Database                           │
│               Table: "ATL_MPS"                              │
│               ~1 row per minute                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Repository Structure

```
STP_chatbot/
├── main.py          ← FastAPI backend (API routes, pipeline logic)
├── prompts.py       ← All LLM system prompts (intent, SQL, answer)
├── .env             ← Secrets (DB creds, Gemini API key) — never commit!
├── venv/            ← Python virtual environment
├── scratch.py       ← Dev scratch / one-off tests
└── frontend/
    ├── index.html   ← App shell / layout
    ├── app.js       ← All UI logic (fetch, render, state)
    └── style.css    ← Design system + all component styles
```

---

## 4. Backend — `main.py`

### 4.1 Startup & Config

```python
load_dotenv(override=True)   # Load .env
DB_CONFIG = { host, port, dbname, user, password }
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
```

`get_gemini_client()` lazily initialises a singleton Gemini client (thread-safe).

---

### 4.2 API Routes

| Method | Route | Description |
|---|---|---|
| `GET` | `/` | Welcome message |
| `GET` | `/health` | Liveness check (polled by frontend badge every 10 s) |
| `POST` | `/parse-intent` | Parses user message → intent JSON |
| `POST` | `/generate-sql` | Converts intent JSON → SQL string |
| `POST` | `/query` | **Full pipeline** (intent → SQL → DB → answer) |

---

### 4.3 `/query` — Full Pipeline (primary route)

```
User message
    │
    ▼
[Hardcoded Example Check]
    │  If message exactly matches one of 8 preset queries
    │  → skip LLM, run SQL directly (faster + free)
    │
    ▼
Step 1: parse_intent()   [Gemini call #1]
    │
    ├─ intent == "greeting"       → friendly greeting (no SQL)
    ├─ intent == "out_of_scope"   → scope warning (no SQL)
    ├─ intent == "unknown"        → "please rephrase" (no SQL)
    └─ clarification_needed=true  → time-range quick-reply buttons (no SQL)
    │
    ▼
Step 2: generate_sql()   [Gemini call #2]
    │  flow_analysis / sec_analysis → appends FLOW_ANALYSIS_SQL_ADDENDUM
    │
    ▼
Step 2b: validate_sql_safety()
    │  Must start with SELECT or WITH
    │  Blocks: INSERT UPDATE DELETE DROP TRUNCATE ALTER CREATE EXEC …
    │
    ▼
Step 3: run_query_in_db()
    │  psycopg2 → PostgreSQL → max 1500 rows returned
    │
    ▼
Step 4: call_gemini(ANSWER_SYSTEM_PROMPT)   [Gemini call #3]
    │  Input: user question + SQL + raw rows
    │  Output: plain-English answer
    │
    ▼
FullResponse JSON → frontend
```

---

### 4.4 Pydantic Models

```python
class QueryRequest:
    user_message: str
    conversation_history: list   # Last N turns for context

class FullResponse:
    intent_json: dict            # Parsed intent fields
    sql_query: str | None        # Generated SQL
    clarification_needed: bool
    clarification_message: str | None
    clarification_options: list | None  # Quick-reply time buttons
    db_results: list | None      # Raw rows from DB
    db_error: str | None
    final_answer: str | None     # Human-readable answer
    token_usage: TokenUsage      # prompt + output + total counts
```

---

### 4.5 `call_gemini()` — LLM Helper

- Injects **current IST + UTC timestamps** automatically into every system prompt so "today" / "yesterday" resolve correctly.
- Retries up to **3 times** on empty responses.
- Runs via `run_in_executor` so the async event loop is never blocked.

---

### 4.6 Hardcoded Example Queries (bypass LLM)

```python
EXAMPLE_QUERIES_SQL = {
    "What is the current flow rate?": "SELECT ...",
    "Show energy consumed by pump 2 today": "SELECT ...",
    "How many times did pump 1 start last week?": "WITH lagged AS ...",
    "Compare SEC of all pumps for May 2026": "WITH flow_diff AS ...",
    "What is the wet well level now?": "SELECT ...",
    "Show runtime of all pumps yesterday": "SELECT ...",
    "Power factor of pump 3 last 7 days": "SELECT ...",
    "How many pumps are running right now?": "SELECT ..."
}
```

Exact string match → SQL runs directly, zero Gemini cost.

---

## 5. Prompts — `prompts.py`

### 5.1 `INTENT_SYSTEM_PROMPT`

**Role:** User text → structured JSON object.

**Output schema fields:**

| Field | Type | Description |
|---|---|---|
| `intent` | string | One of 17 intent categories (see §9) |
| `pumps` | `[int]` | Pump numbers 1–6 mentioned. `[]` = all |
| `metrics` | `[string]` | Metric keys e.g. `p1_kwh`, `flow_m3hr` |
| `time_range` | object | `type`, `start`, `end`, `relative`, `raw` |
| `aggregation` | string | `sum` / `avg` / `max` / `min` / `delta` / `latest` / `trend` |
| `limit` | int / null | `1` for current, `null` for aggregates |
| `requires_lag` | bool | `true` for flow volume / pump start count |
| `group_by` | string / null | `pump` / `hour` / `day` / `none` |
| `clarification_needed` | bool | `true` when time range is missing |
| `clarification_reason` | string / null | Message shown to the user |

---

### 5.2 `SQL_SYSTEM_PROMPT`

**Role:** Intent JSON → valid PostgreSQL `SELECT` query.

**Key rules:**
1. Always `FROM "ATL_MPS"` — never another table.
2. All column names **double-quoted** exactly as documented.
3. `LAG()` only for flow delta and pump start count. Never for energy.
4. `KWH → SUM()`. Power / Voltage / Current → `AVG()` where `ONOFF=1`.
5. `SEC = SUM(KWH) / flow_volume_m3`.
6. `"DateAndTime"` is UTC — always convert with `AT TIME ZONE 'Asia/Kolkata'` for daily grouping.
7. No mutating statements ever.

---

### 5.3 `FLOW_ANALYSIS_SQL_ADDENDUM`

Injected only for `flow_analysis` / `sec_analysis` intents. Adds two critical trigger rules:

- **Trigger 1 — Per-pump flow CTE:** When user says "per pump" / "each pump", divide `FIT101.OUTPUT` by `total_active_pumps` using a CTE.
- **Trigger 2 — Threshold filtering:** When user asks for timestamps where per-pump flow exceeds a threshold, filter in the outer `WHERE`.

> ⚠️ `[PLC]FIT101.OUTPUT` = **combined flow of ALL running pumps**, not one pump.

---

### 5.4 `ANSWER_SYSTEM_PROMPT`

**Role:** Raw DB rows → clean plain-English answer.

- Lead with the key number/fact.
- Use proper units (kWh, m³/hr, mm, A, V, VA).
- Round to 2 decimal places.
- Never mention SQL, columns, or DB internals.
- If empty result → "No data found" + suggest broader range.

---

## 6. Frontend Components

### 6.1 Layout (`index.html`)

Three-column layout:

```
┌──────────────┬────────────────────────┬──────────────────┐
│  Left (280px)│     Chat (flex:1)      │  Right (320px)   │
│              │                        │                  │
│ • API URL    │ • Top bar + badge      │ • Token Usage    │
│ • Mode radio │ • Message list         │   (session cost) │
│ • 8 Examples │ • Input + Send button  │ • Intent Panel   │
│ • Clear Chat │                        │ • SQL Panel      │
└──────────────┴────────────────────────┴──────────────────┘
```

---

### 6.2 JavaScript (`app.js`)

**State:**
```javascript
let history = [];                         // [{role, content}] conversation turns
let sessionTotals = { input: 0, output: 0 };
const MAX_HISTORY = 6;                    // Turns sent to backend per request
```

**Key functions:**

| Function | Purpose |
|---|---|
| `sendMessage()` | POST to API, orchestrate all UI updates |
| `checkHealth()` | Ping `/health` every 10 s → update green/red badge |
| `appendUserBubble(text)` | Right-aligned blue message bubble |
| `appendThinkingBubble()` | Animated dots + rotating "thinking" phrases |
| `updateBotBubble(id, content)` | Replace thinking bubble with bot response |
| `renderIntentPanel(intent)` | Update intent cards + flags + JSON expander |
| `renderSqlPanel(data)` | Show SQL + copy button + row count |
| `updateTokens(p_in, p_out)` | Accumulate token counts + compute cost |
| `handleClarifyOption(q, opt)` | Append chosen time to original query, re-send |
| `renderMarkdown(text)` | Mini renderer: bold, italic, inline code, newlines |

**Pipeline mode toggle (radio):**
- **Full Pipeline** → `/query` → full intent + SQL + DB + answer
- **Intent Only** → `/parse-intent` → shows parsed JSON only, no SQL

---

### 6.3 CSS Design System (`style.css`)

**CSS custom properties:**

```css
/* Backgrounds */
--bg-100: #ffffff    --bg-200: #f8fafc    --bg-300: #f1f5f9
--border: #e2e8f0

/* Accent colours */
--accent-blue: #0ea5e9    --accent-purple: #8b5cf6
--accent-green: #22c55e   --accent-red: #ef4444

/* Fonts */
--font-sans: 'Inter'   --font-mono: 'JetBrains Mono'   --font-display: 'Syne'
```

**Component classes:**

| Class | Description |
|---|---|
| `.app-shell` | Root flex container, full viewport |
| `.user-bubble` | Right-aligned blue gradient message |
| `.bot-bubble` | Left-aligned light-bg message |
| `.clarify-bubble` | Yellow-tinted time-range request |
| `.clarify-option-btn` | Animated pill button (7 unique color themes + `float-in` keyframe) |
| `.thinking-bubble` | Three bouncing dots |
| `.intent-badge` | Blue pill tag showing intent name in bot bubble |
| `.json-expander` | Collapsible raw intent JSON block |
| `.sql-block` | Dark monospace display with blue left border |
| `.pill-on / .pill-off / .pill-warn` | Green/red/yellow status badges |
| `.metric-card` | KPI mini-card (intent / pumps / aggregation) |

---

## 7. End-to-End Request Flow

**Example: "Show energy for pump 3 last 7 days"**

```
1.  User types → sendMessage() fires
2.  appendUserBubble("Show energy for pump 3 last 7 days")
3.  appendThinkingBubble() — animated dots appear

4.  fetch POST /query { user_message, conversation_history }

5.  Backend Step 1 — Gemini (INTENT_SYSTEM_PROMPT)
    → {
        "intent": "energy_analysis",
        "pumps": [3],
        "metrics": ["p3_kwh"],
        "time_range": { "type": "relative", "relative": "last_7_days" },
        "aggregation": "sum",
        "limit": null,
        "clarification_needed": false
      }

6.  Backend Step 2 — Gemini (SQL_SYSTEM_PROMPT)
    → SELECT SUM("[PLC]P_DATA[60]") AS p3_energy_kwh
       FROM "ATL_MPS"
       WHERE "DateAndTime" >= NOW() - INTERVAL '7 days';

7.  validate_sql_safety() → PASS (starts with SELECT, no banned keywords)

8.  psycopg2 executes SQL → [{ "p3_energy_kwh": 87.42 }]

9.  Backend Step 4 — Gemini (ANSWER_SYSTEM_PROMPT)
    → "Pump 3 consumed 87.42 kWh over the last 7 days."

10. Frontend receives FullResponse:
    → updateBotBubble()    — shows answer with "energy_analysis" badge
    → renderIntentPanel()  — shows intent / pumps / aggregation cards
    → renderSqlPanel()     — shows SQL + copy button + "1 rows"
    → updateTokens()       — updates session totals + cost estimate
```

---

## 8. Database Schema Reference

**Table:** `"ATL_MPS"` (always double-quoted)
**Granularity:** ~1 row per minute
**Timestamp:** `"DateAndTime"` — `TIMESTAMPTZ`, stored in **UTC**

### Core Columns

| Column | Description | Unit |
|---|---|---|
| `"DateAndTime"` | Timestamp (UTC) | TIMESTAMPTZ |
| `"[PLC]FIT101.OUTPUT"` | **Total combined flow** of ALL running pumps | m³/hr |
| `"[PLC]FIT101_TOTAL.D_MLD"` | Cumulative flow since midnight | MLD |
| `"[PLC]FIT101_MIN.PER_MIN"` | Flow per minute | ML/min |
| `"[PLC]HLT101.OUTPUT"` | Wet well level | mm |
| `"[PLC]P1.ONOFF"` – `"[PLC]P6.ONOFF"` | Pump ON/OFF | 1=ON, 0=OFF |

### Per-Pump Column Map (P1–P6)

| Metric | P1 | P2 | P3 | P4 | P5 | P6 |
|---|---|---|---|---|---|---|
| **Energy (kWh)** | `[18]` | `[39]` | `[60]` | `[81]` | `[102]` | `[123]` |
| **Voltage (V)** | `[12]` | `[33]` | `[54]` | `[75]` | `[96]` | `[117]` |
| **Active Power (W)** | `[0]` | `[21]` | `[42]` | `[63]` | `[84]` | `[105]` |
| **Current (A)** | `[14]` | `[35]` | `[56]` | `[77]` | `[98]` | `[119]` |
| **Apparent Power (VA)** | `[8]` | `[29]` | `[50]` | `[71]` | `[92]` | `[113]` |

> Prefix all: `"[PLC]P_DATA[N]"` — always double-quoted in SQL.

### Key Derived Formulas

```sql
-- Specific Energy Consumption
SEC = SUM("[PLC]P_DATA[kwh_col]") / SUM(flow_volume_m3)

-- Power Factor (pump must be ON)
PF = AVG("[PLC]P_DATA[active_power_col]")
   / NULLIF(AVG("[PLC]P_DATA[apparent_power_col]"), 0)
WHERE "[PLC]PN.ONOFF" = 1

-- Flow volume from cumulative MLD using LAG
flow_delta_m3 = CASE
  WHEN (current_MLD - LAG(current_MLD) OVER (ORDER BY "DateAndTime")) > 0
  THEN (current_MLD - LAG(current_MLD) OVER (ORDER BY "DateAndTime")) * 1000000
  ELSE NULL
END

-- Per-pump flow (FIT101.OUTPUT is TOTAL)
flow_per_pump = "[PLC]FIT101.OUTPUT" / total_active_pumps
-- WHERE total_active_pumps > 0
```

---

## 9. Intent Categories

| Intent | Triggered when user asks about… |
|---|---|
| `current_status` | "current", "now", "latest" readings |
| `pump_runtime` | how long pumps ran |
| `pump_start_count` | how many times pumps started/cycled |
| `energy_analysis` | kWh consumption |
| `power_factor` | PF per pump |
| `flow_analysis` | flow rate, MLD, m³/hr |
| `voltage_analysis` | voltage |
| `current_analysis` | current (amps) |
| `active_power` | active power (watts) |
| `apparent_power` | apparent power (VA) |
| `sec_analysis` | specific energy consumption kWh/m³ |
| `pump_performance` | efficiency / performance comparison |
| `wet_well_level` | wet well / tank level (HLT101) |
| `multi_pump_concurrency` | how many pumps running simultaneously |
| `greeting` | hi, hello, thanks, good morning, etc. |
| `out_of_scope` | weather, sports, general chat, coding |
| `unknown` | cannot be classified |

---

## 10. Environment & Configuration

**File:** `STP_chatbot/.env`

```env
# Google Gemini
GEMINI_API_KEY=your-google-ai-studio-api-key
GEMINI_MODEL=gemini-2.0-flash

# PostgreSQL
DB_HOST=192.168.0.x
DB_PORT=5432
DB_NAME=stp_db
DB_USER=postgres
DB_PASSWORD=your-db-password
```

> [!CAUTION]
> **Never commit `.env` to Git.** Add it to `.gitignore`.

The frontend API URL is also configurable live in the left panel of the UI (default: `http://192.168.0.19:8001`).

---

## 11. Running the Project Locally

### Prerequisites
- Python 3.10+, PostgreSQL running, Google Gemini API key

### Backend

```bash
cd STP_chatbot
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install fastapi uvicorn psycopg2-binary python-dotenv google-genai

# Edit .env with your credentials, then:
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

- Backend API: `http://localhost:8001`
- Interactive API docs: `http://localhost:8001/docs`

### Frontend

```bash
cd STP_chatbot/frontend
python3 -m http.server --bind 0.0.0.0 8000
```

Open browser: `http://localhost:8000`

---

## 12. Key Design Decisions & Rules

### SQL Safety Layer
`validate_sql_safety()` strips single-quoted string literals (to avoid false positives like `WHERE status = 'DELETE'`) then checks:
1. Query starts with `SELECT` or `WITH`.
2. No mutating keyword (`INSERT`, `UPDATE`, `DELETE`, `DROP`, `TRUNCATE`, `ALTER`, `CREATE`, `EXEC`, etc.) appears as a bare token.

### Conversation History
Last 6 turns (`MAX_HISTORY = 6`) are sent with every request so the LLM handles follow-ups like "What about pump 2?" after a previous pump 1 query.

### Timezone Handling
- DB stores **UTC**. Users operate in **IST (UTC+5:30)**.
- `call_gemini()` injects current IST and UTC timestamps automatically.
- SQL rule: use `DATE("DateAndTime" AT TIME ZONE 'Asia/Kolkata')` for day-level grouping.

### Token Cost Tracking
Gemini Flash pricing used by the frontend:
- Input: **$0.50 / 1M tokens**
- Output: **$3.00 / 1M tokens**

Per-request and session totals displayed in real time in the right panel.

### Flow Column Warning
`"[PLC]FIT101.OUTPUT"` = combined flow of **all running pumps**. The `FLOW_ANALYSIS_SQL_ADDENDUM` forces use of the `active_pumps` CTE to derive per-pump flow correctly.

### Clarification Flow
When `clarification_needed=true` (missing time range), 7 quick-reply pill buttons appear (`today`, `yesterday`, `last 7 days`, `last 30 days`, `this month`, `last hour`, `last 24 hours`). Clicking one appends that phrase to the original question and auto-re-sends.

---

*Documentation — STP Monitor Chatbot v1.0 | May 2026*
