from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Any
import json
import os
import re
import asyncio
import hashlib
import threading
import redis.asyncio as aioredis
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone, timedelta

# ── Gemini API (for all LLM calls) ──
import google.genai as genai
from google.genai import types

load_dotenv(override=True)

from prompts import INTENT_SYSTEM_PROMPT, SQL_SYSTEM_PROMPT, ANSWER_SYSTEM_PROMPT, FLOW_ANALYSIS_SQL_ADDENDUM

# ---------- DB connection config ----------
DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "dbname":   os.getenv("DB_NAME", "stp_db"),
    "user":     os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "sslmode":  "prefer",
}

# ---------- Gemini API config ----------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Model for complex SQL (levels 3-4): more capable, higher cost
GEMINI_COMPLEX_SQL_MODEL = os.getenv("GEMINI_COMPLEX_SQL_MODEL", "gemini-3.5-flash")

# Model for simple SQL (levels 1-2): faster and cheaper
GEMINI_SIMPLE_SQL_MODEL = os.getenv("GEMINI_SIMPLE_SQL_MODEL", "gemini-3-flash-preview")

# Model for intent parsing & final answer (Gemma 4 via Gemini SDK — free)
GEMINI_ANSWER_MODEL = "gemma-4-31b-it"

# Pricing (USD per 1M tokens)
GEMINI_COMPLEX_INPUT_PRICE_PER_M  = 1.5
GEMINI_COMPLEX_OUTPUT_PRICE_PER_M = 9

GEMINI_SIMPLE_INPUT_PRICE_PER_M  = 0.5
GEMINI_SIMPLE_OUTPUT_PRICE_PER_M = 3

# Legacy alias kept for any logging that still references it
GEMINI_FLASH_INPUT_PRICE_PER_M  = GEMINI_COMPLEX_INPUT_PRICE_PER_M
GEMINI_FLASH_OUTPUT_PRICE_PER_M = GEMINI_COMPLEX_OUTPUT_PRICE_PER_M


_gemini_client: genai.Client | None = None


def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


# ---------- Local Redis cache config ----------
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6380))

# TTL for cached SQL queries (seconds). SQL structure won't change for same
# question, but DB is always queried fresh, so real-time data is never stale.
SQL_CACHE_TTL = int(os.getenv("SQL_CACHE_TTL", 300))  # default: 5 minutes

_redis_client: aioredis.Redis | None = None


def get_redis_client() -> aioredis.Redis | None:
    """Return a shared async local Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            decode_responses=True
        )
    return _redis_client


def _make_cache_key(user_message: str) -> str:
    """Stable cache key from normalized user message."""
    normalized = user_message.strip().lower()
    digest = hashlib.md5(normalized.encode()).hexdigest()
    return f"stp_chatbot:sql:{digest}"


# Matches ISO date literals like '2026-05-27' or '2026-05-27T00:00:00' or '2026-05-27 17:00:00'
# that appear inside SQL single-quoted strings — these are hardcoded and unsafe to cache.
_ISO_DATE_RE = re.compile(
    r"'\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?'"
)


def _sql_has_iso_literal(sql: str) -> bool:
    return bool(_ISO_DATE_RE.search(sql))


async def cache_get(user_message: str) -> dict | None:
    """Return cached {intent_json, sql_query} or None on miss/error."""
    client = get_redis_client()
    if client is None:
        print("[Redis] Client is None")
        return None
    try:
        key = _make_cache_key(user_message)
        raw = await client.get(key)
        if raw:
            print(f"[Redis] Cache HIT for: {user_message[:60]}")
            return json.loads(raw)
    except Exception as exc:
        print(f"[Redis] cache_get error (falling back to LLM): {exc}")
    return None


async def cache_set(user_message: str, intent_json: dict, sql_query: str) -> None:
    """Store {intent_json, sql_query} in Redis with TTL. Fails silently.

    Skips caching entirely if the SQL contains hardcoded ISO date literals —
    those queries are time-frozen and would return stale data on a cache hit.
    """
    if _sql_has_iso_literal(sql_query):
        print(
            f"[Redis] Cache SKIP (ISO date literal detected — not safe to cache): "
            f"{user_message[:60]}"
        )
        return
    client = get_redis_client()
    if client is None:
        print("[Redis] Client is None")
        return
    try:
        key = _make_cache_key(user_message)
        payload = json.dumps({"intent_json": intent_json, "sql_query": sql_query})
        await client.set(key, payload, ex=SQL_CACHE_TTL)
        print(f"[Redis] Cache SET (TTL={SQL_CACHE_TTL}s) for: {user_message[:60]}")
    except Exception as exc:
        print(f"[Redis] cache_set error (continuing without cache): {exc}")





# ---------- DB helpers ----------

def get_connection():
    return psycopg2.connect(**DB_CONFIG)


def run_query_in_db(sql: str, max_rows: int = 1500) -> tuple[list[dict], str | None]:
    try:
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows = [dict(r) for r in cur.fetchmany(max_rows)]
            return rows, None
        finally:
            conn.close()
    except psycopg2.Error as e:
        return [], str(e)


# ---------- SQL safety validator ----------

_BLOCKED_SQL_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "TRUNCATE",
    "ALTER", "CREATE", "REPLACE", "MERGE", "UPSERT",
    "GRANT", "REVOKE", "EXECUTE", "EXEC", "CALL",
}


def validate_sql_safety(sql: str) -> tuple[bool, str | None]:
    """
    Return (is_safe, reason).

    A query is safe when:
      1. The first meaningful keyword is SELECT or WITH (CTE prefix).
      2. No blocked keyword appears as a standalone SQL token anywhere in the query.

    Both checks are case-insensitive and ignore keywords that appear inside
    string literals (single-quoted values).
    """
    # Strip single-quoted string literals so we don't false-positive on values
    # like WHERE status = 'DELETE' or WHERE note = 'drop this'.
    sql_no_strings = re.sub(r"'[^']*'", "''", sql)

    # Tokenise by splitting on non-word characters (spaces, parens, commas…)
    tokens = [t.upper() for t in re.split(r"\W+", sql_no_strings) if t]

    if not tokens:
        return False, "The generated query appears to be empty."

    # Rule 1 — query must start with SELECT or WITH
    if tokens[0] not in ("SELECT", "WITH"):
        return False, (
            f"Only SELECT queries are allowed. "
            f"The generated query starts with '{tokens[0]}'."
        )

    # Rule 2 — no mutating keyword anywhere in the token list
    found = _BLOCKED_SQL_KEYWORDS.intersection(tokens)
    if found:
        bad = ", ".join(sorted(found))
        return False, (
            f"The generated query contains a disallowed operation: {bad}. "
            "Only read-only SELECT queries are permitted."
        )

    return True, None


# ---------- Hardcoded Example Queries ----------

EXAMPLE_QUERIES_SQL = {
    "What is the current flow rate?": """SELECT "[PLC]FIT101.OUTPUT" AS current_flow_m3hr FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;""",
    "Show energy consumed by pump 2 today": """SELECT SUM("[PLC]P_DATA[39]") AS p2_energy_kwh FROM "ATL_MPS" WHERE DATE("DateAndTime") = CURRENT_DATE;""",
    "How many times did pump 1 start last week?": """WITH lagged AS ( SELECT "[PLC]P1.ONOFF" AS p1_onoff, LAG("[PLC]P1.ONOFF") OVER (ORDER BY "DateAndTime") AS prev_p1_onoff FROM "ATL_MPS" WHERE "DateAndTime" >= NOW() - INTERVAL '7 days' ) SELECT COUNT(*) AS p1_start_count FROM lagged WHERE p1_onoff = 1 AND prev_p1_onoff = 0;""",
    "Compare SEC of all pumps for May 2026": """WITH flow_diff AS ( SELECT "DateAndTime", "[PLC]P_DATA[18]" AS p1_kwh, "[PLC]P_DATA[39]" AS p2_kwh, "[PLC]P_DATA[60]" AS p3_kwh, "[PLC]P_DATA[81]" AS p4_kwh, "[PLC]P_DATA[102]" AS p5_kwh, "[PLC]P_DATA[123]" AS p6_kwh, CASE WHEN ("[PLC]FIT101_TOTAL.D_MLD" - LAG("[PLC]FIT101_TOTAL.D_MLD") OVER (ORDER BY "DateAndTime")) > 0 THEN ("[PLC]FIT101_TOTAL.D_MLD" - LAG("[PLC]FIT101_TOTAL.D_MLD") OVER (ORDER BY "DateAndTime")) * 60000 ELSE 0 END AS flow_volume_m3 FROM "ATL_MPS" WHERE "DateAndTime" >= '2026-05-01' AND "DateAndTime" < '2026-06-01' ), stats AS ( SELECT SUM(p1_kwh) AS p1_kwh_total, SUM(p2_kwh) AS p2_kwh_total, SUM(p3_kwh) AS p3_kwh_total, SUM(p4_kwh) AS p4_kwh_total, SUM(p5_kwh) AS p5_kwh_total, SUM(p6_kwh) AS p6_kwh_total, SUM(flow_volume_m3) AS flow_volume_m3_total FROM flow_diff ) SELECT CASE WHEN flow_volume_m3_total > 0 THEN p1_kwh_total / flow_volume_m3_total ELSE 0 END AS p1_sec, CASE WHEN flow_volume_m3_total > 0 THEN p2_kwh_total / flow_volume_m3_total ELSE 0 END AS p2_sec, CASE WHEN flow_volume_m3_total > 0 THEN p3_kwh_total / flow_volume_m3_total ELSE 0 END AS p3_sec, CASE WHEN flow_volume_m3_total > 0 THEN p4_kwh_total / flow_volume_m3_total ELSE 0 END AS p4_sec, CASE WHEN flow_volume_m3_total > 0 THEN p5_kwh_total / flow_volume_m3_total ELSE 0 END AS p5_sec, CASE WHEN flow_volume_m3_total > 0 THEN p6_kwh_total / flow_volume_m3_total ELSE 0 END AS p6_sec FROM stats;""",
    "What is the current wet well level?": """SELECT "[PLC]HLT101.OUTPUT" AS wet_well_level_mm FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;""",
    "Show runtime of all pumps yesterday": """SELECT SUM(CASE WHEN "[PLC]P1.ONOFF" = 1 THEN 1 ELSE 0 END) AS p1_runtime_mins, SUM(CASE WHEN "[PLC]P2.ONOFF" = 1 THEN 1 ELSE 0 END) AS p2_runtime_mins, SUM(CASE WHEN "[PLC]P3.ONOFF" = 1 THEN 1 ELSE 0 END) AS p3_runtime_mins, SUM(CASE WHEN "[PLC]P4.ONOFF" = 1 THEN 1 ELSE 0 END) AS p4_runtime_mins, SUM(CASE WHEN "[PLC]P5.ONOFF" = 1 THEN 1 ELSE 0 END) AS p5_runtime_mins, SUM(CASE WHEN "[PLC]P6.ONOFF" = 1 THEN 1 ELSE 0 END) AS p6_runtime_mins FROM "ATL_MPS" WHERE DATE("DateAndTime") = CURRENT_DATE - 1;""",
    "Power factor of pump 3 last 7 days": """SELECT AVG("[PLC]P_DATA[42]") / NULLIF(AVG("[PLC]P_DATA[50]"), 0) AS p3_power_factor FROM "ATL_MPS" WHERE "[PLC]P3.ONOFF" = 1 AND "DateAndTime" >= NOW() - INTERVAL '7 days';""",
    "How many pumps are running right now?": """SELECT ("[PLC]P1.ONOFF" + "[PLC]P2.ONOFF" + "[PLC]P3.ONOFF" + "[PLC]P4.ONOFF" + "[PLC]P5.ONOFF" + "[PLC]P6.ONOFF") AS running_pumps_count FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;"""
}


# ---------- Conversation History (JSON file) ----------

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "conversation_history.json")
_history_lock = threading.Lock()


def _load_history_file() -> list:
    """Read and return the full conversation history list from disk."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[History] Could not read {HISTORY_FILE}: {exc}")
        return []


def _save_history_file(history: list) -> None:
    """Atomically write the history list to disk."""
    try:
        with _history_lock:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2, default=str)
    except OSError as exc:
        print(f"[History] Could not write {HISTORY_FILE}: {exc}")


# ---------- FastAPI app ----------

app = FastAPI(title="STP Chatbot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Pydantic models ----------

class QueryRequest(BaseModel):
    user_message: str
    conversation_history: Optional[list] = []


class HistoryTurn(BaseModel):
    user: str
    assistant: str
    timestamp: Optional[str] = None
    intent: Optional[str] = None
    session_id: Optional[str] = None  # caller-supplied or auto-derived from IP+UA
    user_agent: Optional[str] = None


class IntentResponse(BaseModel):
    intent: str
    pumps: list[int]
    metrics: list[str]
    time_range: dict
    aggregation: str
    limit: Optional[int]
    requires_lag: bool
    clarification_needed: bool
    clarification_reason: Optional[str]
    sql_level: int = 1  # 1=trivial, 2=simple, 3=moderate, 4=complex
    raw_json: dict


class TokenUsage(BaseModel):
    # Intent tokens (Gemma — free)
    intent_prompt_tokens: int = 0
    intent_output_tokens: int = 0
    # SQL-generation tokens (Gemini Flash — billed)
    sql_prompt_tokens: int = 0
    sql_output_tokens: int = 0
    sql_model_used: str = ""             # Which model was selected for SQL
    sql_level: int = 1                   # SQL complexity level from intent (1-4)
    # Answer tokens (Gemma — free)
    answer_prompt_tokens: int = 0
    answer_output_tokens: int = 0
    # Cost fields (SQL step only)
    gemini_sql_input_cost_usd: float = 0.0
    gemini_sql_output_cost_usd: float = 0.0
    gemini_sql_total_cost_usd: float = 0.0


class FullResponse(BaseModel):
    intent_json: dict
    sql_query: Optional[str]
    clarification_needed: bool
    clarification_message: Optional[str]
    clarification_options: Optional[list] = None  # Quick-reply buttons for clarification
    db_results: Optional[list] = None
    db_error: Optional[str] = None
    final_answer: Optional[str] = None
    follow_up_questions: Optional[list[str]] = None  # Suggested follow-up questions from Gemma
    token_usage: Optional[TokenUsage] = None


# ---------- Date/time context helper ----------

def _build_date_context() -> str:
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist_tz)
    now_utc = datetime.now(timezone.utc)
    return (
        f"\n\n--- CURRENT DATE/TIME CONTEXT ---\n"
        f"User's Timezone: IST (Indian Standard Time)\n"
        f"Current IST Time: {now_ist.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"Database Timezone: UTC\n"
        f"Current UTC Time: {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"IMPORTANT: The user speaks in IST, but the database uses UTC.\n"
        f"When interpreting 'today', 'yesterday', etc., determine the start and end of that day in IST, "
        f"then convert those boundaries to UTC timestamps for the query.\n"
        f"Do not use Postgres CURRENT_DATE if it relies on the server timezone, prefer absolute UTC boundaries if necessary."
        f"\n-----------------------------------\n"
    )


# ---------- Gemini SDK call — Gemini Flash (SQL only, billed) ----------

async def call_gemini_flash(
    system_prompt: str,
    messages: list,
    temperature: float = 0.1,
    label: str = "intent",
    model: str | None = None,
) -> tuple[str, dict]:
    """Call a Gemini Flash model via Gemini SDK. Used for SQL generation only. Tracks cost.

    Args:
        model: Which Gemini model to use. Defaults to GEMINI_COMPLEX_SQL_MODEL if not given.
               Pass GEMINI_SIMPLE_SQL_MODEL for level-1/2 queries.
    """
    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY env var is not set. Check your .env file.",
        )

    selected_model = model or GEMINI_COMPLEX_SQL_MODEL

    # Choose pricing based on selected model
    if selected_model == GEMINI_SIMPLE_SQL_MODEL:
        input_price_per_m  = GEMINI_SIMPLE_INPUT_PRICE_PER_M
        output_price_per_m = GEMINI_SIMPLE_OUTPUT_PRICE_PER_M
    else:
        input_price_per_m  = GEMINI_COMPLEX_INPUT_PRICE_PER_M
        output_price_per_m = GEMINI_COMPLEX_OUTPUT_PRICE_PER_M

    dynamic_system_prompt = system_prompt + _build_date_context()

    contents = [
        types.Content(
            role="model" if msg["role"] == "assistant" else "user",
            parts=[types.Part(text=msg["content"])],
        )
        for msg in messages
    ]

    config = types.GenerateContentConfig(
        system_instruction=dynamic_system_prompt,
        temperature=temperature,
        max_output_tokens=5000,
    )

    def _call() -> tuple[str, dict]:
        client = get_gemini_client()
        for attempt in range(3):
            response = client.models.generate_content(
                model=selected_model, contents=contents, config=config,
            )
            text = response.text
            if text:
                if label == "intent":
                    print("INTENT", text)
                break
        else:
            text = ""

        prompt_tokens = getattr(response.usage_metadata, "prompt_token_count", 0)
        output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0)
        input_cost  = (prompt_tokens / 1_000_000) * input_price_per_m
        output_cost = (output_tokens / 1_000_000) * output_price_per_m
        total_cost  = input_cost + output_cost

        usage = {
            "promptTokenCount":     prompt_tokens,
            "candidatesTokenCount": output_tokens,
            "totalTokenCount":      getattr(response.usage_metadata, "total_token_count", 0),
            "inputCostUSD":         round(input_cost, 8),
            "outputCostUSD":        round(output_cost, 8),
            "totalCostUSD":         round(total_cost, 8),
            "model":                selected_model,
        }
        print(
            f"[Gemini/{selected_model}] [{label}] tokens=({prompt_tokens}in/{output_tokens}out) "
            f"cost=${total_cost:.8f} USD"
        )
        return text, usage

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _call)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini API SDK error: {exc}")


# ---------- Gemini SDK call — Gemma 4 31B (final answer only — free) ----------

async def call_gemma(
    system_prompt: str,
    messages: list,
    temperature: float = 0.3,
) -> tuple[str, dict]:
    """Call gemma-4-31b-it via Gemini SDK for final answer formatting only."""
    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY env var is not set. Check your .env file.",
        )

    dynamic_system_prompt = system_prompt + _build_date_context()

    contents = [
        types.Content(
            role="model" if msg["role"] == "assistant" else "user",
            parts=[types.Part(text=msg["content"])],
        )
        for msg in messages
    ]

    config = types.GenerateContentConfig(
        system_instruction=dynamic_system_prompt,
        temperature=temperature,
        max_output_tokens=5000,
    )

    def _call() -> tuple[str, dict]:
        client = get_gemini_client()
        for attempt in range(3):
            response = client.models.generate_content(
                model=GEMINI_ANSWER_MODEL, contents=contents, config=config,
            )
            text = response.text
            if text:
                break
        else:
            text = ""

        usage = {
            "promptTokenCount":     getattr(response.usage_metadata, "prompt_token_count", 0),
            "candidatesTokenCount": getattr(response.usage_metadata, "candidates_token_count", 0),
            "totalTokenCount":      getattr(response.usage_metadata, "total_token_count", 0),
        }
        print(f"[Gemini/{GEMINI_ANSWER_MODEL}] answer snippet: {text[:120]}")
        return text, usage

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _call)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini GEMMA API SDK error: {exc}")





# ---------- Routes ----------

@app.get("/")
async def root():
    return {"message": "STP Chatbot API is running", "version": "1.0.0"}


@app.post("/parse-intent", response_model=IntentResponse)
async def parse_intent(request: QueryRequest):
    messages = request.conversation_history + [
        {"role": "user", "content": request.user_message}
    ]
    # Intent parsing → Gemma 4 31B (free)
    raw_text, usage = await call_gemma(INTENT_SYSTEM_PROMPT, messages, temperature=0.1)

    if not raw_text:
        raise HTTPException(
            status_code=502,
            detail="Gemini returned an empty response for intent parsing.",
        )

    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = clean.split("```")[1]
        if clean.startswith("json"):
            clean = clean[4:]
    clean = clean.strip().strip("```")

    try:
        intent_data = json.loads(clean)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail=f"Intent parsing failed: {raw_text}")

    intent_data["_usage"] = usage

    return IntentResponse(
        intent=intent_data.get("intent", "unknown"),
        pumps=intent_data.get("pumps", []),
        metrics=intent_data.get("metrics", []),
        time_range=intent_data.get("time_range", {}),
        aggregation=intent_data.get("aggregation", "avg"),
        limit=intent_data.get("limit"),
        requires_lag=intent_data.get("requires_lag", False),
        clarification_needed=intent_data.get("clarification_needed", False),
        clarification_reason=intent_data.get("clarification_reason"),
        sql_level=int(intent_data.get("sql_level", 1)),
        raw_json=intent_data,
    )


@app.post("/generate-sql")
async def generate_sql(
    intent: dict,
    intent_name: str = "",
    user_message: str = "",
    sql_model: str | None = None,
):
    # Inject flow_analysis-specific trigger rules when applicable
    sql_prompt = SQL_SYSTEM_PROMPT
    if intent_name in ("flow_analysis", "sec_analysis") or intent.get("intent") in ("flow_analysis", "sec_analysis"):
        sql_prompt = SQL_SYSTEM_PROMPT + FLOW_ANALYSIS_SQL_ADDENDUM

    # Default to complex model if not specified
    effective_model = sql_model or GEMINI_COMPLEX_SQL_MODEL

    # Include the original user question so trigger rules can match on its text
    user_question_context = (
        f"User's original question: \"{user_message}\"\n\n" if user_message else ""
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"{user_question_context}"
                f"Generate a PostgreSQL query for this intent:\n"
                f"{json.dumps(intent, indent=2)}"
            ),
        }
    ]
    # SQL generation → selected Gemini Flash model (billed, cost tracked)
    sql, usage = await call_gemini_flash(
        sql_prompt, messages, label="sql", model=effective_model
    )

    if not sql:
        raise HTTPException(
            status_code=502,
            detail="Gemini returned an empty response for SQL generation.",
        )

    clean = sql.strip()
    if "```" in clean:
        parts = clean.split("```")
        for part in parts:
            p = part.strip()
            # Strip markdown language identifiers
            if p.lower().startswith("sql"):
                p = p[3:].strip()
            elif p.lower().startswith("postgresql"):
                p = p[10:].strip()
                
            if p.upper().startswith("SELECT") or p.upper().startswith("WITH"):
                clean = p
                break

    return {"sql": clean.strip(), "usage": usage}


@app.post("/query", response_model=FullResponse)
async def full_pipeline(request: QueryRequest):
    """Full pipeline: message → intent JSON → SQL → safety check → DB → NL answer.

    Caching strategy (local Redis):
      - Only the generated SQL (and its intent JSON) is cached, keyed by user message.
      - The DB is ALWAYS queried fresh so real-time pump data is never stale.
      - TTL default is 5 minutes (SQL_CACHE_TTL env var).
    """
    user_msg_stripped = request.user_message.strip()
    intent_usage: dict = {}
    sql_usage: dict = {}
    pipeline_path: str = "llm"  # 'example' | 'cached' | 'llm'

    # ── Check for Example Queries ──────────────────────────────
    if user_msg_stripped in EXAMPLE_QUERIES_SQL:
        pipeline_path = "example"
        # Bypass intent parsing and SQL generation for known examples
        generated_sql = EXAMPLE_QUERIES_SQL[user_msg_stripped]
        intent_raw = {
            "intent": "example_query",
            "pumps": [],
            "metrics": [],
            "time_range": {"type": "none"},
            "aggregation": "none",
            "limit": None,
            "requires_lag": False,
            "clarification_needed": False,
            "clarification_reason": None,
            "sql_level": 0,  # 0 = no LLM used (hardcoded example)
        }

    else:
        # ── Redis Cache Check (skip LLM if SQL already known) ──────
        cached = await cache_get(user_msg_stripped)
        if cached:
            pipeline_path = "cached"
            # Cache hit: reuse SQL structure, DB will still run fresh below
            intent_raw    = cached["intent_json"]
            generated_sql = cached["sql_query"]
        else:
            # ── Step 1: Parse intent ───────────────────────────────────
            intent_resp = await parse_intent(request)
            intent_usage = intent_resp.raw_json.pop("_usage", {})
            intent_raw = intent_resp.raw_json

            def _usage_obj() -> TokenUsage:
                return TokenUsage(
                    intent_prompt_tokens=intent_usage.get("promptTokenCount", 0),
                    intent_output_tokens=intent_usage.get("candidatesTokenCount", 0),
                )

            # ── Greeting ───────────────────────────────────────────────
            if intent_resp.intent == "greeting":
                return FullResponse(
                    intent_json=intent_raw,
                    sql_query=None,
                    clarification_needed=False,
                    clarification_message=(
                        "👋 Hello! I'm the STP Monitor Chatbot. "
                        "I can help you query pump runtimes, energy consumption, flow rates, "
                        "wet well levels, and more. "
                        "Try asking: *What is the current flow rate?* or "
                        "*Show energy used by pump 2 today.*"
                    ),
                    token_usage=_usage_obj(),
                )

            # ── Out-of-scope ───────────────────────────────────────────
            if intent_resp.intent == "out_of_scope":
                return FullResponse(
                    intent_json=intent_raw,
                    sql_query=None,
                    clarification_needed=False,
                    clarification_message=(
                        "⚠️ That question is outside my scope. I'm an STP monitoring chatbot. "
                        "Please ask me about pumps, flow, energy, wet well levels, or other "
                        "STP-related data."
                    ),
                    token_usage=_usage_obj(),
                )

            # ── Unknown intent ─────────────────────────────────────────
            if intent_resp.intent == "unknown":
                return FullResponse(
                    intent_json=intent_raw,
                    sql_query=None,
                    clarification_needed=False,
                    clarification_message="Sorry I am not able to answer! Please rephrase your question.",
                    token_usage=_usage_obj(),
                )

            # ── Clarification needed ───────────────────────────────────
            if intent_resp.clarification_needed:
                clarification_options = [
                    "today",
                    "yesterday",
                    "last 7 days",
                    "last 30 days",
                    "this month",
                    "last hour",
                    "last 24 hours",
                ]
                return FullResponse(
                    intent_json=intent_raw,
                    sql_query=None,
                    clarification_needed=True,
                    clarification_message=intent_resp.clarification_reason
                    or "Please provide more details.",
                    clarification_options=clarification_options,
                    token_usage=_usage_obj(),
                )

            # ── Step 2: Generate SQL ───────────────────────────────────
            sql_level = intent_resp.sql_level
            if sql_level > 3:
                sql_model = GEMINI_COMPLEX_SQL_MODEL
            else:
                sql_model = GEMINI_SIMPLE_SQL_MODEL

            print(f"[SQL Model] sql_level={sql_level} → using model: {sql_model}")

            sql_resp = await generate_sql(
                intent_raw,
                intent_name=intent_resp.intent,
                user_message=request.user_message,
                sql_model=sql_model,
            )
            sql_usage = sql_resp.get("usage", {})
            generated_sql = sql_resp["sql"]

            # ── Store in Redis cache for next time ─────────────────────
            # Only cache after safety validation passes (done below)
            # We cache here before the safety check intentionally so that
            # unsafe queries are also stored (they'll be blocked consistently).
            await cache_set(user_msg_stripped, intent_raw, generated_sql)

        # ── Step 2b: Safety validation ─────────────────────────────
        is_safe, safety_reason = validate_sql_safety(generated_sql)
        if not is_safe:
            return FullResponse(
                intent_json=intent_raw,
                sql_query=generated_sql,          # include for debugging / logging
                clarification_needed=False,
                clarification_message=None,
                db_results=None,
                db_error=None,
                final_answer=(
                    "🚫 I can only run read-only queries against the STP database. "
                    f"The query that was generated appears to contain a disallowed "
                    f"operation and has been blocked for safety.\n\n"
                    f"**Reason:** {safety_reason}\n\n"
                    "Please rephrase your question so it asks for data rather than "
                    "changes to the database."
                ),
                token_usage=TokenUsage(
                    intent_prompt_tokens=intent_usage.get("promptTokenCount", 0),
                    intent_output_tokens=intent_usage.get("candidatesTokenCount", 0),
                    sql_prompt_tokens=sql_usage.get("promptTokenCount", 0),
                    sql_output_tokens=sql_usage.get("candidatesTokenCount", 0),
                    gemini_sql_input_cost_usd=sql_usage.get("inputCostUSD", 0.0),
                    gemini_sql_output_cost_usd=sql_usage.get("outputCostUSD", 0.0),
                    gemini_sql_total_cost_usd=sql_usage.get("totalCostUSD", 0.0),
                ),
            )

    # ── Step 3: Execute SQL against DB ────────────────────────
    loop = asyncio.get_event_loop()
    db_rows, db_error = await loop.run_in_executor(
        None, run_query_in_db, generated_sql
    )

    # ── Step 4: Natural-language answer ───────────────────────
    final_answer: str | None = None
    answer_usage: dict = {}

    if db_error:
        answer_prompt_content = (
            f"User question: {request.user_message}\n\n"
            f"The SQL query that was generated:\n{generated_sql}\n\n"
            f"The database returned an error:\n{db_error}\n\n"
            "Please tell the user what went wrong in plain language and suggest "
            "how to rephrase their question."
        )
    else:
        rows_text = json.dumps(db_rows, indent=2, default=str)
        answer_prompt_content = (
            f"User question: {request.user_message}\n\n"
            f"SQL query executed:\n{generated_sql}\n\n"
            f"Query results ({len(db_rows)} rows):\n{rows_text}\n\n"
            "Using the results above, answer the user's question clearly and "
            "concisely. Include specific numbers, units, and context. "
            "If the result set is empty, say so and suggest why."
        )

    answer_messages = [{"role": "user", "content": answer_prompt_content}]
    follow_up_questions: list[str] = []
    try:
        # Final answer → Gemma 4 31B (free) — returns JSON {answer, follow_ups}
        raw_answer, answer_usage = await call_gemma(
            ANSWER_SYSTEM_PROMPT, answer_messages, temperature=0.3
        )
        # Parse the JSON response from Gemma
        clean_answer = raw_answer.strip()
        if clean_answer.startswith("```"):
            # Strip any accidental markdown fences
            parts = clean_answer.split("```")
            for part in parts:
                p = part.strip()
                if p.startswith("json"):
                    p = p[4:].strip()
                if p.startswith("{"):
                    clean_answer = p
                    break
        try:
            answer_json = json.loads(clean_answer)
            final_answer = answer_json.get("answer", clean_answer)
            follow_up_questions = answer_json.get("follow_ups", [])
            # Ensure it's a list of strings
            if not isinstance(follow_up_questions, list):
                follow_up_questions = []
            follow_up_questions = [str(q) for q in follow_up_questions[:3]]
        except (json.JSONDecodeError, AttributeError):
            # Fallback: treat whole response as plain answer, no follow-ups
            final_answer = raw_answer
            follow_up_questions = []
    except HTTPException as exc:
        final_answer = f"(Could not generate answer: {exc.detail})"

    # ── Accumulate tokens & compute cost ──────────────────────
    # Intent = Gemma (free), SQL = Gemini Flash (billed — model chosen by sql_level), Answer = Gemma (free)
    intent_in_tokens  = intent_usage.get("promptTokenCount", 0)
    intent_out_tokens = intent_usage.get("candidatesTokenCount", 0)

    sql_input_tokens  = sql_usage.get("promptTokenCount", 0)
    sql_output_tokens = sql_usage.get("candidatesTokenCount", 0)
    sql_input_cost    = sql_usage.get("inputCostUSD", 0.0)
    sql_output_cost   = sql_usage.get("outputCostUSD", 0.0)
    sql_total_cost    = sql_usage.get("totalCostUSD", 0.0)

    # Determine sql_model_used and sql_level based on which path ran:
    #   'example'  → hardcoded SQL, no LLM called at all
    #   'cached'   → SQL came from Redis cache, no LLM called this request
    #   'llm'      → LLM was called; use model/level from actual usage
    if pipeline_path == "example":
        sql_model_used = "example"
        sql_level_used = 0  # no SQL complexity — hardcoded
    elif pipeline_path == "cached":
        sql_model_used = "cached"
        sql_level_used = intent_raw.get("sql_level", 0)
    else:  # 'llm'
        sql_model_used = sql_usage.get("model", "")
        sql_level_used = intent_raw.get("sql_level", 1)

    answer_in_tokens  = answer_usage.get("promptTokenCount", 0)
    answer_out_tokens = answer_usage.get("candidatesTokenCount", 0)

    total_flash_cost = sql_total_cost  # Only SQL step is billed

    combined_usage = TokenUsage(
        # Intent (Gemma 4 31B — free)
        intent_prompt_tokens=intent_in_tokens,
        intent_output_tokens=intent_out_tokens,
        # SQL (Gemini Flash — billed, model depends on sql_level)
        sql_prompt_tokens=sql_input_tokens,
        sql_output_tokens=sql_output_tokens,
        sql_model_used=sql_model_used,
        sql_level=sql_level_used,
        # Answer (Gemma 4 31B — free)
        answer_prompt_tokens=answer_in_tokens,
        answer_output_tokens=answer_out_tokens,
        # Cost — SQL step only
        gemini_sql_input_cost_usd=round(sql_input_cost, 8),
        gemini_sql_output_cost_usd=round(sql_output_cost, 8),
        gemini_sql_total_cost_usd=round(total_flash_cost, 8),
    )

    print(
        f"[Cost Summary] sql_level={sql_level_used} model={sql_model_used} "
        f"SQL cost=${total_flash_cost:.8f} USD "
        f"(sql: {sql_input_tokens}in/{sql_output_tokens}out) | "
        f"Gemma 4 31B (free) — intent: {intent_in_tokens}in/{intent_out_tokens}out, "
        f"answer: {answer_in_tokens}in/{answer_out_tokens}out"
    )

    return FullResponse(
        intent_json=intent_raw,
        sql_query=generated_sql,
        clarification_needed=False,
        clarification_message=None,
        db_results=db_rows,
        db_error=db_error,
        final_answer=final_answer,
        follow_up_questions=follow_up_questions or None,
        token_usage=combined_usage,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------- Conversation History endpoints ----------

@app.get("/history")
async def get_history():
    """Return the full persisted conversation history from conversation_history.json."""
    history = await asyncio.get_event_loop().run_in_executor(None, _load_history_file)
    return {"history": history, "count": len(history)}


@app.get("/history/all")
async def get_history_grouped():
    """
    Return all conversation history grouped by session_id.
    Useful for the frontend History panel to show per-user/session conversations.
    """
    all_turns = await asyncio.get_event_loop().run_in_executor(None, _load_history_file)

    # Group turns by session_id (fall back to 'unknown' if not set)
    sessions: dict[str, list] = {}
    for turn in all_turns:
        sid = turn.get("session_id") or "unknown"
        sessions.setdefault(sid, []).append(turn)

    # Build a sorted list of session summaries (most recent first)
    session_list = []
    for sid, turns in sessions.items():
        session_list.append({
            "session_id": sid,
            "turn_count": len(turns),
            "first_seen": turns[0].get("timestamp", ""),
            "last_seen": turns[-1].get("timestamp", ""),
            "source_ip": turns[-1].get("source_ip", "unknown"),
            "user_agent": turns[-1].get("user_agent", ""),
            "turns": turns,
        })

    # Sort sessions: most recently active first
    session_list.sort(key=lambda s: s["last_seen"], reverse=True)

    return {
        "total_turns": len(all_turns),
        "total_sessions": len(session_list),
        "sessions": session_list,
    }


@app.post("/history/save")
async def save_history_turn(turn: HistoryTurn, req: Request):
    """Append a single user/assistant turn to conversation_history.json."""
    ist_tz = timezone(timedelta(hours=5, minutes=30))
    timestamp = turn.timestamp or datetime.now(ist_tz).strftime("%Y-%m-%d %H:%M:%S IST")

    # Derive source IP (works behind proxies too)
    forwarded_for = req.headers.get("x-forwarded-for")
    source_ip = forwarded_for.split(",")[0].strip() if forwarded_for else (req.client.host if req.client else "unknown")

    # User-Agent from request header if not provided in body
    user_agent = turn.user_agent or req.headers.get("user-agent", "unknown")

    # Session ID: use caller-supplied value, or fingerprint from IP + UA
    if turn.session_id:
        session_id = turn.session_id
    else:
        fingerprint = f"{source_ip}|{user_agent}"
        session_id = hashlib.md5(fingerprint.encode()).hexdigest()[:12]

    new_entry = {
        "user": turn.user,
        "assistant": turn.assistant,
        "timestamp": timestamp,
        "intent": turn.intent,
        "session_id": session_id,
        "source_ip": source_ip,
        "user_agent": user_agent,
    }

    def _append():
        history = _load_history_file()
        history.append(new_entry)
        _save_history_file(history)
        return len(history)

    count = await asyncio.get_event_loop().run_in_executor(None, _append)
    print(f"[History] Saved turn #{count} (session={session_id}, ip={source_ip}): '{turn.user[:60]}'")
    return {"status": "saved", "total_turns": count, "session_id": session_id, "entry": new_entry}


@app.get("/history/mine")
async def get_my_history(req: Request):
    """
    Return conversation history for the caller's IP address only.
    Groups turns by session_id so the frontend can render per-session chats.
    """
    # Resolve caller IP (same logic as /history/save)
    forwarded_for = req.headers.get("x-forwarded-for")
    caller_ip = forwarded_for.split(",")[0].strip() if forwarded_for else (req.client.host if req.client else "unknown")

    all_turns = await asyncio.get_event_loop().run_in_executor(None, _load_history_file)

    # Keep only turns from this IP
    my_turns = [t for t in all_turns if t.get("source_ip") == caller_ip]

    # Group by session_id
    sessions: dict[str, list] = {}
    for turn in my_turns:
        sid = turn.get("session_id") or "unknown"
        sessions.setdefault(sid, []).append(turn)

    session_list = []
    for sid, turns in sessions.items():
        session_list.append({
            "session_id": sid,
            "turn_count": len(turns),
            "first_seen": turns[0].get("timestamp", ""),
            "last_seen": turns[-1].get("timestamp", ""),
            "source_ip": caller_ip,
            "user_agent": turns[-1].get("user_agent", ""),
            "turns": turns,
        })

    session_list.sort(key=lambda s: s["last_seen"], reverse=True)

    return {
        "caller_ip": caller_ip,
        "total_turns": len(my_turns),
        "total_sessions": len(session_list),
        "sessions": session_list,
    }


@app.delete("/history")
async def clear_history():
    """Delete all conversation history (overwrites conversation_history.json with [])."""
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: _save_history_file([])
    )
    print("[History] Cleared all conversation history.")
    return {"status": "cleared"}