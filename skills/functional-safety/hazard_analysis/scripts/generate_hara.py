"""
HARA Builder — generates a multi-tab ISO 26262 HARA workbook from a JSON input.

Usage:
    python generate_hara.py <input.json> <output.xlsx>

Input JSON schema (see examples/sample_input.json for a full example):
{
  "item": {name, abbr, project, doc_id, revision, date, author, approver, company},
  "scope_description": str,
  "boundary_diagram_notes": str,            // text fallback if no image embedded
  "assumptions": [{id, category, assumption}],
  "interfaces": [{id, interface, direction, description}],
  "functions": [{id, name, description, kinetic_authority}],   // kinetic_authority: high|medium|low|none
  "environments": {locations: [...], weather: [...]},          // optional, defaults from references/operating_environments.md
  "function_malfunction_ratings": [
      {function_id, malfunction_id, classification, subsumed_by, rationale, hazard_description}
  ],
  "rating_overrides": [                                        // optional per (F,M,L,W) overrides
      {function_id, malfunction_id, location_code, weather_code, S, E, C, S_rationale, E_rationale, C_rationale}
  ]
}
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


# ---------------------------------------------------------------------------
# Reference data — embedded so the script is self-contained
# ---------------------------------------------------------------------------

MALFUNCTIONS: list[dict[str, str]] = [
    {"id": "M01", "name": "No Function",                "definition": "Function never executes when commanded."},
    {"id": "M02", "name": "Stops Functioning",          "definition": "Function executes initially, then stops while still commanded."},
    {"id": "M03", "name": "Unrequested Function",       "definition": "Function executes without being commanded."},
    {"id": "M04", "name": "Function Stuck",             "definition": "Function holds last commanded value, ignores new commands."},
    {"id": "M05", "name": "Excessive Function",         "definition": "Function output magnitude exceeds command."},
    {"id": "M06", "name": "Partial Function",           "definition": "Function output magnitude is less than command, but non-zero."},
    {"id": "M07", "name": "Functions Early",            "definition": "Function executes before the trigger condition is met."},
    {"id": "M08", "name": "Functions Late",             "definition": "Function executes after the trigger condition has passed."},
    {"id": "M09", "name": "Function Applies Too Short", "definition": "Output duration shorter than commanded."},
    {"id": "M10", "name": "Function Applies Too Long",  "definition": "Output duration longer than commanded."},
    {"id": "M11", "name": "Function is Delayed",        "definition": "Function eventually executes correctly but with latency beyond spec."},
    {"id": "M12", "name": "Inverse Function",           "definition": "Function executes in the opposite direction of the command."},
    {"id": "M13", "name": "Erratic or Intermittent",    "definition": "Function output oscillates or chatters around the command."},
    {"id": "M14", "name": "Function is Uneven",         "definition": "Function output has non-monotonic / asymmetric profile."},
]

# Adversarial malfunctions tend to fight the driver → bias C upward.
ADVERSARIAL_MALFUNCTIONS = {"M03", "M05", "M07", "M12"}

DEFAULT_LOCATIONS = [
    {"code": "L01", "name": "Parking lot",           "speed_min_kph": 0,  "speed_max_kph": 15,  "default_e": "E3"},
    {"code": "L02", "name": "Urban / city street",   "speed_min_kph": 15, "speed_max_kph": 60,  "default_e": "E4"},
    {"code": "L03", "name": "Suburban arterial",     "speed_min_kph": 40, "speed_max_kph": 80,  "default_e": "E4"},
    {"code": "L04", "name": "Rural highway",         "speed_min_kph": 60, "speed_max_kph": 100, "default_e": "E3"},
    {"code": "L05", "name": "Interstate / motorway", "speed_min_kph": 90, "speed_max_kph": 130, "default_e": "E4"},
    {"code": "L06", "name": "Off-road / unpaved",    "speed_min_kph": 0,  "speed_max_kph": 60,  "default_e": "E1"},
]

DEFAULT_WEATHER = [
    {"code": "W01", "name": "Dry, daylight", "default_e": "E4"},
    {"code": "W02", "name": "Wet",           "default_e": "E3"},
    {"code": "W03", "name": "Night, dry",    "default_e": "E3"},
    {"code": "W04", "name": "Heavy rain",    "default_e": "E2"},
    {"code": "W05", "name": "Snow / ice",    "default_e": "E2"},
    {"code": "W06", "name": "Fog",           "default_e": "E1"},
]

SEVERITY_TABLE = [
    {"S": "S0", "AIS": "AIS 0",                    "description": "No injuries; material damage only.",                    "examples": "Bumper scrape in a parking maneuver."},
    {"S": "S1", "AIS": "AIS 1–2 (≥10% prob.)",     "description": "Light to moderate injuries.",                            "examples": "Bruises, sprains, minor lacerations."},
    {"S": "S2", "AIS": "AIS 3–6 (<10%) + AIS 1–2 (>10%)", "description": "Severe to life-threatening (survival probable).", "examples": "Concussion, multiple fractures."},
    {"S": "S3", "AIS": "AIS 3–6 (≥10% prob.)",     "description": "Life-threatening (survival uncertain) to fatal.",        "examples": "High-speed loss of control, full unintended brake on highway."},
]

EXPOSURE_TABLE = [
    {"E": "E0", "probability": "Incredibly unlikely",     "anchor": "< 10⁻⁴ of operating time",                "examples": "Closed test track scenario."},
    {"E": "E1", "probability": "Very low probability",    "anchor": "< 1% of time, < once per year",            "examples": "Towing in alpine winter pass."},
    {"E": "E2", "probability": "Low probability",         "anchor": "1–10% of time, < once per month",          "examples": "Heavy snowfall, towing in rain."},
    {"E": "E3", "probability": "Medium probability",      "anchor": "10–50% of time, < once per week",          "examples": "City stop-and-go, wet road, night."},
    {"E": "E4", "probability": "High probability",        "anchor": "> 50% of time, > once per week",           "examples": "Highway cruising, dry pavement, daytime."},
]

CONTROLLABILITY_TABLE = [
    {"C": "C0", "description": "Controllable in general", "anchor": "Any reasonable driver action avoids harm.",     "examples": "Slow cruise speed creep on empty highway."},
    {"C": "C1", "description": "Simply controllable",     "anchor": "≥99% of drivers can avoid the harm.",           "examples": "Loss of brake assist (still have hydraulic pedal)."},
    {"C": "C2", "description": "Normally controllable",   "anchor": "≥90% of drivers can avoid the harm.",           "examples": "Moderate unintended deceleration during cruise."},
    {"C": "C3", "description": "Difficult / uncontrollable", "anchor": "<90% can avoid; or no useful action exists.","examples": "Inverse steering torque at highway speed."},
]

# ASIL matrix (S, E, C1, C2, C3). S0 → "—", E0 → "QM" handled in formula.
ASIL_MATRIX = [
    ("S1", "E1", "QM", "QM", "QM"),
    ("S1", "E2", "QM", "QM", "QM"),
    ("S1", "E3", "QM", "QM", "A"),
    ("S1", "E4", "QM", "A",  "B"),
    ("S2", "E1", "QM", "QM", "QM"),
    ("S2", "E2", "QM", "QM", "A"),
    ("S2", "E3", "QM", "A",  "B"),
    ("S2", "E4", "A",  "B",  "C"),
    ("S3", "E1", "QM", "QM", "A"),
    ("S3", "E2", "QM", "A",  "B"),
    ("S3", "E3", "A",  "B",  "C"),
    ("S3", "E4", "B",  "C",  "D"),
]

E_ORDER = {"E0": 0, "E1": 1, "E2": 2, "E3": 3, "E4": 4}


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------

FONT_NAME = "Calibri"

NAVY = "1F3864"
LIGHT_BLUE = "D9E2F3"
ALT_ROW = "F2F2F2"
WARN_YELLOW = "FFF2CC"
GREEN_OK = "C6EFCE"
RED_BAD = "F8CBAD"

THIN = Side(border_style="thin", color="BFBFBF")
MEDIUM = Side(border_style="medium", color="404040")
BORDER_ALL = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def title_font(size: int = 16) -> Font:
    return Font(name=FONT_NAME, size=size, bold=True, color="FFFFFF")


def header_font() -> Font:
    return Font(name=FONT_NAME, size=11, bold=True, color="FFFFFF")


def body_font() -> Font:
    return Font(name=FONT_NAME, size=10)


def style_title_row(ws, row: int, last_col: int, text: str) -> None:
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=last_col)
    cell = ws.cell(row=row, column=1, value=text)
    cell.font = title_font(14)
    cell.fill = PatternFill("solid", fgColor=NAVY)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[row].height = 28


def style_header_row(ws, row: int, headers: list[str]) -> None:
    for col, h in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=h)
        cell.font = header_font()
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER_ALL
    ws.row_dimensions[row].height = 32


def stripe_body(ws, start_row: int, end_row: int, last_col: int) -> None:
    for r in range(start_row, end_row + 1):
        fill = PatternFill("solid", fgColor=ALT_ROW) if (r - start_row) % 2 else None
        for c in range(1, last_col + 1):
            cell = ws.cell(row=r, column=c)
            cell.font = body_font()
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = BORDER_ALL
            if fill:
                cell.fill = fill


def autosize(ws, widths: dict[int, int]) -> None:
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = width


# ---------------------------------------------------------------------------
# Heuristic auto-rating
# ---------------------------------------------------------------------------

HIGH_AUTHORITY_MALFUNCTIONS_BY_RANK = {
    "high":   {"M01", "M02", "M03", "M04", "M05", "M06", "M07", "M08", "M09", "M10", "M11", "M12", "M13", "M14"},
    "medium": {"M01", "M03", "M05", "M06", "M12", "M13"},
    "low":    {"M03", "M05", "M12"},
    "none":   set(),
}


def suggest_severity(speed_max_kph: int, malfunction_id: str, kinetic_authority: str, weather_code: str) -> tuple[str, str]:
    """Return (S_rating, rationale)."""
    auth = (kinetic_authority or "medium").lower()
    if auth == "none":
        return "S0", f"S0. Function has no kinetic authority; malfunction cannot produce injury."

    relevant = HIGH_AUTHORITY_MALFUNCTIONS_BY_RANK.get(auth, set())
    if malfunction_id not in relevant:
        return "S1", f"S1. {malfunction_id} on a {auth}-authority function produces only minor disturbance at {speed_max_kph} km/h."

    weather_bias = weather_code in {"W04", "W05", "W06"}
    if speed_max_kph >= 100:
        return "S3", f"S3. Speed band up to {speed_max_kph} km/h. Loss of control or unintended kinetic input at this speed yields AIS 3–6 ≥10% probability." + (" Weather amplifies." if weather_bias else "")
    if speed_max_kph >= 60:
        return "S2", f"S2. Speed band up to {speed_max_kph} km/h. Severe injury credible (AIS 3–6 <10%, AIS 1–2 >10%)." + (" Weather amplifies." if weather_bias else "")
    if speed_max_kph >= 30:
        s = "S2" if malfunction_id in ADVERSARIAL_MALFUNCTIONS else "S1"
        return s, f"{s}. Speed band up to {speed_max_kph} km/h with {'adversarial' if s == 'S2' else 'non-adversarial'} malfunction {malfunction_id}."
    return "S1", f"S1. Low speed band ({speed_max_kph} km/h max). Light to moderate injuries credible."


def suggest_exposure(location: dict, weather: dict) -> tuple[str, str]:
    """Combined E is the rarer of the two contributing bands."""
    loc_e = location.get("default_e", "E3")
    wx_e = weather.get("default_e", "E4")
    combined = min([loc_e, wx_e], key=lambda x: E_ORDER.get(x, 4))
    return combined, (
        f"{combined}. Location '{location['name']}' (default {loc_e}) combined with weather '{weather['name']}' "
        f"(default {wx_e}). Combined exposure governed by rarer condition."
    )


def suggest_controllability(speed_max_kph: int, malfunction_id: str) -> tuple[str, str]:
    """Higher speed + adversarial malfunction → higher C."""
    if malfunction_id in ADVERSARIAL_MALFUNCTIONS and speed_max_kph >= 80:
        return "C3", f"C3. Adversarial malfunction {malfunction_id} at {speed_max_kph} km/h gives <0.5 s reaction window; <90% of drivers can avoid harm."
    if speed_max_kph >= 100:
        return "C3", f"C3. Highway speed band up to {speed_max_kph} km/h limits driver reaction time below safe threshold."
    if speed_max_kph >= 60:
        return "C2", f"C2. Mid-speed band ({speed_max_kph} km/h max). ≥90% of average drivers can avoid harm with normal reaction."
    return "C2", f"C2. Low to mid speed ({speed_max_kph} km/h max). Driver retains authority but malfunction is unexpected; assume normally controllable."


def suggest_classification(function: dict, malfunction_id: str) -> tuple[str, str]:
    """Auto-suggest SC / NSC / NA when the user did not provide one."""
    auth = (function.get("kinetic_authority") or "medium").lower()
    if auth == "none":
        return "NSC", "Function has no kinetic authority; malfunction cannot lead to a hazardous event."
    if auth == "low" and malfunction_id not in {"M03", "M05", "M12"}:
        return "NSC", f"Low-authority function; {malfunction_id} unlikely to produce a hazardous outcome."
    return "SC", f"{auth.title()}-authority function exposed to {malfunction_id}; safety-critical pending detailed rating."


# ---------------------------------------------------------------------------
# Tab builders
# ---------------------------------------------------------------------------

def build_title_page(wb: Workbook, item: dict) -> None:
    ws = wb.create_sheet("00_Title_Page")
    ws.sheet_view.showGridLines = False

    style_title_row(ws, 2, 4, "Hazard Analysis and Risk Assessment (HARA)")
    style_title_row(ws, 3, 4, f"{item.get('name', '')}  ({item.get('abbr', '')})")

    fields = [
        ("Project",        item.get("project", "")),
        ("Document ID",    item.get("doc_id", "")),
        ("Revision",       item.get("revision", "")),
        ("Date",           item.get("date", "")),
        ("Author",         item.get("author", "")),
        ("Approver",       item.get("approver", "")),
        ("Company",        item.get("company", "")),
        ("Standard",       "ISO 26262:2018, Part 3 (Concept Phase)"),
        ("Methodology",    "14-Malfunction Guide-Word Cartesian HARA"),
    ]

    row = 6
    for label, value in fields:
        ws.cell(row=row, column=2, value=label).font = Font(name=FONT_NAME, size=11, bold=True, color=NAVY)
        ws.cell(row=row, column=3, value=value).font = body_font()
        ws.cell(row=row, column=3).alignment = Alignment(horizontal="left")
        row += 1

    autosize(ws, {1: 4, 2: 22, 3: 60, 4: 4})


def build_doc_control(wb: Workbook) -> None:
    ws = wb.create_sheet("01_Document_Control")
    ws.sheet_view.showGridLines = False

    style_title_row(ws, 1, 5, "Document Control")

    style_header_row(ws, 3, ["Revision", "Date", "Author", "Description of change", "Approver"])
    placeholders = [
        ("0.1", "", "", "Initial draft generated by hara-builder skill", ""),
        ("0.2", "", "", "Internal review comments incorporated", ""),
        ("1.0", "", "", "Released for FSC", ""),
    ]
    for i, row_data in enumerate(placeholders, start=4):
        for col, val in enumerate(row_data, start=1):
            ws.cell(row=i, column=col, value=val)
    stripe_body(ws, 4, 4 + len(placeholders) - 1, 5)

    # Distribution list
    style_title_row(ws, 10, 5, "Distribution List")
    style_header_row(ws, 11, ["Name", "Role", "Organization", "Email", "Notes"])
    for i in range(12, 16):
        for c in range(1, 6):
            ws.cell(row=i, column=c, value="")
    stripe_body(ws, 12, 15, 5)

    autosize(ws, {1: 12, 2: 14, 3: 22, 4: 50, 5: 22})


def build_assumptions(wb: Workbook, assumptions: list[dict]) -> None:
    ws = wb.create_sheet("02_Assumptions")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 4, "Assumptions and Item Boundary Conditions")

    style_header_row(ws, 3, ["ID", "Category", "Assumption", "Rationale / Source"])
    if not assumptions:
        assumptions = [
            {"id": "A01", "category": "Vehicle",   "assumption": "Passenger vehicle, GVW < 3500 kg.",                                "rationale": "Default scope."},
            {"id": "A02", "category": "Driver",    "assumption": "Average non-expert driver, valid license, not impaired.",          "rationale": "Per ISO 26262-3 controllability definition."},
            {"id": "A03", "category": "Markets",   "assumption": "North America, Europe; LHD and RHD.",                              "rationale": "Default scope."},
            {"id": "A04", "category": "Lifecycle", "assumption": "HARA covers normal operation; service/repair excluded.",           "rationale": "Per ISO 26262-3, Clause 6."},
            {"id": "A05", "category": "Cybersec",  "assumption": "Cybersecurity threats handled separately under ISO/SAE 21434.",     "rationale": "Scope partition."},
        ]
    for i, a in enumerate(assumptions, start=4):
        ws.cell(row=i, column=1, value=a.get("id", ""))
        ws.cell(row=i, column=2, value=a.get("category", ""))
        ws.cell(row=i, column=3, value=a.get("assumption", ""))
        ws.cell(row=i, column=4, value=a.get("rationale", ""))
    stripe_body(ws, 4, 3 + len(assumptions), 4)
    autosize(ws, {1: 8, 2: 16, 3: 60, 4: 40})


def build_architecture_boundary(wb: Workbook, scope_description: str, boundary_notes: str, interfaces: list[dict]) -> None:
    ws = wb.create_sheet("03_Architecture_Boundary")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 5, "Architecture Boundary — Item Definition")

    ws.cell(row=3, column=1, value="Item Scope").font = Font(name=FONT_NAME, bold=True, size=11, color=NAVY)
    ws.merge_cells(start_row=3, start_column=2, end_row=3, end_column=5)
    ws.cell(row=3, column=2, value=scope_description or "(Describe what the item is, what it does, and what it does NOT do.)").alignment = Alignment(wrap_text=True, vertical="top")

    ws.cell(row=5, column=1, value="Boundary Diagram").font = Font(name=FONT_NAME, bold=True, size=11, color=NAVY)
    ws.merge_cells(start_row=5, start_column=2, end_row=5, end_column=5)
    ws.cell(row=5, column=2,
            value=boundary_notes or "(Insert boundary block diagram image here. Use Insert → Picture. "
                                    "Diagram should show: (a) item ECUs and SW components, (b) sensors providing inputs, "
                                    "(c) actuators receiving outputs, (d) other vehicle systems exchanging signals, "
                                    "(e) the human driver as the supervising agent.)").alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[5].height = 120

    # Interfaces table
    style_title_row(ws, 8, 5, "External Interfaces")
    style_header_row(ws, 9, ["Interface ID", "Counterpart", "Direction", "Signal / Data", "Notes"])
    if not interfaces:
        interfaces = [
            {"id": "I01", "interface": "Brake actuator", "direction": "Out", "description": "Hydraulic pressure command per wheel"},
            {"id": "I02", "interface": "Wheel speed sensors", "direction": "In", "description": "Per-wheel rotational speed"},
            {"id": "I03", "interface": "Yaw rate / lat acc IMU", "direction": "In", "description": "Vehicle motion state"},
            {"id": "I04", "interface": "Steering angle sensor", "direction": "In", "description": "Driver intent"},
            {"id": "I05", "interface": "Driver display / chime", "direction": "Out", "description": "Warning to driver"},
        ]
    for i, intf in enumerate(interfaces, start=10):
        ws.cell(row=i, column=1, value=intf.get("id", ""))
        ws.cell(row=i, column=2, value=intf.get("interface", ""))
        ws.cell(row=i, column=3, value=intf.get("direction", ""))
        ws.cell(row=i, column=4, value=intf.get("description", ""))
        ws.cell(row=i, column=5, value=intf.get("notes", ""))
    stripe_body(ws, 10, 9 + len(interfaces), 5)
    autosize(ws, {1: 14, 2: 26, 3: 12, 4: 40, 5: 30})


def build_functions(wb: Workbook, functions: list[dict]) -> None:
    ws = wb.create_sheet("04_Functions")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 4, "Item Functions")
    style_header_row(ws, 3, ["Function ID", "Function Name", "Description", "Kinetic Authority"])
    for i, f in enumerate(functions, start=4):
        ws.cell(row=i, column=1, value=f.get("id", ""))
        ws.cell(row=i, column=2, value=f.get("name", ""))
        ws.cell(row=i, column=3, value=f.get("description", ""))
        ws.cell(row=i, column=4, value=(f.get("kinetic_authority") or "medium").lower())
    stripe_body(ws, 4, 3 + len(functions), 4)
    autosize(ws, {1: 12, 2: 28, 3: 60, 4: 18})


def build_malfunctions(wb: Workbook) -> None:
    ws = wb.create_sheet("05_Malfunctions")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 3, "Malfunction Guide Words (M01–M14)")
    style_header_row(ws, 3, ["ID", "Name", "Definition"])
    for i, m in enumerate(MALFUNCTIONS, start=4):
        ws.cell(row=i, column=1, value=m["id"])
        ws.cell(row=i, column=2, value=m["name"])
        ws.cell(row=i, column=3, value=m["definition"])
    stripe_body(ws, 4, 3 + len(MALFUNCTIONS), 3)
    autosize(ws, {1: 8, 2: 28, 3: 70})


def build_operating_environment(wb: Workbook, environments: dict) -> None:
    ws = wb.create_sheet("06_Operating_Environment")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 5, "Operating Environment — Locations and Weather")

    locations = environments.get("locations") or DEFAULT_LOCATIONS
    weather = environments.get("weather") or DEFAULT_WEATHER

    style_header_row(ws, 3, ["Location Code", "Location", "Speed min (km/h)", "Speed max (km/h)", "Default E"])
    for i, loc in enumerate(locations, start=4):
        ws.cell(row=i, column=1, value=loc["code"])
        ws.cell(row=i, column=2, value=loc["name"])
        ws.cell(row=i, column=3, value=loc.get("speed_min_kph", 0))
        ws.cell(row=i, column=4, value=loc.get("speed_max_kph", 0))
        ws.cell(row=i, column=5, value=loc.get("default_e", "E3"))
    stripe_body(ws, 4, 3 + len(locations), 5)

    base = 4 + len(locations) + 2
    style_title_row(ws, base, 5, "Weather / Surface")
    style_header_row(ws, base + 1, ["Weather Code", "Condition", "", "", "Default E"])
    for i, wx in enumerate(weather, start=base + 2):
        ws.cell(row=i, column=1, value=wx["code"])
        ws.cell(row=i, column=2, value=wx["name"])
        ws.cell(row=i, column=5, value=wx.get("default_e", "E3"))
    stripe_body(ws, base + 2, base + 1 + len(weather), 5)

    autosize(ws, {1: 14, 2: 28, 3: 16, 4: 16, 5: 12})


def build_severity_reference(wb: Workbook) -> None:
    ws = wb.create_sheet("07_Severity_Reference")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 4, "Severity (S) — Mapped to AIS")
    style_header_row(ws, 3, ["S", "AIS Band", "Description", "Examples"])
    for i, row in enumerate(SEVERITY_TABLE, start=4):
        ws.cell(row=i, column=1, value=row["S"])
        ws.cell(row=i, column=2, value=row["AIS"])
        ws.cell(row=i, column=3, value=row["description"])
        ws.cell(row=i, column=4, value=row["examples"])
    stripe_body(ws, 4, 3 + len(SEVERITY_TABLE), 4)
    autosize(ws, {1: 6, 2: 32, 3: 50, 4: 50})


def build_exposure_reference(wb: Workbook) -> None:
    ws = wb.create_sheet("08_Exposure_Reference")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 4, "Exposure (E) — Probability of Operational Situation")
    ws.merge_cells("A3:D3")
    note = ws.cell(row=3, column=1,
                   value="Note: Exposure is rated against the OPERATIONAL SITUATION, not the hazard itself. "
                         "Hazard frequency is captured downstream via PMHF (random hardware failure) analysis.")
    note.font = Font(name=FONT_NAME, italic=True, size=10, color="9C5700")
    note.fill = PatternFill("solid", fgColor=WARN_YELLOW)
    note.alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[3].height = 36

    style_header_row(ws, 5, ["E", "Probability", "Quantitative Anchor", "Examples"])
    for i, row in enumerate(EXPOSURE_TABLE, start=6):
        ws.cell(row=i, column=1, value=row["E"])
        ws.cell(row=i, column=2, value=row["probability"])
        ws.cell(row=i, column=3, value=row["anchor"])
        ws.cell(row=i, column=4, value=row["examples"])
    stripe_body(ws, 6, 5 + len(EXPOSURE_TABLE), 4)
    autosize(ws, {1: 6, 2: 28, 3: 38, 4: 50})


def build_controllability_reference(wb: Workbook) -> None:
    ws = wb.create_sheet("09_Controllability_Reference")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 4, "Controllability (C) — Driver's Ability to Avoid Harm")
    style_header_row(ws, 3, ["C", "Description", "Quantitative Anchor", "Examples"])
    for i, row in enumerate(CONTROLLABILITY_TABLE, start=4):
        ws.cell(row=i, column=1, value=row["C"])
        ws.cell(row=i, column=2, value=row["description"])
        ws.cell(row=i, column=3, value=row["anchor"])
        ws.cell(row=i, column=4, value=row["examples"])
    stripe_body(ws, 4, 3 + len(CONTROLLABILITY_TABLE), 4)
    autosize(ws, {1: 6, 2: 28, 3: 38, 4: 50})


def build_asil_matrix(wb: Workbook) -> None:
    ws = wb.create_sheet("10_ASIL_Matrix")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 6, "ASIL Determination Matrix (ISO 26262-3:2018)")
    # Column layout: A=S, B=E, C=Key (helper), D=C1, E=C2, F=C3
    ws.cell(row=3, column=1, value="S").font = header_font(); ws.cell(row=3, column=1).fill = PatternFill("solid", fgColor=NAVY)
    ws.cell(row=3, column=2, value="E").font = header_font(); ws.cell(row=3, column=2).fill = PatternFill("solid", fgColor=NAVY)
    ws.cell(row=3, column=3, value="Key").font = header_font(); ws.cell(row=3, column=3).fill = PatternFill("solid", fgColor=NAVY)
    ws.cell(row=3, column=4, value="C1").font = header_font(); ws.cell(row=3, column=4).fill = PatternFill("solid", fgColor=NAVY)
    ws.cell(row=3, column=5, value="C2").font = header_font(); ws.cell(row=3, column=5).fill = PatternFill("solid", fgColor=NAVY)
    ws.cell(row=3, column=6, value="C3").font = header_font(); ws.cell(row=3, column=6).fill = PatternFill("solid", fgColor=NAVY)
    for c in range(1, 7):
        ws.cell(row=3, column=c).alignment = Alignment(horizontal="center")
        ws.cell(row=3, column=c).border = BORDER_ALL
    ws.row_dimensions[3].height = 24

    for i, (s, e, c1, c2, c3) in enumerate(ASIL_MATRIX, start=4):
        ws.cell(row=i, column=1, value=s)
        ws.cell(row=i, column=2, value=e)
        ws.cell(row=i, column=3, value=f"=A{i}&\"-\"&B{i}")
        ws.cell(row=i, column=4, value=c1)
        ws.cell(row=i, column=5, value=c2)
        ws.cell(row=i, column=6, value=c3)
    stripe_body(ws, 4, 3 + len(ASIL_MATRIX), 6)

    # Note rows
    note_row = 4 + len(ASIL_MATRIX) + 1
    ws.merge_cells(start_row=note_row, start_column=1, end_row=note_row, end_column=6)
    n = ws.cell(row=note_row, column=1, value="S0 → no safety requirement (—). E0 → QM regardless of S, C. Handled in HARA tab formula.")
    n.font = Font(name=FONT_NAME, italic=True, size=10)
    n.fill = PatternFill("solid", fgColor=WARN_YELLOW)
    autosize(ws, {1: 6, 2: 6, 3: 10, 4: 8, 5: 8, 6: 8})


def build_function_malfunction_filter(
    wb: Workbook,
    functions: list[dict],
    user_ratings: list[dict],
) -> list[dict]:
    """
    Build the SC/NSC/NA filter tab. Returns the list of safety-critical pairs
    (each pair as a dict) for downstream Cartesian expansion.
    """
    ws = wb.create_sheet("11_Function_x_Malfunction")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 7, "Function × Malfunction Safety-Criticality Filter")

    note = ws.cell(row=2, column=1,
                   value="Each function is evaluated against all 14 malfunction guide words. Classification: "
                         "SC = Safety Critical, NSC = Not Safety Critical, NA = Not Applicable. "
                         "Use 'Subsumed by' when one malfunction's response is identical to another already rated SC.")
    note.font = Font(name=FONT_NAME, italic=True, size=10, color="9C5700")
    note.fill = PatternFill("solid", fgColor=WARN_YELLOW)
    note.alignment = Alignment(wrap_text=True)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=7)
    ws.row_dimensions[2].height = 36

    style_header_row(ws, 4, ["Function ID", "Function", "Malfunction ID", "Malfunction", "Classification", "Subsumed by", "Hazard Description / Rationale"])

    # Index user ratings by (function_id, malfunction_id)
    rating_index = {(r["function_id"], r["malfunction_id"]): r for r in user_ratings}

    sc_pairs: list[dict] = []
    row = 5
    for f in functions:
        for m in MALFUNCTIONS:
            user = rating_index.get((f["id"], m["id"]))
            if user:
                cls = user.get("classification", "").upper() or "SC"
                subsumed = user.get("subsumed_by", "") or ""
                hazard = user.get("hazard_description") or user.get("rationale", "")
            else:
                cls, hazard = suggest_classification(f, m["id"])
                subsumed = ""

            ws.cell(row=row, column=1, value=f["id"])
            ws.cell(row=row, column=2, value=f["name"])
            ws.cell(row=row, column=3, value=m["id"])
            ws.cell(row=row, column=4, value=m["name"])
            cls_cell = ws.cell(row=row, column=5, value=cls)
            cls_cell.alignment = Alignment(horizontal="center", vertical="center")
            if cls == "SC":
                cls_cell.fill = PatternFill("solid", fgColor=RED_BAD)
                cls_cell.font = Font(name=FONT_NAME, bold=True, size=10)
            elif cls == "NSC":
                cls_cell.fill = PatternFill("solid", fgColor=GREEN_OK)
            else:
                cls_cell.fill = PatternFill("solid", fgColor=ALT_ROW)
            ws.cell(row=row, column=6, value=subsumed)
            ws.cell(row=row, column=7, value=hazard)
            row += 1

            if cls == "SC" and not subsumed:
                sc_pairs.append({
                    "function": f,
                    "malfunction": m,
                    "hazard_description": hazard or f"Hazard caused by {m['name'].lower()} of {f['name']}",
                })

    stripe_end = row - 1
    # Apply borders without overwriting our colored Classification cells
    for r in range(5, stripe_end + 1):
        for c in range(1, 8):
            cell = ws.cell(row=r, column=c)
            if c != 5:
                cell.font = body_font()
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = BORDER_ALL

    autosize(ws, {1: 10, 2: 22, 3: 12, 4: 22, 5: 14, 6: 14, 7: 50})
    return sc_pairs


def build_hara_worksheet(
    wb: Workbook,
    sc_pairs: list[dict],
    environments: dict,
    overrides: list[dict],
) -> list[dict]:
    """
    Build the main HARA cartesian. Returns the rows that received an ASIL ≥ A,
    used to populate the Safety Goals tab.
    """
    ws = wb.create_sheet("12_HARA_Worksheet")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 17, "HARA Worksheet — Cartesian (Function × Malfunction × Operating Environment)")

    headers = [
        "HARA ID", "Function ID", "Function", "Malfunction ID", "Malfunction",
        "Location Code", "Location", "Speed band (km/h)", "Weather Code", "Weather",
        "Hazardous Event", "S", "S Rationale", "E", "E Rationale", "C", "C Rationale", "ASIL", "Safe State",
    ]
    # Note: 19 headers; widen columns accordingly
    headers = [
        "HARA ID", "Function ID", "Function", "Malfunction ID", "Malfunction",
        "Location", "Speed band (km/h)", "Weather",
        "Hazardous Event",
        "S", "S Rationale", "E", "E Rationale", "C", "C Rationale",
        "ASIL", "Safe State",
    ]
    style_header_row(ws, 3, headers)

    locations = environments.get("locations") or DEFAULT_LOCATIONS
    weather_list = environments.get("weather") or DEFAULT_WEATHER

    # Index of overrides by (function_id, malfunction_id, location_code, weather_code)
    override_index = {
        (o["function_id"], o["malfunction_id"], o["location_code"], o["weather_code"]): o
        for o in overrides
    }

    asil_pos_lookup = {"QM": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    significant_rows: list[dict] = []

    row = 4
    counter = 1
    for pair in sc_pairs:
        f = pair["function"]
        m = pair["malfunction"]
        for loc in locations:
            for wx in weather_list:
                key = (f["id"], m["id"], loc["code"], wx["code"])
                ovr = override_index.get(key, {})

                speed_max = loc.get("speed_max_kph", 0)
                S, S_rat = (ovr.get("S"), ovr.get("S_rationale")) if ovr.get("S") else suggest_severity(speed_max, m["id"], f.get("kinetic_authority", "medium"), wx["code"])
                E, E_rat = (ovr.get("E"), ovr.get("E_rationale")) if ovr.get("E") else suggest_exposure(loc, wx)
                C, C_rat = (ovr.get("C"), ovr.get("C_rationale")) if ovr.get("C") else suggest_controllability(speed_max, m["id"])

                hara_id = f"H-{counter:04d}"
                hazardous_event = (
                    f"{m['name']} of '{f['name']}' while in {loc['name']} "
                    f"({loc.get('speed_min_kph', 0)}–{speed_max} km/h), {wx['name'].lower()}."
                )
                speed_band_text = f"{loc.get('speed_min_kph', 0)}–{speed_max}"

                ws.cell(row=row, column=1, value=hara_id)
                ws.cell(row=row, column=2, value=f["id"])
                ws.cell(row=row, column=3, value=f["name"])
                ws.cell(row=row, column=4, value=m["id"])
                ws.cell(row=row, column=5, value=m["name"])
                ws.cell(row=row, column=6, value=loc["name"])
                ws.cell(row=row, column=7, value=speed_band_text)
                ws.cell(row=row, column=8, value=wx["name"])
                ws.cell(row=row, column=9, value=hazardous_event)
                ws.cell(row=row, column=10, value=S)
                ws.cell(row=row, column=11, value=S_rat)
                ws.cell(row=row, column=12, value=E)
                ws.cell(row=row, column=13, value=E_rat)
                ws.cell(row=row, column=14, value=C)
                ws.cell(row=row, column=15, value=C_rat)

                # ASIL formula referencing 10_ASIL_Matrix
                # S col = J (10), E col = L (12), C col = N (14); ASIL col = P (16)
                asil_formula = (
                    f'=IF(J{row}="S0","—",'
                    f'IF(K{row}="","",'
                    f'IF(L{row}="E0","QM",'
                    f'INDEX(\'10_ASIL_Matrix\'!$D$4:$F$15,'
                    f'MATCH(J{row}&"-"&L{row},\'10_ASIL_Matrix\'!$C$4:$C$15,0),'
                    f'MATCH(N{row},\'10_ASIL_Matrix\'!$D$3:$F$3,0)))))'
                )
                ws.cell(row=row, column=16, value=asil_formula)

                # Safe state suggestion
                safe_state = pair.get("safe_state") or f.get("safe_state") or "Disable function output; transition to driver-controlled mode; illuminate warning lamp."
                ws.cell(row=row, column=17, value=safe_state)

                # Track for safety goals
                # We compute likely ASIL inline for selection; user can later override
                expected_asil = compute_asil_python(S, E, C)
                if asil_pos_lookup.get(expected_asil, 0) >= 1:  # ASIL A or higher
                    significant_rows.append({
                        "hara_id": hara_id,
                        "function": f,
                        "malfunction": m,
                        "location": loc,
                        "weather": wx,
                        "hazardous_event": hazardous_event,
                        "S": S, "E": E, "C": C, "ASIL": expected_asil,
                        "safe_state": safe_state,
                        "hazard_description": pair["hazard_description"],
                    })

                row += 1
                counter += 1

    end_row = row - 1
    stripe_body(ws, 4, end_row, len(headers))

    # Highlight ASIL column based on value (conditional formatting via fill)
    asil_color = {"A": "FFE699", "B": "F4B084", "C": "F8696B", "D": "C00000"}
    for r in range(4, end_row + 1):
        # Re-color ASIL via the precomputed value (formula renders later, but we can color the cell now)
        S_val = ws.cell(row=r, column=10).value
        E_val = ws.cell(row=r, column=12).value
        C_val = ws.cell(row=r, column=14).value
        asil_val = compute_asil_python(S_val, E_val, C_val)
        cell = ws.cell(row=r, column=16)
        if asil_val in asil_color:
            cell.fill = PatternFill("solid", fgColor=asil_color[asil_val])
            cell.font = Font(name=FONT_NAME, bold=True, color="FFFFFF" if asil_val == "D" else "000000")
            cell.alignment = Alignment(horizontal="center", vertical="center")

    # Freeze header
    ws.freeze_panes = "A4"

    # Auto-filter
    ws.auto_filter.ref = f"A3:{get_column_letter(len(headers))}{end_row}"

    autosize(ws, {
        1: 10, 2: 10, 3: 22, 4: 12, 5: 22,
        6: 20, 7: 14, 8: 16,
        9: 50,
        10: 6, 11: 50,
        12: 6, 13: 50,
        14: 6, 15: 50,
        16: 8, 17: 40,
    })

    return significant_rows


def compute_asil_python(S: str, E: str, C: str) -> str:
    """Mirror of the worksheet formula for use during generation (safety goals selection)."""
    if not S or not E or not C:
        return ""
    if S == "S0":
        return "—"
    if E == "E0":
        return "QM"
    for s, e, c1, c2, c3 in ASIL_MATRIX:
        if s == S and e == E:
            return {"C1": c1, "C2": c2, "C3": c3}.get(C, "QM")
    return "QM"


def build_safety_goals(wb: Workbook, significant_rows: list[dict]) -> list[dict]:
    """Aggregate ASIL ≥ A rows into safety goals. Returns the consolidated SG list."""
    ws = wb.create_sheet("13_Safety_Goals")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 7, "Safety Goals and Safe States")

    note = ws.cell(row=2, column=1,
                   value="Safety goal text format: 'Prevent <hazard description>'. One safety goal per unique "
                         "(function, hazard) pair; the highest ASIL across all environments governs.")
    note.font = Font(name=FONT_NAME, italic=True, size=10, color="9C5700")
    note.fill = PatternFill("solid", fgColor=WARN_YELLOW)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=7)
    ws.row_dimensions[2].height = 36

    style_header_row(ws, 4, ["SG_ID", "Function", "Hazard", "Worst-case ASIL", "Driving HARA IDs", "Safety Goal", "Safe State"])

    # Group by (function_id, hazard_description)
    groups: dict[tuple[str, str], dict] = {}
    asil_rank = {"QM": 0, "A": 1, "B": 2, "C": 3, "D": 4}
    for r in significant_rows:
        key = (r["function"]["id"], r["hazard_description"])
        existing = groups.get(key)
        if existing:
            if asil_rank[r["ASIL"]] > asil_rank[existing["ASIL"]]:
                existing["ASIL"] = r["ASIL"]
                existing["safe_state"] = r["safe_state"]
            existing["hara_ids"].append(r["hara_id"])
        else:
            groups[key] = {
                "function": r["function"],
                "hazard_description": r["hazard_description"],
                "ASIL": r["ASIL"],
                "safe_state": r["safe_state"],
                "hara_ids": [r["hara_id"]],
            }

    sgs: list[dict] = []
    row = 5
    for i, (key, g) in enumerate(sorted(groups.items(), key=lambda kv: -asil_rank[kv[1]["ASIL"]]), start=1):
        sg_id = f"SG-{i:03d}"
        sg_text = f"Prevent {g['hazard_description'].rstrip('.')}"
        sgs.append({**g, "sg_id": sg_id, "sg_text": sg_text})
        ws.cell(row=row, column=1, value=sg_id)
        ws.cell(row=row, column=2, value=g["function"]["name"])
        ws.cell(row=row, column=3, value=g["hazard_description"])
        ws.cell(row=row, column=4, value=g["ASIL"])
        ws.cell(row=row, column=5, value=", ".join(g["hara_ids"][:6]) + ("…" if len(g["hara_ids"]) > 6 else ""))
        ws.cell(row=row, column=6, value=sg_text)
        ws.cell(row=row, column=7, value=g["safe_state"])
        row += 1

    if row > 5:
        stripe_body(ws, 5, row - 1, 7)
        # Color ASIL column
        asil_color = {"A": "FFE699", "B": "F4B084", "C": "F8696B", "D": "C00000"}
        for r in range(5, row):
            v = ws.cell(row=r, column=4).value
            cell = ws.cell(row=r, column=4)
            if v in asil_color:
                cell.fill = PatternFill("solid", fgColor=asil_color[v])
                cell.font = Font(name=FONT_NAME, bold=True, color="FFFFFF" if v == "D" else "000000")
                cell.alignment = Alignment(horizontal="center", vertical="center")
    else:
        ws.cell(row=5, column=1, value="(No ASIL ≥ A safety goals were derived. Review HARA worksheet.)").font = body_font()

    autosize(ws, {1: 10, 2: 24, 3: 50, 4: 14, 5: 28, 6: 60, 7: 40})
    return sgs


def build_fsc_handoff(wb: Workbook, sgs: list[dict]) -> None:
    ws = wb.create_sheet("14_FSC_Handoff")
    ws.sheet_view.showGridLines = False
    style_title_row(ws, 1, 11, "Functional Safety Concept (FSC) — Hand-off")

    note = ws.cell(row=2, column=1,
                   value="Pre-populated from Safety Goals. Analyst fills FTTI, allocation, warning strategy, "
                         "FSRs, verification methods, and any ASIL decomposition decisions during the FSC phase.")
    note.font = Font(name=FONT_NAME, italic=True, size=10, color="9C5700")
    note.fill = PatternFill("solid", fgColor=WARN_YELLOW)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=11)
    ws.row_dimensions[2].height = 36

    headers = [
        "SG_ID", "Safety Goal", "ASIL", "Safe State",
        "FTTI (ms)", "Allocation Target", "Warning & Degradation",
        "FSR ID", "Functional Safety Requirement", "Verification Method", "Decomposition",
    ]
    style_header_row(ws, 4, headers)

    row = 5
    for sg in sgs:
        # Pre-populate 3 FSR placeholder rows per safety goal
        for j in range(1, 4):
            fsr_id = f"FSR-{sg['sg_id'].split('-')[1]}-{j:02d}"
            ws.cell(row=row, column=1, value=sg["sg_id"] if j == 1 else "")
            ws.cell(row=row, column=2, value=sg["sg_text"] if j == 1 else "")
            ws.cell(row=row, column=3, value=sg["ASIL"] if j == 1 else "")
            ws.cell(row=row, column=4, value=sg["safe_state"] if j == 1 else "")
            ws.cell(row=row, column=5, value="" if j > 1 else "")
            ws.cell(row=row, column=6, value="")
            ws.cell(row=row, column=7, value="")
            ws.cell(row=row, column=8, value=fsr_id)
            ws.cell(row=row, column=9, value="(Derived requirement — to be authored in FSC phase.)")
            ws.cell(row=row, column=10, value="Test")
            ws.cell(row=row, column=11, value="None")
            row += 1

    if row > 5:
        stripe_body(ws, 5, row - 1, len(headers))

    autosize(ws, {1: 10, 2: 50, 3: 8, 4: 32, 5: 12, 6: 22, 7: 36, 8: 14, 9: 50, 10: 16, 11: 22})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _coerce_env_items(items: Any, prefix: str, is_location: bool) -> list[dict]:
    """把 LLM 给出的 locations/weather 规范化为下游要求的 dict 列表。

    - 字符串项（LLM 偶尔输出 ["Parking lot", ...]）→ {"code", "name"} 自动补码
    - dict 项缺 code/name → 补；速度字段强制为数值
    """
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for i, it in enumerate(items, start=1):
        code = f"{prefix}{i:02d}"
        if isinstance(it, str):
            if not it.strip():
                continue
            entry = {"code": code, "name": it.strip()}
            if is_location:
                entry.update({"speed_min_kph": 0, "speed_max_kph": 0, "default_e": "E3"})
            else:
                entry["default_e"] = "E3"
            out.append(entry)
        elif isinstance(it, dict):
            name = str(it.get("name") or it.get("location") or it.get("weather") or code)
            entry = dict(it)
            entry["code"] = str(it.get("code") or code)
            entry["name"] = name
            if is_location:
                try:
                    entry["speed_min_kph"] = int(float(it.get("speed_min_kph") or 0))
                except (TypeError, ValueError):
                    entry["speed_min_kph"] = 0
                try:
                    entry["speed_max_kph"] = int(float(it.get("speed_max_kph") or 0))
                except (TypeError, ValueError):
                    entry["speed_max_kph"] = 0
            out.append(entry)
    return out


def _coerce_inputs(data: Any) -> dict:
    """对 LLM 生成的输入 JSON 做容错规范化（渲染器不得因模型输出的小形态偏差崩溃）。

    - functions：丢弃无 id 的非对象项；补 name；kinetic_authority 归一到枚举
    - function_malfunction_ratings：只保留 function_id/malfunction_id 齐全的对象
    - rating_overrides：只保留四键齐全的对象
    - environments.locations/weather：字符串数组 → 标准对象数组
    - assumptions/interfaces：过滤非对象并补 id
    """
    if not isinstance(data, dict):
        raise ValueError("Input JSON top-level must be an object.")

    valid_auth = {"high", "medium", "low", "none"}
    raw_fns = data.get("functions")
    functions: list[dict] = []
    if isinstance(raw_fns, list):
        for f in raw_fns:
            if not isinstance(f, dict) or not str(f.get("id") or "").strip():
                continue
            fn = dict(f)
            fn["id"] = str(fn["id"]).strip()
            fn["name"] = str(fn.get("name") or fn["id"])
            auth = str(fn.get("kinetic_authority") or "medium").strip().lower()
            fn["kinetic_authority"] = auth if auth in valid_auth else "medium"
            functions.append(fn)
    data["functions"] = functions

    raw_ratings = data.get("function_malfunction_ratings")
    if isinstance(raw_ratings, list):
        ratings = [
            r for r in raw_ratings
            if isinstance(r, dict)
            and str(r.get("function_id") or "").strip()
            and str(r.get("malfunction_id") or "").strip()
        ]
        data["function_malfunction_ratings"] = ratings
    else:
        data["function_malfunction_ratings"] = []

    raw_overrides = data.get("rating_overrides")
    if isinstance(raw_overrides, list):
        data["rating_overrides"] = [
            o for o in raw_overrides
            if isinstance(o, dict)
            and all(str(o.get(k) or "").strip() for k in
                    ("function_id", "malfunction_id", "location_code", "weather_code"))
        ]
    else:
        data["rating_overrides"] = []

    env = data.get("environments")
    if not isinstance(env, dict):
        env = {}
    env["locations"] = _coerce_env_items(env.get("locations"), "LU", True)
    env["weather"] = _coerce_env_items(env.get("weather"), "WU", False)
    data["environments"] = env

    for key, id_prefix in (("assumptions", "A"), ("interfaces", "I")):
        raw = data.get(key)
        if isinstance(raw, list):
            items = [x for x in raw if isinstance(x, dict)]
            for i, x in enumerate(items, start=1):
                if not str(x.get("id") or "").strip():
                    x["id"] = f"{id_prefix}{i:02d}"
            data[key] = items
        else:
            data[key] = []

    return data


def generate(input_path: str, output_path: str) -> dict:
    # 平台以 UTF-8 写输入 JSON；必须显式指定编码，否则在 Windows GBK(ACP=936)
    # 且未启用 PYTHONUTF8 的服务进程中会按 GBK 读取中文而 UnicodeDecodeError。
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)
    data = _coerce_inputs(data)

    item = data.get("item", {})
    functions = data.get("functions", [])
    if not functions:
        raise ValueError("Input must include at least one valid function (with non-empty 'id') under 'functions'.")
    environments = data.get("environments", {}) or {}
    user_ratings = data.get("function_malfunction_ratings", []) or []
    overrides = data.get("rating_overrides", []) or []
    assumptions = data.get("assumptions", []) or []
    interfaces = data.get("interfaces", []) or []

    wb = Workbook()
    # Remove default sheet
    wb.remove(wb.active)

    build_title_page(wb, item)
    build_doc_control(wb)
    build_assumptions(wb, assumptions)
    build_architecture_boundary(wb, data.get("scope_description", ""), data.get("boundary_diagram_notes", ""), interfaces)
    build_functions(wb, functions)
    build_malfunctions(wb)
    build_operating_environment(wb, environments)
    build_severity_reference(wb)
    build_exposure_reference(wb)
    build_controllability_reference(wb)
    build_asil_matrix(wb)

    sc_pairs = build_function_malfunction_filter(wb, functions, user_ratings)
    significant_rows = build_hara_worksheet(wb, sc_pairs, environments, overrides)
    sgs = build_safety_goals(wb, significant_rows)
    build_fsc_handoff(wb, sgs)

    # Set tab order is preserved by creation order. Make Title the active sheet.
    wb.active = 0

    wb.save(output_path)

    return {
        "functions": len(functions),
        "function_malfunction_pairs": len(functions) * len(MALFUNCTIONS),
        "safety_critical_pairs": len(sc_pairs),
        "hara_rows": sum(1 for _ in sc_pairs) * len(environments.get("locations") or DEFAULT_LOCATIONS) * len(environments.get("weather") or DEFAULT_WEATHER),
        "significant_rows": len(significant_rows),
        "safety_goals": len(sgs),
        "output_path": output_path,
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python generate_hara.py <input.json> <output.xlsx>", file=sys.stderr)
        sys.exit(2)
    summary = generate(sys.argv[1], sys.argv[2])
    print(json.dumps(summary, indent=2))
