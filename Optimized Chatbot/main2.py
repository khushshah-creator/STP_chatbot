from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import json
import os
import re
import asyncio
from dotenv import load_dotenv
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone, timedelta

# ── Gemini API (Developer API) ──
import google.genai as genai
from google.genai import types

load_dotenv(override=True)

from prompts import INTENT_SYSTEM_PROMPT, SQL_SYSTEM_PROMPT, ANSWER_SYSTEM_PROMPT
from intent_parser import parse_intent_from_code
from sql_builder import build_sql_from_intent


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
MODEL          = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

_gemini_client: genai.Client | None = None


def get_gemini_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


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
    "What is the wet well level now?": """SELECT "[PLC]HLT101.OUTPUT" AS wet_well_level_mm FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;""",
    "Show runtime of all pumps yesterday": """SELECT SUM(CASE WHEN "[PLC]P1.ONOFF" = 1 THEN 1 ELSE 0 END) AS p1_runtime_mins, SUM(CASE WHEN "[PLC]P2.ONOFF" = 1 THEN 1 ELSE 0 END) AS p2_runtime_mins, SUM(CASE WHEN "[PLC]P3.ONOFF" = 1 THEN 1 ELSE 0 END) AS p3_runtime_mins, SUM(CASE WHEN "[PLC]P4.ONOFF" = 1 THEN 1 ELSE 0 END) AS p4_runtime_mins, SUM(CASE WHEN "[PLC]P5.ONOFF" = 1 THEN 1 ELSE 0 END) AS p5_runtime_mins, SUM(CASE WHEN "[PLC]P6.ONOFF" = 1 THEN 1 ELSE 0 END) AS p6_runtime_mins FROM "ATL_MPS" WHERE DATE("DateAndTime") = CURRENT_DATE - 1;""",
    "Power factor of pump 3 last 7 days": """SELECT AVG("[PLC]P_DATA[42]") / NULLIF(AVG("[PLC]P_DATA[50]"), 0) AS p3_power_factor FROM "ATL_MPS" WHERE "[PLC]P3.ONOFF" = 1 AND "DateAndTime" >= NOW() - INTERVAL '7 days';""",
    "How many pumps are running right now?": """SELECT ("[PLC]P1.ONOFF" + "[PLC]P2.ONOFF" + "[PLC]P3.ONOFF" + "[PLC]P4.ONOFF" + "[PLC]P5.ONOFF" + "[PLC]P6.ONOFF") AS running_pumps_count FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;"""
}


# ---------- FastAPI app ----------

app = FastAPI(title="STP Chatbot API", version="2.0.0")

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
    raw_json: dict


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    candidates_tokens: int = 0
    total_tokens: int = 0


class FullResponse(BaseModel):
    intent_json: dict
    sql_query: Optional[str]
    clarification_needed: bool
    clarification_message: Optional[str]
    clarification_options: Optional[list] = None  # Quick-reply buttons for clarification
    db_results: Optional[list] = None
    db_error: Optional[str] = None
    final_answer: Optional[str] = None
    token_usage: Optional[TokenUsage] = None
    source: Optional[str] = None  # "code" or "llm" — for debugging


# ---------- Core LLM call ----------

async def call_gemini(
    system_prompt: str,
    messages: list,
    temperature: float = 0.1,
) -> tuple[str, dict]:
    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="GEMINI_API_KEY env var is not set. Check your .env file.",
        )

    ist_tz = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist_tz)
    now_utc = datetime.now(timezone.utc)
    date_context = (
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
    
    dynamic_system_prompt = system_prompt + date_context

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
                model=MODEL, contents=contents, config=config,
            )
            text = response.text
            if text:
                break
        else:
            text = "" # Fallback if all retries return empty
            
        usage = {
            "promptTokenCount":     getattr(response.usage_metadata, "prompt_token_count", 0),
            "candidatesTokenCount": getattr(response.usage_metadata, "candidates_token_count", 0),
            "totalTokenCount":      getattr(response.usage_metadata, "total_token_count", 0),
        }
        print("text", text)
        return text, usage

    try:
        return await asyncio.get_event_loop().run_in_executor(None, _call)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini API SDK error: {exc}")


# ---------- LLM-based fallback functions ----------

async def parse_intent_llm(request: QueryRequest) -> tuple[dict, dict]:
    """LLM fallback for intent parsing. Returns (intent_dict, usage)."""
    messages = request.conversation_history + [
        {"role": "user", "content": request.user_message}
    ]
    raw_text, usage = await call_gemini(INTENT_SYSTEM_PROMPT, messages)

    if not raw_text:
        raise HTTPException(status_code=502, detail="Gemini returned an empty response for intent parsing.")

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

    return intent_data, usage


async def generate_sql_llm(intent: dict, intent_name: str = "", user_message: str = "") -> tuple[str, dict]:
    """LLM fallback for SQL generation. Returns (sql, usage)."""
    sql_prompt = SQL_SYSTEM_PROMPT

    user_question_context = f"User's original question: \"{user_message}\"\n\n" if user_message else ""
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
    sql, usage = await call_gemini(sql_prompt, messages)

    if not sql:
        raise HTTPException(status_code=502, detail="Gemini returned an empty response for SQL generation.")

    clean = sql.strip()
    if "```" in clean:
        parts = clean.split("```")
        for part in parts:
            p = part.strip()
            if p.lower().startswith("sql"):
                p = p[3:].strip()
            elif p.lower().startswith("postgresql"):
                p = p[10:].strip()
            if p.upper().startswith("SELECT") or p.upper().startswith("WITH"):
                clean = p
                break

    return clean.strip(), usage


async def generate_answer_llm(user_message: str, sql: str, db_rows: list, db_error: str | None) -> tuple[str, dict]:
    """LLM fallback for answer generation. Returns (answer, usage)."""
    if db_error:
        content = (
            f"User question: {user_message}\n\n"
            f"The SQL query that was generated:\n{sql}\n\n"
            f"The database returned an error:\n{db_error}\n\n"
            "Please tell the user what went wrong in plain language and suggest how to rephrase their question."
        )
    else:
        rows_text = json.dumps(db_rows, indent=2, default=str)
        content = (
            f"User question: {user_message}\n\n"
            f"SQL query executed:\n{sql}\n\n"
            f"Query results ({len(db_rows)} rows):\n{rows_text}\n\n"
            "Using the results above, answer the user's question clearly and "
            "concisely. Include specific numbers, units, and context. "
            "If the result set is empty, say so and suggest why."
        )

    messages = [{"role": "user", "content": content}]
    try:
        answer, usage = await call_gemini(ANSWER_SYSTEM_PROMPT, messages, temperature=0.3)
        return answer, usage
    except HTTPException as exc:
        return f"(Could not generate answer: {exc.detail})", {}


# ---------- Routes ----------

@app.get("/")
async def root():
    return {"message": "STP Chatbot API is running (Optimized v2)", "version": "2.0.0"}


@app.post("/parse-intent", response_model=IntentResponse)
async def parse_intent(request: QueryRequest):
    # Try code-based first
    code_result = parse_intent_from_code(request.user_message)
    if code_result is not None:
        return IntentResponse(
            intent=code_result.get("intent", "unknown"),
            pumps=code_result.get("pumps", []),
            metrics=code_result.get("metrics", []),
            time_range=code_result.get("time_range", {}),
            aggregation=code_result.get("aggregation", "avg"),
            limit=code_result.get("limit"),
            requires_lag=code_result.get("requires_lag", False),
            clarification_needed=code_result.get("clarification_needed", False),
            clarification_reason=code_result.get("clarification_reason"),
            raw_json={**code_result, "_source": "code"},
        )

    # Fallback to LLM
    intent_data, usage = await parse_intent_llm(request)
    intent_data["_usage"] = usage
    intent_data["_source"] = "llm"

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
        raw_json=intent_data,
    )


@app.post("/query", response_model=FullResponse)
async def full_pipeline(request: QueryRequest):
    """
    Full pipeline: message → intent → SQL → DB → answer.
    
    Optimization strategy (minimize LLM calls):
      1. Code-based intent parsing first → LLM fallback only if code returns None
      2. Code-based SQL building first → LLM fallback only if builder returns None
      3. LLM always generates the final answer (answer_formatter removed)
    """
    user_msg_stripped = request.user_message.strip()
    llm_calls_made = 0
    intent_usage = {}
    sql_usage = {}
    answer_usage = {}
    source = "code"

    # ── Check for hardcoded Example Queries ────────────────────
    if user_msg_stripped in EXAMPLE_QUERIES_SQL:
        generated_sql = EXAMPLE_QUERIES_SQL[user_msg_stripped]
        intent_raw = {
            "intent": "example_query", "pumps": [], "metrics": [],
            "time_range": {"type": "none"}, "aggregation": "none",
            "limit": None, "requires_lag": False,
            "clarification_needed": False, "clarification_reason": None,
        }
    else:
        # ── Step 1: Parse intent (CODE FIRST) ─────────────────
        intent_raw = parse_intent_from_code(user_msg_stripped)

        if intent_raw is None:
            # Fallback to LLM
            print(f"[FALLBACK] Intent: code failed, using LLM for: {user_msg_stripped}")
            intent_raw, intent_usage = await parse_intent_llm(request)
            llm_calls_made += 1
            source = "llm"
        else:
            print(f"[CODE] Intent parsed: {intent_raw.get('intent')} for: {user_msg_stripped}")

        intent_name = intent_raw.get("intent", "unknown")

        def _usage_obj() -> TokenUsage:
            return TokenUsage(
                prompt_tokens=intent_usage.get("promptTokenCount", 0),
                candidates_tokens=intent_usage.get("candidatesTokenCount", 0),
                total_tokens=intent_usage.get("totalTokenCount", 0),
            )

        # ── Greeting (zero LLM) ──────────────────────────────
        if intent_name == "greeting":
            return FullResponse(
                intent_json=intent_raw, sql_query=None,
                clarification_needed=False, source=source,
                clarification_message=(
                    "👋 Hello! I'm the STP Monitor Chatbot. "
                    "I can help you query pump runtimes, energy consumption, flow rates, "
                    "wet well levels, and more. "
                    "Try asking: *What is the current flow rate?* or "
                    "*Show energy used by pump 2 today.*"
                ),
                token_usage=_usage_obj(),
            )

        # ── Out-of-scope (zero LLM) ──────────────────────────
        if intent_name == "out_of_scope":
            return FullResponse(
                intent_json=intent_raw, sql_query=None,
                clarification_needed=False, source=source,
                clarification_message=(
                    "⚠️ That question is outside my scope. I'm an STP monitoring chatbot. "
                    "Please ask me about pumps, flow, energy, wet well levels, or other "
                    "STP-related data."
                ),
                token_usage=_usage_obj(),
            )

        # ── Unknown intent ────────────────────────────────────
        if intent_name == "unknown":
            return FullResponse(
                intent_json=intent_raw, sql_query=None,
                clarification_needed=False, source=source,
                clarification_message="Sorry I am not able to answer! Please rephrase your question.",
                token_usage=_usage_obj(),
            )

        # ── Clarification needed ──────────────────────────────
        if intent_raw.get("clarification_needed"):
            return FullResponse(
                intent_json=intent_raw, sql_query=None,
                clarification_needed=True, source=source,
                clarification_message=intent_raw.get("clarification_reason") or "Please provide more details.",
                clarification_options=["today", "yesterday", "last 7 days", "last 30 days", "this month", "last hour", "last 24 hours"],
                token_usage=_usage_obj(),
            )

        # ── Step 2: Generate SQL (CODE FIRST) ─────────────────
        generated_sql = build_sql_from_intent(intent_raw)

        if generated_sql is None:
            # Fallback to LLM
            print(f"[FALLBACK] SQL: code builder failed, using LLM for intent: {intent_name}")
            generated_sql, sql_usage = await generate_sql_llm(
                intent_raw, intent_name=intent_name, user_message=request.user_message,
            )
            llm_calls_made += 1
            source = "llm"
        else:
            print(f"[CODE] SQL generated for intent: {intent_name}")

        # ── Step 2b: Safety validation ────────────────────────
        is_safe, safety_reason = validate_sql_safety(generated_sql)
        if not is_safe:
            return FullResponse(
                intent_json=intent_raw, sql_query=generated_sql,
                clarification_needed=False, clarification_message=None,
                source=source,
                final_answer=(
                    "🚫 I can only run read-only queries against the STP database. "
                    f"The query that was generated appears to contain a disallowed "
                    f"operation and has been blocked for safety.\n\n"
                    f"**Reason:** {safety_reason}\n\n"
                    "Please rephrase your question so it asks for data rather than "
                    "changes to the database."
                ),
                token_usage=TokenUsage(
                    prompt_tokens=intent_usage.get("promptTokenCount", 0) + sql_usage.get("promptTokenCount", 0),
                    candidates_tokens=intent_usage.get("candidatesTokenCount", 0) + sql_usage.get("candidatesTokenCount", 0),
                    total_tokens=intent_usage.get("totalTokenCount", 0) + sql_usage.get("totalTokenCount", 0),
                ),
            )

    # ── Step 3: Execute SQL against DB ────────────────────────
    loop = asyncio.get_event_loop()
    db_rows, db_error = await loop.run_in_executor(None, run_query_in_db, generated_sql)

    # ── Step 4: Generate answer via LLM (always) ──────────────
    print(f"[LLM] Generating final answer")
    final_answer, answer_usage = await generate_answer_llm(
        request.user_message, generated_sql, db_rows, db_error,
    )
    llm_calls_made += 1
    source = "llm"

    # ── Accumulate tokens ─────────────────────────────────────
    combined_usage = TokenUsage(
        prompt_tokens=(
            intent_usage.get("promptTokenCount", 0)
            + sql_usage.get("promptTokenCount", 0)
            + answer_usage.get("promptTokenCount", 0)
        ),
        candidates_tokens=(
            intent_usage.get("candidatesTokenCount", 0)
            + sql_usage.get("candidatesTokenCount", 0)
            + answer_usage.get("candidatesTokenCount", 0)
        ),
        total_tokens=(
            intent_usage.get("totalTokenCount", 0)
            + sql_usage.get("totalTokenCount", 0)
            + answer_usage.get("totalTokenCount", 0)
        ),
    )

    print(f"[STATS] LLM calls: {llm_calls_made}, source: {source}")

    return FullResponse(
        intent_json=intent_raw,
        sql_query=generated_sql,
        clarification_needed=False,
        clarification_message=None,
        db_results=db_rows,
        db_error=db_error,
        final_answer=final_answer,
        token_usage=combined_usage,
        source=source,
    )


@app.get("/health")
async def health():
    return {"status": "ok"}