"""
Code-based SQL builder for STP Chatbot.
Generates PostgreSQL queries from a parsed intent dict — NO LLM calls.
Returns None only when the intent is too complex for template-based generation.
"""

from datetime import datetime, timezone, timedelta


# ── Column mappings ──────────────────────────────────────────────────────

PUMP_KWH_COL = {
    1: '"[PLC]P_DATA[18]"', 2: '"[PLC]P_DATA[39]"', 3: '"[PLC]P_DATA[60]"',
    4: '"[PLC]P_DATA[81]"', 5: '"[PLC]P_DATA[102]"', 6: '"[PLC]P_DATA[123]"',
}

PUMP_VOLTAGE_COL = {
    1: '"[PLC]P_DATA[12]"', 2: '"[PLC]P_DATA[33]"', 3: '"[PLC]P_DATA[54]"',
    4: '"[PLC]P_DATA[75]"', 5: '"[PLC]P_DATA[96]"', 6: '"[PLC]P_DATA[117]"',
}

PUMP_ACTIVE_POWER_COL = {
    1: '"[PLC]P_DATA[0]"', 2: '"[PLC]P_DATA[21]"', 3: '"[PLC]P_DATA[42]"',
    4: '"[PLC]P_DATA[63]"', 5: '"[PLC]P_DATA[84]"', 6: '"[PLC]P_DATA[105]"',
}

PUMP_CURRENT_COL = {
    1: '"[PLC]P_DATA[14]"', 2: '"[PLC]P_DATA[35]"', 3: '"[PLC]P_DATA[56]"',
    4: '"[PLC]P_DATA[77]"', 5: '"[PLC]P_DATA[98]"', 6: '"[PLC]P_DATA[119]"',
}

PUMP_APPARENT_POWER_COL = {
    1: '"[PLC]P_DATA[8]"', 2: '"[PLC]P_DATA[29]"', 3: '"[PLC]P_DATA[50]"',
    4: '"[PLC]P_DATA[71]"', 5: '"[PLC]P_DATA[92]"', 6: '"[PLC]P_DATA[113]"',
}

PUMP_ONOFF_COL = {
    1: '"[PLC]P1.ONOFF"', 2: '"[PLC]P2.ONOFF"', 3: '"[PLC]P3.ONOFF"',
    4: '"[PLC]P4.ONOFF"', 5: '"[PLC]P5.ONOFF"', 6: '"[PLC]P6.ONOFF"',
}

ALL_PUMPS = [1, 2, 3, 4, 5, 6]


def _get_pumps(intent: dict) -> list[int]:
    """Return pump list; empty means all 6."""
    pumps = intent.get("pumps", [])
    return pumps if pumps else ALL_PUMPS


# ── Time range → WHERE clause ───────────────────────────────────────────

def _time_where(intent: dict) -> str:
    """Build WHERE clause from time_range. Returns empty string if none."""
    tr = intent.get("time_range", {})
    tr_type = tr.get("type", "none")

    if tr_type == "none":
        return ""

    if tr_type == "point_in_time":
        # Narrow ±10 minute window around the target point for WHERE
        # (ORDER BY closest-match handled per-builder; this is a pre-filter)
        point = tr.get("point")
        if point:
            return (
                f"""WHERE "DateAndTime" >= '{point}'::timestamptz - INTERVAL '10 minutes'"""
                f""" AND "DateAndTime" <= '{point}'::timestamptz + INTERVAL '10 minutes'"""
            )
        return ""

    if tr_type == "absolute":
        start = tr.get("start")
        end = tr.get("end")
        if start and end:
            return f"""WHERE "DateAndTime" >= '{start}'::timestamptz AND "DateAndTime" < '{end}'::timestamptz"""
        return ""

    if tr_type == "relative":
        rel = tr.get("relative", "")
        mapping = {
            "today": """WHERE DATE("DateAndTime" AT TIME ZONE 'Asia/Kolkata') = (NOW() AT TIME ZONE 'Asia/Kolkata')::date""",
            "yesterday": """WHERE DATE("DateAndTime" AT TIME ZONE 'Asia/Kolkata') = ((NOW() AT TIME ZONE 'Asia/Kolkata')::date - 1)""",
            "last_7_days": """WHERE "DateAndTime" >= NOW() - INTERVAL '7 days'""",
            "last_30_days": """WHERE "DateAndTime" >= NOW() - INTERVAL '30 days'""",
            "last_month": """WHERE "DateAndTime" >= NOW() - INTERVAL '30 days'""",
            "this_month": """WHERE DATE_TRUNC('month', "DateAndTime" AT TIME ZONE 'Asia/Kolkata') = DATE_TRUNC('month', NOW() AT TIME ZONE 'Asia/Kolkata')""",
            "this_week": """WHERE DATE_TRUNC('week', "DateAndTime" AT TIME ZONE 'Asia/Kolkata') = DATE_TRUNC('week', NOW() AT TIME ZONE 'Asia/Kolkata')""",
            "last_hour": """WHERE "DateAndTime" >= NOW() - INTERVAL '1 hour'""",
            "last_24_hours": """WHERE "DateAndTime" >= NOW() - INTERVAL '24 hours'""",
            "last_2_hours": """WHERE "DateAndTime" >= NOW() - INTERVAL '2 hours'""",
            "last_12_hours": """WHERE "DateAndTime" >= NOW() - INTERVAL '12 hours'""",
        }
        if rel in mapping:
            return mapping[rel]

        # Generic "last_N_days/hours/minutes"
        import re
        m = re.match(r"last_(\d+)_(days?|hours?|minutes?)", rel)
        if m:
            n, unit = m.group(1), m.group(2)
            # Normalize unit
            if unit.startswith("day"):
                unit = "days"
            elif unit.startswith("hour"):
                unit = "hours"
            elif unit.startswith("minute"):
                unit = "minutes"
            return f"""WHERE "DateAndTime" >= NOW() - INTERVAL '{n} {unit}'"""

    return ""


def _and_time_where(intent: dict) -> str:
    """Returns ' AND ...' version of time filter for CTEs with existing WHERE."""
    tw = _time_where(intent)
    if not tw:
        return ""
    # Replace leading WHERE with AND
    return tw.replace("WHERE ", "AND ", 1)

def _is_point_in_time(intent: dict) -> bool:
    """Returns True when the user asked for a reading AT a specific moment."""
    return intent.get("time_range", {}).get("type") == "point_in_time"


def _point_order_by(intent: dict) -> str:
    """ORDER BY clause that picks the row closest to the target point."""
    point = intent.get("time_range", {}).get("point", "")
    return f"ORDER BY ABS(EXTRACT(EPOCH FROM (\"DateAndTime\" - '{point}'::timestamptz))) LIMIT 1"



def build_sql_from_intent(intent: dict) -> str | None:
    """
    Build SQL from a parsed intent dict.
    Returns SQL string, or None to signal LLM fallback.
    """
    intent_name = intent.get("intent", "unknown")
    builder = _BUILDERS.get(intent_name)
    if builder is None:
        return None
    try:
        return builder(intent)
    except Exception:
        return None  # Fallback to LLM


def _build_current_status(intent: dict) -> str:
    """Latest reading for flow, wet well, or pump status."""
    metrics = intent.get("metrics", [])

    if "flow_m3hr" in metrics or not metrics:
        return 'SELECT "[PLC]FIT101.OUTPUT" AS current_flow_m3hr FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;'
    if "wet_well" in metrics:
        return 'SELECT "[PLC]HLT101.OUTPUT" AS wet_well_level_mm FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;'

    # Pump on/off status
    pumps = _get_pumps(intent)
    cols = ", ".join(f'{PUMP_ONOFF_COL[p]} AS p{p}_status' for p in pumps)
    return f'SELECT {cols} FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;'


def _build_pump_runtime(intent: dict) -> str:
    pumps = _get_pumps(intent)
    where = _time_where(intent)
    cols = ", ".join(
        f'SUM(CASE WHEN {PUMP_ONOFF_COL[p]} = 1 THEN 1 ELSE 0 END) AS p{p}_runtime_mins'
        for p in pumps
    )
    return f'SELECT {cols} FROM "ATL_MPS" {where};'


def _build_pump_start_count(intent: dict) -> str:
    pumps = _get_pumps(intent)
    where = _time_where(intent)
    # Use LAG to detect 0→1 transitions
    cte_cols = []
    for p in pumps:
        col = PUMP_ONOFF_COL[p]
        cte_cols.append(f'{col} AS p{p}_onoff')
        cte_cols.append(f'LAG({col}) OVER (ORDER BY "DateAndTime") AS prev_p{p}_onoff')

    cte_select = ", ".join(cte_cols)
    count_cols = ", ".join(
        f"SUM(CASE WHEN p{p}_onoff = 1 AND prev_p{p}_onoff = 0 THEN 1 ELSE 0 END) AS p{p}_start_count"
        for p in pumps
    )
    return (
        f'WITH lagged AS (\n'
        f'  SELECT {cte_select}\n'
        f'  FROM "ATL_MPS"\n'
        f'  {where}\n'
        f')\n'
        f'SELECT {count_cols}\n'
        f'FROM lagged;'
    )


def _build_energy_analysis(intent: dict) -> str:
    pumps = _get_pumps(intent)
    where = _time_where(intent)
    cols = ", ".join(
        f'SUM({PUMP_KWH_COL[p]}) AS p{p}_energy_kwh'
        for p in pumps
    )
    return f'SELECT {cols} FROM "ATL_MPS" {where};'


def _build_power_factor(intent: dict) -> str:
    pumps = _get_pumps(intent)
    where = _time_where(intent)
    # PF = AVG(Active Power) / NULLIF(AVG(Apparent Power), 0) where pump is ON
    parts = []
    for p in pumps:
        onoff = PUMP_ONOFF_COL[p]
        active = PUMP_ACTIVE_POWER_COL[p]
        apparent = PUMP_APPARENT_POWER_COL[p]
        parts.append(
            f'AVG(CASE WHEN {onoff} = 1 THEN {active} END) / '
            f'NULLIF(AVG(CASE WHEN {onoff} = 1 THEN {apparent} END), 0) AS p{p}_power_factor'
        )
    cols = ", ".join(parts)
    return f'SELECT {cols} FROM "ATL_MPS" {where};'


def _build_flow_analysis(intent: dict) -> str:
    metrics = intent.get("metrics", ["flow_m3hr"])

    # Point-in-time: get the closest single reading to that moment
    if _is_point_in_time(intent):
        where = _time_where(intent)  # narrow ±10 min window
        order = _point_order_by(intent)
        if "flow_mld" in metrics:
            col = '"[PLC]FIT101_TOTAL.D_MLD" AS flow_mld'
        else:
            col = '"[PLC]FIT101.OUTPUT" AS flow_m3hr'
        return f'SELECT {col}, "DateAndTime" FROM "ATL_MPS" {where} {order};'

    where = _time_where(intent)

    if "flow_mld" in metrics:
        # Delta-based MLD calculation
        return (
            f'WITH flow_lag AS (\n'
            f'  SELECT "[PLC]FIT101_TOTAL.D_MLD" AS mld,\n'
            f'         LAG("[PLC]FIT101_TOTAL.D_MLD") OVER (ORDER BY "DateAndTime") AS prev_mld\n'
            f'  FROM "ATL_MPS"\n'
            f'  {where}\n'
            f')\n'
            f'SELECT SUM(CASE WHEN mld - prev_mld > 0 THEN mld - prev_mld ELSE 0 END) AS total_flow_mld\n'
            f'FROM flow_lag WHERE prev_mld IS NOT NULL;'
        )

    if intent.get("aggregation") == "latest" or intent.get("limit") == 1:
        return 'SELECT "[PLC]FIT101.OUTPUT" AS current_flow_m3hr FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;'

    return f'SELECT AVG("[PLC]FIT101.OUTPUT") AS avg_flow_m3hr FROM "ATL_MPS" {where};'


def _build_wet_well_level(intent: dict) -> str:
    if _is_point_in_time(intent):
        where = _time_where(intent)
        order = _point_order_by(intent)
        return f'SELECT "[PLC]HLT101.OUTPUT" AS wet_well_level_mm, "DateAndTime" FROM "ATL_MPS" {where} {order};'
    if intent.get("aggregation") == "latest" or intent.get("limit") == 1:
        return 'SELECT "[PLC]HLT101.OUTPUT" AS wet_well_level_mm FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;'
    where = _time_where(intent)
    return (
        f'SELECT AVG("[PLC]HLT101.OUTPUT") AS avg_wet_well_mm, '
        f'MIN("[PLC]HLT101.OUTPUT") AS min_wet_well_mm, '
        f'MAX("[PLC]HLT101.OUTPUT") AS max_wet_well_mm '
        f'FROM "ATL_MPS" {where};'
    )


def _build_voltage_analysis(intent: dict) -> str:
    pumps = _get_pumps(intent)
    if _is_point_in_time(intent):
        where = _time_where(intent)
        order = _point_order_by(intent)
        cols = ", ".join(f'{PUMP_VOLTAGE_COL[p]} AS p{p}_voltage' for p in pumps)
        return f'SELECT {cols}, "DateAndTime" FROM "ATL_MPS" {where} {order};'
    where = _time_where(intent)
    cols = ", ".join(
        f'AVG(CASE WHEN {PUMP_ONOFF_COL[p]} = 1 THEN {PUMP_VOLTAGE_COL[p]} END) AS p{p}_avg_voltage'
        for p in pumps
    )
    return f'SELECT {cols} FROM "ATL_MPS" {where};'


def _build_current_analysis(intent: dict) -> str:
    pumps = _get_pumps(intent)
    if _is_point_in_time(intent):
        where = _time_where(intent)
        order = _point_order_by(intent)
        cols = ", ".join(f'{PUMP_CURRENT_COL[p]} AS p{p}_current_amps' for p in pumps)
        return f'SELECT {cols}, "DateAndTime" FROM "ATL_MPS" {where} {order};'
    where = _time_where(intent)
    cols = ", ".join(
        f'AVG(CASE WHEN {PUMP_ONOFF_COL[p]} = 1 THEN {PUMP_CURRENT_COL[p]} END) AS p{p}_avg_current_amps'
        for p in pumps
    )
    return f'SELECT {cols} FROM "ATL_MPS" {where};'


def _build_active_power(intent: dict) -> str:
    pumps = _get_pumps(intent)
    if _is_point_in_time(intent):
        where = _time_where(intent)
        order = _point_order_by(intent)
        cols = ", ".join(f'{PUMP_ACTIVE_POWER_COL[p]} AS p{p}_active_power_w' for p in pumps)
        return f'SELECT {cols}, "DateAndTime" FROM "ATL_MPS" {where} {order};'
    where = _time_where(intent)
    cols = ", ".join(
        f'AVG(CASE WHEN {PUMP_ONOFF_COL[p]} = 1 THEN {PUMP_ACTIVE_POWER_COL[p]} END) AS p{p}_avg_active_power_w'
        for p in pumps
    )
    return f'SELECT {cols} FROM "ATL_MPS" {where};'


def _build_apparent_power(intent: dict) -> str:
    pumps = _get_pumps(intent)
    if _is_point_in_time(intent):
        where = _time_where(intent)
        order = _point_order_by(intent)
        cols = ", ".join(f'{PUMP_APPARENT_POWER_COL[p]} AS p{p}_apparent_power_va' for p in pumps)
        return f'SELECT {cols}, "DateAndTime" FROM "ATL_MPS" {where} {order};'
    where = _time_where(intent)
    cols = ", ".join(
        f'AVG(CASE WHEN {PUMP_ONOFF_COL[p]} = 1 THEN {PUMP_APPARENT_POWER_COL[p]} END) AS p{p}_avg_apparent_power_va'
        for p in pumps
    )
    return f'SELECT {cols} FROM "ATL_MPS" {where};'


def _build_multi_pump_concurrency(intent: dict) -> str:
    return (
        'SELECT ("[PLC]P1.ONOFF" + "[PLC]P2.ONOFF" + "[PLC]P3.ONOFF" '
        '+ "[PLC]P4.ONOFF" + "[PLC]P5.ONOFF" + "[PLC]P6.ONOFF") AS running_pumps_count '
        'FROM "ATL_MPS" ORDER BY "DateAndTime" DESC LIMIT 1;'
    )


def _build_sec_analysis(intent: dict) -> str:
    pumps = _get_pumps(intent)
    where = _time_where(intent)

    kwh_cols = ", ".join(f'{PUMP_KWH_COL[p]} AS p{p}_kwh' for p in pumps)
    sum_kwh = ", ".join(f'SUM(p{p}_kwh) AS p{p}_kwh_total' for p in pumps)
    sec_cols = ", ".join(
        f"CASE WHEN flow_volume_m3_total > 0 THEN p{p}_kwh_total / flow_volume_m3_total ELSE 0 END AS p{p}_sec"
        for p in pumps
    )

    return (
        f'WITH flow_diff AS (\n'
        f'  SELECT "DateAndTime", {kwh_cols},\n'
        f'    CASE WHEN ("[PLC]FIT101_TOTAL.D_MLD" - LAG("[PLC]FIT101_TOTAL.D_MLD") OVER (ORDER BY "DateAndTime")) > 0\n'
        f'      THEN ("[PLC]FIT101_TOTAL.D_MLD" - LAG("[PLC]FIT101_TOTAL.D_MLD") OVER (ORDER BY "DateAndTime")) * 60000\n'
        f'      ELSE 0 END AS flow_volume_m3\n'
        f'  FROM "ATL_MPS"\n'
        f'  {where}\n'
        f'),\n'
        f'stats AS (\n'
        f'  SELECT {sum_kwh}, SUM(flow_volume_m3) AS flow_volume_m3_total\n'
        f'  FROM flow_diff\n'
        f')\n'
        f'SELECT {sec_cols}\n'
        f'FROM stats;'
    )


def _build_pump_performance(intent: dict) -> str | None:
    # Too complex for template — fall back to LLM
    return None


# ── Builder registry ─────────────────────────────────────────────────────

_BUILDERS = {
    "current_status": _build_current_status,
    "pump_runtime": _build_pump_runtime,
    "pump_start_count": _build_pump_start_count,
    "energy_analysis": _build_energy_analysis,
    "power_factor": _build_power_factor,
    "flow_analysis": _build_flow_analysis,
    "wet_well_level": _build_wet_well_level,
    "voltage_analysis": _build_voltage_analysis,
    "current_analysis": _build_current_analysis,
    "active_power": _build_active_power,
    "apparent_power": _build_apparent_power,
    "multi_pump_concurrency": _build_multi_pump_concurrency,
    "sec_analysis": _build_sec_analysis,
    "pump_performance": _build_pump_performance,
}
