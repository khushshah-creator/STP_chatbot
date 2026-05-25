"""
Code-based answer formatter for STP Chatbot.
Generates natural-language answers from DB results — NO LLM calls.
Returns None only when results are too complex for template formatting.
"""

import json


def format_answer(
    user_message: str,
    intent: dict,
    sql: str,
    db_rows: list[dict],
    db_error: str | None,
) -> str | None:
    """
    Format a natural-language answer from DB results.

    Returns:
        str if we can format it, None to signal LLM fallback.
    """
    if db_error:
        return None  # Let LLM explain the error

    if not db_rows:
        return "📭 No data found for the requested period. Try a broader time range or check if the pumps were active."

    intent_name = intent.get("intent", "unknown")
    formatter = _FORMATTERS.get(intent_name)

    if formatter:
        try:
            return formatter(intent, db_rows)
        except Exception:
            return None  # Fallback to LLM

    # For unknown intents, try generic formatting
    return _format_generic(intent, db_rows)


def _round_val(val, decimals=2):
    """Safely round a value."""
    if val is None:
        return "N/A"
    try:
        f = float(val)
        if f == int(f) and abs(f) < 1e10:
            return str(int(f))
        return f"{f:.{decimals}f}"
    except (ValueError, TypeError):
        return str(val)


def _format_single_row(row: dict, units: dict | None = None) -> str:
    """Format a single-row result into readable text."""
    if not units:
        units = {}
    parts = []
    for key, val in row.items():
        unit = units.get(key, "")
        display_key = key.replace("_", " ").title()
        parts.append(f"• **{display_key}**: {_round_val(val)} {unit}".strip())
    return "\n".join(parts)


# ── Intent-specific formatters ────────────────────────────────────────────

def _format_current_status(intent: dict, rows: list[dict]) -> str:
    row = rows[0]

    if "current_flow_m3hr" in row:
        return f"📊 The current flow rate is **{_round_val(row['current_flow_m3hr'])} m³/hr**."
    if "wet_well_level_mm" in row:
        return f"📊 The current wet well level is **{_round_val(row['wet_well_level_mm'])} mm**."

    # Pump status
    parts = []
    for key, val in row.items():
        pump_num = key.replace("p", "").replace("_status", "")
        status = "🟢 ON" if val == 1 else "🔴 OFF"
        parts.append(f"• Pump {pump_num}: {status}")
    return "**Pump Status:**\n" + "\n".join(parts)


def _format_pump_runtime(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    parts = []
    for key, val in row.items():
        pump_label = key.split("_")[0].upper()  # p1 -> P1
        mins = int(float(val)) if val is not None else 0
        hrs = mins // 60
        rem = mins % 60
        if hrs > 0:
            parts.append(f"• {pump_label}: **{hrs}h {rem}m** ({mins} minutes)")
        else:
            parts.append(f"• {pump_label}: **{mins} minutes**")
    return "⏱️ **Pump Runtime:**\n" + "\n".join(parts)


def _format_pump_start_count(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    parts = []
    for key, val in row.items():
        pump_label = key.split("_")[0].upper()
        count = int(float(val)) if val is not None else 0
        parts.append(f"• {pump_label}: **{count} starts**")
    return "🔄 **Pump Start Counts:**\n" + "\n".join(parts)


def _format_energy_analysis(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    parts = []
    total = 0.0
    for key, val in row.items():
        pump_label = key.split("_")[0].upper()
        kwh = float(val) if val is not None else 0
        total += kwh
        parts.append(f"• {pump_label}: **{_round_val(kwh)} kWh**")
    result = "⚡ **Energy Consumption:**\n" + "\n".join(parts)
    if len(parts) > 1:
        result += f"\n• **Total: {_round_val(total)} kWh**"
    return result


def _format_flow_analysis(intent: dict, rows: list[dict]) -> str:
    row = rows[0]

    if "current_flow_m3hr" in row:
        return f"🌊 The current flow rate is **{_round_val(row['current_flow_m3hr'])} m³/hr**."
    if "avg_flow_m3hr" in row:
        return f"🌊 The average flow rate is **{_round_val(row['avg_flow_m3hr'])} m³/hr**."
    if "total_flow_mld" in row:
        return f"🌊 The total flow volume is **{_round_val(row['total_flow_mld'])} MLD**."

    return _format_generic(intent, rows)


def _format_wet_well_level(intent: dict, rows: list[dict]) -> str:
    row = rows[0]

    if "wet_well_level_mm" in row:
        return f"📏 The current wet well level is **{_round_val(row['wet_well_level_mm'])} mm**."

    parts = []
    if "avg_wet_well_mm" in row:
        parts.append(f"• Average: **{_round_val(row['avg_wet_well_mm'])} mm**")
    if "min_wet_well_mm" in row:
        parts.append(f"• Minimum: **{_round_val(row['min_wet_well_mm'])} mm**")
    if "max_wet_well_mm" in row:
        parts.append(f"• Maximum: **{_round_val(row['max_wet_well_mm'])} mm**")

    return "📏 **Wet Well Level:**\n" + "\n".join(parts) if parts else _format_generic(intent, rows)


def _format_voltage_analysis(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    parts = []
    for key, val in row.items():
        pump_label = key.split("_")[0].upper()
        parts.append(f"• {pump_label}: **{_round_val(val)} V**")
    return "🔌 **Average Voltage (when running):**\n" + "\n".join(parts)


def _format_current_analysis(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    parts = []
    for key, val in row.items():
        pump_label = key.split("_")[0].upper()
        parts.append(f"• {pump_label}: **{_round_val(val)} A**")
    return "⚡ **Average Current (when running):**\n" + "\n".join(parts)


def _format_active_power(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    parts = []
    for key, val in row.items():
        pump_label = key.split("_")[0].upper()
        parts.append(f"• {pump_label}: **{_round_val(val)} W**")
    return "💡 **Average Active Power (when running):**\n" + "\n".join(parts)


def _format_apparent_power(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    parts = []
    for key, val in row.items():
        pump_label = key.split("_")[0].upper()
        parts.append(f"• {pump_label}: **{_round_val(val)} VA**")
    return "💡 **Average Apparent Power (when running):**\n" + "\n".join(parts)


def _format_power_factor(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    parts = []
    for key, val in row.items():
        pump_label = key.split("_")[0].upper()
        parts.append(f"• {pump_label}: **{_round_val(val, 4)}**")
    return "📊 **Power Factor (when running):**\n" + "\n".join(parts)


def _format_multi_pump_concurrency(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    count = row.get("running_pumps_count", 0)
    count = int(float(count)) if count is not None else 0
    if count == 0:
        return "Currently **no pumps** are running."
    return f"Currently **{count} pump{'s' if count != 1 else ''}** {'are' if count != 1 else 'is'} running."


def _format_sec_analysis(intent: dict, rows: list[dict]) -> str:
    row = rows[0]
    parts = []
    for key, val in row.items():
        pump_label = key.split("_")[0].upper()
        parts.append(f"• {pump_label}: **{_round_val(val, 4)} kWh/m³**")
    return "📊 **Specific Energy Consumption (SEC):**\n" + "\n".join(parts)


def _format_generic(intent: dict, rows: list[dict]) -> str | None:
    """Generic formatter for single-row results. Returns None for multi-row."""
    if len(rows) > 10:
        return None  # Too many rows — let LLM summarize

    if len(rows) == 1:
        return "📊 **Results:**\n" + _format_single_row(rows[0])

    # Multiple rows — build a simple table
    if len(rows) <= 10:
        headers = list(rows[0].keys())
        header_line = "| " + " | ".join(h.replace("_", " ").title() for h in headers) + " |"
        sep_line = "| " + " | ".join("---" for _ in headers) + " |"
        data_lines = []
        for row in rows:
            data_lines.append("| " + " | ".join(_round_val(row.get(h)) for h in headers) + " |")
        return "📊 **Results:**\n\n" + header_line + "\n" + sep_line + "\n" + "\n".join(data_lines)

    return None


# ── Formatter registry ────────────────────────────────────────────────────

_FORMATTERS = {
    "current_status": _format_current_status,
    "pump_runtime": _format_pump_runtime,
    "pump_start_count": _format_pump_start_count,
    "energy_analysis": _format_energy_analysis,
    "flow_analysis": _format_flow_analysis,
    "wet_well_level": _format_wet_well_level,
    "voltage_analysis": _format_voltage_analysis,
    "current_analysis": _format_current_analysis,
    "active_power": _format_active_power,
    "apparent_power": _format_apparent_power,
    "power_factor": _format_power_factor,
    "multi_pump_concurrency": _format_multi_pump_concurrency,
    "sec_analysis": _format_sec_analysis,
    "example_query": _format_generic,
}
