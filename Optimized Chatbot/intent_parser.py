"""
Code-based intent parser for STP Chatbot.
Extracts intent, pumps, metrics, time_range, aggregation using keyword/regex
matching — NO LLM calls. Returns None only when confidence is too low.
"""

import re
from datetime import datetime, timezone, timedelta

# ── Greeting patterns ──────────────────────────────────────────────────────
_GREETING_PATTERNS = re.compile(
    r"^\s*("
    r"hi\b|hello\b|hey\b|howdy\b|hola\b|yo\b"
    r"|good\s*(morning|afternoon|evening|night|day)"
    r"|how\s+are\s+you|what'?s\s+up|sup\b"
    r"|thanks?\b|thank\s+you|thx\b"
    r"|bye\b|goodbye\b|see\s+you|later\b"
    r"|namaste\b|namaskar\b"
    r")\s*[?!.]*\s*$",
    re.IGNORECASE,
)

# ── STP domain keywords — if NONE of these appear, it's out_of_scope ──────
_STP_KEYWORDS = re.compile(
    r"pump|flow|energy|kwh|kw|power|voltage|current|amp|runtime"
    r"|run\s*time|wet\s*well|level|stp|sewage|sec\b|specific\s+energy"
    r"|mld|m3|cubic|onoff|on.off|start|cycle|running"
    r"|p[1-6]|fit101|hlt101|atl_mps"
    r"|power\s*factor|pf\b|apparent|active\s*power"
    r"|watt|va\b|volt",
    re.IGNORECASE,
)

# ── Intent keyword maps ───────────────────────────────────────────────────
_INTENT_PATTERNS = {
    "pump_start_count": re.compile(
        r"(how\s+many\s+times|start\s*count|cycle|started|trip)", re.IGNORECASE
    ),
    "pump_runtime": re.compile(
        r"(run\s*time|running\s+time|hours?\s+ran|how\s+long\s+.*run|duration\s+.*run|operated)", re.IGNORECASE
    ),
    "sec_analysis": re.compile(
        r"(sec\b|specific\s+energy\s+consumption|kwh\s*/\s*m[³3]|energy\s+per\s+(cubic|m3|volume))", re.IGNORECASE
    ),
    "power_factor": re.compile(
        r"(power\s*factor|pf\b)", re.IGNORECASE
    ),
    "energy_analysis": re.compile(
        r"(energy|kwh|kilo\s*watt\s*hour|consumption|consumed)", re.IGNORECASE
    ),
    "flow_analysis": re.compile(
        r"(flow|mld|m3\s*/\s*hr|cubic\s+met|fit101|volume\s+pump|liter|litre)", re.IGNORECASE
    ),
    "wet_well_level": re.compile(
        r"(wet\s*well|hlt|tank\s*level|sump\s*level|well\s+level)", re.IGNORECASE
    ),
    "voltage_analysis": re.compile(
        r"(voltage|volt|vll)", re.IGNORECASE
    ),
    "current_analysis": re.compile(
        r"(current|amp(?:ere|s)?)\b", re.IGNORECASE
    ),
    "active_power": re.compile(
        r"(active\s+power|watt(?:age)?(?:\s|$))", re.IGNORECASE
    ),
    "apparent_power": re.compile(
        r"(apparent\s+power|\bva\b|kva)", re.IGNORECASE
    ),
    "multi_pump_concurrency": re.compile(
        r"(how\s+many\s+pumps?\s+(?:are\s+)?runn|simultaneous|concurren|pumps?\s+running\s+(?:now|right\s+now|currently))",
        re.IGNORECASE,
    ),
    "pump_performance": re.compile(
        r"(pump\s+performance|efficiency|compare\s+pump|pump\s+comparison)", re.IGNORECASE
    ),
}

# ── "Current / now / latest" detector ─────────────────────────────────────
_CURRENT_RE = re.compile(
    r"\b(current|now|right\s+now|at\s+the\s+moment|present|latest|live|real\s*time)\b",
    re.IGNORECASE,
)

# ── Pump extractor ────────────────────────────────────────────────────────
_PUMP_RE = re.compile(
    r"(?:pump\s*|p)(\d)(?!\d)", re.IGNORECASE
)
_ALL_PUMPS_RE = re.compile(
    r"\b(all\s+pumps?|every\s+pump|each\s+pump|all\s+6|pumps?\s+1\s*(?:to|-)\s*6)\b",
    re.IGNORECASE,
)

# ── Time range extractors ────────────────────────────────────────────────
_RELATIVE_TIME_MAP = {
    r"\btoday\b": "today",
    r"\byesterday\b": "yesterday",
    r"\blast\s+(?:7|seven)\s+days?\b": "last_7_days",
    r"\blast\s+week\b": "last_7_days",
    r"\bpast\s+(?:7|seven)\s+days?\b": "last_7_days",
    r"\blast\s+(?:30|thirty)\s+days?\b": "last_30_days",
    r"\blast\s+month\b": "last_month",
    r"\bthis\s+month\b": "this_month",
    r"\blast\s+(?:1\s+)?hour\b": "last_hour",
    r"\blast\s+(?:24|twenty\s*four)\s+hours?\b": "last_24_hours",
    r"\blast\s+(?:2|two)\s+hours?\b": "last_2_hours",
    r"\blast\s+(?:12|twelve)\s+hours?\b": "last_12_hours",
    r"\bthis\s+week\b": "this_week",
}

# Specific month-year: "May 2026", "for may", "in april 2026"
_MONTH_YEAR_RE = re.compile(
    r"\b(?:for|in|during|of)?\s*(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?"
    r"|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"(?:\s+(\d{4}))?\b",
    re.IGNORECASE,
)

_MONTH_NUM = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# "last N days/hours/minutes" or "past N days/hours/minutes"
_LAST_N_RE = re.compile(
    r"\b(?:last|past)\s+(\d+)\s+(day|hour|hr|minute|min)s?\b", re.IGNORECASE
)

# "N hrs/hours/days/minutes ago" or "an hour ago", "a day ago"
_AGO_RE = re.compile(
    r"\b(?:(\d+)|an?|one)\s+(hr|hour|day|minute|min)s?\s+ago\b", re.IGNORECASE
)

# "before N hrs/hours/days" or "in last N hrs" or "within N hours"
_BEFORE_N_RE = re.compile(
    r"\b(?:before|within|in\s+(?:the\s+)?last|in\s+(?:the\s+)?past)\s+(\d+)\s+(hr|hour|day|minute|min)s?\b",
    re.IGNORECASE,
)

# Specific date: "2026-05-01", "01/05/2026", "1st May 2026"
_DATE_RE = re.compile(
    r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"
)

# ── Metric mapping per intent ────────────────────────────────────────────

def _pump_metrics(pumps: list[int], suffix: str) -> list[str]:
    """Generate metric keys like p1_kwh, p2_kwh for given pumps."""
    if not pumps:
        return [f"p{i}_{suffix}" for i in range(1, 7)]
    return [f"p{p}_{suffix}" for p in pumps]


# ── Aggregation keywords ─────────────────────────────────────────────────
_NEEDS_TIME_RANGE_INTENTS = {
    "pump_runtime", "pump_start_count", "energy_analysis",
    "flow_analysis", "sec_analysis", "pump_performance",
    "voltage_analysis", "current_analysis", "active_power", "apparent_power",
    "power_factor",
}


def parse_intent_from_code(user_message: str) -> dict | None:
    """
    Attempt to parse intent from user message using keyword/regex matching.

    Returns:
        dict with full intent JSON if confident, or None to signal LLM fallback.
    """
    msg = user_message.strip()
    msg_lower = msg.lower()

    # ── 1. Greeting check (zero-LLM) ──────────────────────────────
    if _GREETING_PATTERNS.match(msg):
        return _build_intent("greeting")

    # ── 2. Out-of-scope check ─────────────────────────────────────
    if not _STP_KEYWORDS.search(msg):
        return _build_intent("out_of_scope")

    # ── 3. Extract pumps ──────────────────────────────────────────
    pumps: list[int] = []
    if _ALL_PUMPS_RE.search(msg):
        pumps = []  # empty = all
    else:
        pump_matches = _PUMP_RE.findall(msg)
        pumps = sorted(set(int(p) for p in pump_matches if 1 <= int(p) <= 6))

    # ── 4. Detect "current / now" ─────────────────────────────────
    is_current = bool(_CURRENT_RE.search(msg))

    # ── 5. Parse time range ───────────────────────────────────────
    time_range = _parse_time_range(msg, msg_lower)

    # ── 6. Detect intent ──────────────────────────────────────────
    intent = _detect_intent(msg, msg_lower, is_current, pumps)

    if intent is None:
        return None  # Can't determine — fall back to LLM

    # ── 7. Handle "current_status" override ───────────────────────
    if is_current and intent in (
        "flow_analysis", "wet_well_level", "multi_pump_concurrency",
        "energy_analysis", "voltage_analysis", "current_analysis",
        "active_power", "apparent_power",
    ):
        if time_range["type"] == "none":
            intent = "current_status"

    # ── 8. Determine metrics ──────────────────────────────────────
    metrics = _determine_metrics(intent, pumps, msg_lower)

    # ── 9. Determine aggregation & limit ──────────────────────────
    aggregation, limit, requires_lag = _determine_aggregation(intent, is_current)

    # ── 10. Check if clarification is needed ──────────────────────
    clarification_needed = False
    clarification_reason = None

    if (
        intent in _NEEDS_TIME_RANGE_INTENTS
        and time_range["type"] == "none"
        and not is_current
    ):
        clarification_needed = True
        clarification_reason = (
            "For what time range do you want to check this? "
            "(e.g., today, last 7 days, specific date)"
        )

    return _build_intent(
        intent=intent,
        pumps=pumps,
        metrics=metrics,
        time_range=time_range,
        aggregation=aggregation,
        limit=limit,
        requires_lag=requires_lag,
        clarification_needed=clarification_needed,
        clarification_reason=clarification_reason,
    )


def _detect_intent(
    msg: str, msg_lower: str, is_current: bool, pumps: list[int]
) -> str | None:
    """Match intent from keyword patterns. Returns None if ambiguous."""

    # Multi-pump concurrency is very specific — check first
    if _INTENT_PATTERNS["multi_pump_concurrency"].search(msg):
        return "multi_pump_concurrency"

    # Check each pattern in priority order
    matched = []
    for intent_name, pattern in _INTENT_PATTERNS.items():
        if intent_name == "multi_pump_concurrency":
            continue
        if pattern.search(msg):
            matched.append(intent_name)

    # "current" as in electric current vs "current" as in "now" disambiguation
    if "current_analysis" in matched and is_current:
        # If user says "current flow" or "current level", it means "now", not amps
        # Only keep current_analysis if they explicitly mention amps/ampere
        if not re.search(r"\b(amp|ampere|amps|current\s+draw|current\s+of\s+pump|current\s+consumption)\b", msg_lower):
            matched.remove("current_analysis")

    if len(matched) == 1:
        return matched[0]
    elif len(matched) > 1:
        # Priority: sec > energy > flow > power_factor > others
        priority = [
            "pump_start_count", "sec_analysis", "power_factor",
            "pump_runtime", "energy_analysis", "flow_analysis",
            "wet_well_level", "voltage_analysis", "current_analysis",
            "active_power", "apparent_power", "pump_performance",
        ]
        for p in priority:
            if p in matched:
                return p

    if len(matched) == 0:
        # Check if it's just about pump status
        if re.search(r"\b(status|which\s+pump|pump\s+status|is\s+pump\s+\d\s+on|running)\b", msg_lower):
            return "current_status"
        return None  # Can't determine — LLM fallback

    return matched[0]


def _parse_time_range(msg: str, msg_lower: str) -> dict:
    """Extract time range from the user message."""
    result = {"type": "none", "start": None, "end": None, "relative": None, "point": None, "raw": None}

    # Check relative time patterns
    for pattern_str, relative_key in _RELATIVE_TIME_MAP.items():
        match = re.search(pattern_str, msg_lower)
        if match:
            result["type"] = "relative"
            result["relative"] = relative_key
            result["raw"] = match.group(0).strip()
            return result

    # Check "last N days/hours"
    last_n = _LAST_N_RE.search(msg_lower)
    if last_n:
        n = int(last_n.group(1))
        unit = last_n.group(2).lower()
        if unit in ("min", "minute"):
            result["type"] = "relative"
            result["relative"] = f"last_{n}_minutes"
        elif unit == "hour":
            result["type"] = "relative"
            result["relative"] = f"last_{n}_hours"
        else:
            result["type"] = "relative"
            result["relative"] = f"last_{n}_days"
        result["raw"] = last_n.group(0).strip()
        return result

    # Check "N hrs/hours/days ago" or "an hour ago" → point-in-time lookup
    ago_match = _AGO_RE.search(msg_lower)
    if ago_match:
        raw_n, unit = ago_match.group(1), ago_match.group(2).lower()
        # "an" / "a" / "one" -> 1
        n = int(raw_n) if raw_n and raw_n.isdigit() else 1
        now = datetime.now(timezone.utc)
        if unit in ("hr", "hour"):
            point_dt = now - timedelta(hours=n)
        elif unit in ("min", "minute"):
            point_dt = now - timedelta(minutes=n)
        else:  # day
            point_dt = now - timedelta(days=n)
        result["type"] = "point_in_time"
        result["point"] = point_dt.strftime("%Y-%m-%dT%H:%M:%S")
        result["raw"] = ago_match.group(0).strip()
        return result

    # Check "before N hrs" → point-in-time (reading AT that point, not averaged over range)
    before_match = _BEFORE_N_RE.search(msg_lower)
    if before_match:
        n = int(before_match.group(1))
        unit = before_match.group(2).lower()
        now = datetime.now(timezone.utc)
        if unit in ("hr", "hour"):
            point_dt = now - timedelta(hours=n)
        elif unit in ("min", "minute"):
            point_dt = now - timedelta(minutes=n)
        else:  # day
            point_dt = now - timedelta(days=n)
        result["type"] = "point_in_time"
        result["point"] = point_dt.strftime("%Y-%m-%dT%H:%M:%S")
        result["raw"] = before_match.group(0).strip()
        return result

    # Check month-year: "May 2026", "for april"
    month_match = _MONTH_YEAR_RE.search(msg)
    if month_match:
        month_name = month_match.group(1).lower()
        year_str = month_match.group(2)
        month_num = _MONTH_NUM.get(month_name)
        if month_num:
            year = int(year_str) if year_str else datetime.now().year
            start = f"{year}-{month_num:02d}-01T00:00:00"
            # End of month
            if month_num == 12:
                end = f"{year + 1}-01-01T00:00:00"
            else:
                end = f"{year}-{month_num + 1:02d}-01T00:00:00"
            result["type"] = "absolute"
            result["start"] = start
            result["end"] = end
            result["raw"] = month_match.group(0).strip()
            return result

    # Check absolute date: "2026-05-01"
    date_match = _DATE_RE.search(msg)
    if date_match:
        y, m, d = date_match.groups()
        result["type"] = "absolute"
        result["start"] = f"{y}-{int(m):02d}-{int(d):02d}T00:00:00"
        result["end"] = f"{y}-{int(m):02d}-{int(d):02d}T23:59:59"
        result["raw"] = date_match.group(0)
        return result

    return result


def _determine_metrics(intent: str, pumps: list[int], msg_lower: str) -> list[str]:
    """Determine metric keys based on intent and pumps."""
    metric_map = {
        "energy_analysis": "kwh",
        "voltage_analysis": "voltage",
        "current_analysis": "current",
        "active_power": "power",
        "apparent_power": "apparent",
        "pump_runtime": "onoff",
        "pump_start_count": "onoff",
        "pump_performance": "kwh",
    }

    if intent == "flow_analysis":
        if "mld" in msg_lower or "million" in msg_lower:
            return ["flow_mld"]
        if "per min" in msg_lower:
            return ["flow_per_min"]
        return ["flow_m3hr"]

    if intent == "wet_well_level":
        return ["wet_well"]

    if intent == "current_status":
        # Determine what they want current of
        if re.search(r"flow", msg_lower):
            return ["flow_m3hr"]
        if re.search(r"(wet\s*well|level|hlt)", msg_lower):
            return ["wet_well"]
        if re.search(r"(pump|running|on)", msg_lower):
            return _pump_metrics(pumps, "onoff")
        return ["flow_m3hr"]  # default

    if intent == "multi_pump_concurrency":
        return _pump_metrics([], "onoff")

    if intent == "sec_analysis":
        return _pump_metrics(pumps, "kwh") + ["flow_m3hr"]

    if intent == "power_factor":
        return _pump_metrics(pumps, "power") + _pump_metrics(pumps, "apparent")

    suffix = metric_map.get(intent)
    if suffix:
        return _pump_metrics(pumps, suffix)

    return []


def _determine_aggregation(intent: str, is_current: bool) -> tuple[str, int | None, bool]:
    """Returns (aggregation, limit, requires_lag)."""
    if is_current or intent == "current_status":
        return "latest", 1, False

    agg_map = {
        "pump_runtime": ("sum", None, False),
        "pump_start_count": ("sum", None, True),
        "energy_analysis": ("sum", None, False),
        "flow_analysis": ("avg", None, False),
        "wet_well_level": ("avg", None, False),
        "voltage_analysis": ("avg", None, False),
        "current_analysis": ("avg", None, False),
        "active_power": ("avg", None, False),
        "apparent_power": ("avg", None, False),
        "sec_analysis": ("sum", None, True),
        "power_factor": ("avg", None, False),
        "multi_pump_concurrency": ("latest", 1, False),
        "pump_performance": ("avg", None, False),
    }
    return agg_map.get(intent, ("avg", None, False))


def _build_intent(
    intent: str = "unknown",
    pumps: list[int] | None = None,
    metrics: list[str] | None = None,
    time_range: dict | None = None,
    aggregation: str = "latest",
    limit: int | None = None,
    requires_lag: bool = False,
    clarification_needed: bool = False,
    clarification_reason: str | None = None,
) -> dict:
    return {
        "intent": intent,
        "pumps": pumps or [],
        "metrics": metrics or [],
        "time_range": time_range or {"type": "none", "start": None, "end": None, "relative": None, "raw": None},
        "aggregation": aggregation,
        "limit": limit,
        "requires_lag": requires_lag,
        "group_by": None,
        "clarification_needed": clarification_needed,
        "clarification_reason": clarification_reason,
    }
