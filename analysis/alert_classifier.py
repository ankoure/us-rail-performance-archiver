"""Classify alerts in a daily snapshot, after TransitMatters' delays/process.py.

Three-step pipeline applied to each alert dict (as produced by
analysis.alert_snapshot):

1. is_delay — prefer the GTFS-RT Effect enum (SIGNIFICANT_DELAYS / REDUCED_SERVICE
   count as delay; everything else doesn't). Fall back to TM text patterns when
   the effect is unset, UNKNOWN_EFFECT, or OTHER_EFFECT.

2. type — prefer the Cause enum mapped via CAUSE_TO_TYPE. Fall back to first-match
   keyword search across ALERT_PATTERNS when the cause is UNKNOWN_CAUSE. Returns
   "other" when both signals are absent.

3. delay_minutes — TM-port regex over the alert text. MBTA-prose-shaped, so most
   non-MBTA agencies return None. Honest, not broken.

Pure module: no I/O, no DataFrames.
"""

from __future__ import annotations

import re


# Verbatim port from transitmatters/data-ingestion/.../constants.py — keyed by
# TM's delay-type labels so cross-checking against their output is straightforward.
ALERT_PATTERNS: dict[str, list[str]] = {
    "disabled_vehicle": [
        "disabled train",
        "disabled trolley",
        "train that was disabled",
        "disabled bus",
        "train being taken out of service",
        "train being removed from service",
    ],
    "signal_problem": [
        "signal problem",
        "signal issue",
        "signal repairs",
        "signal maintenance",
        "signal repair",
        "signal work",
        "signal department",
    ],
    "switch_problem": [
        "switch problem",
        "switch issue",
        "witch problem",
        "witch issue",
        "switching issue",
    ],
    "brake_problem": [
        "brake issue",
        "brake problem",
        "brakes activated",
        "brakes holding",
        "brakes applied",
    ],
    "power_problem": [
        "power problem",
        "power issue",
        "overhead wires",
        "overhead wire",
        "overhear wires",
        "overheard wires",
        "catenary wires",
        "the overhead",
        "wire repair",
        "repairs to the wire",
        "wire maintenance",
        "wire inspection",
        "wire problem",
        "electrical problem",
        "overhead catenary",
        "third rail wiring",
        "power department work",
    ],
    "door_problem": [
        "door problem",
        "door issue",
    ],
    "track_issue": [
        "track issue",
        "track problem",
        "cracked rail",
        "broken rail",
    ],
    "medical_emergency": [
        "medical emergency",
        "ill passenger",
        "medical assistance",
        "medical attention",
        "sick passenger",
    ],
    "flooding": [
        "flooding",
    ],
    "police_activity": [
        "police",
    ],
    "fire": [
        "fire",
        "smoke",
        "burning",
    ],
    "mechanical_problem": [
        "mechanical problem",
        "mechanical issue",
        "motor problem",
        "pantograph problem",
        "pantograph issue",
        "issue with the heating system",
        "air pressure problem",
    ],
    "track_work": [
        "track work",
        "track maintenance",
        "overnight work",
        "track repair",
        "personnel performed maintenance",
        "maintenance work",
        "overnight maintenance",
        "single track",
    ],
    "car_traffic": [
        "unauthorized vehicle on the tracks",
        "vehicle blocking the tracks",
        "auto accident",
        "car on the tracks",
        "car blocking the tracks",
        "car accident",
        "automobile accident",
        "disabled vehicle on the tracks",
        "due to traffic",
        "car in the track area",
        "car blocking the track area",
        "auto that was blocking",
        "auto blocking the track",
        "auto was removed from the track",
        "accident blocking the tracks",
    ],
}

# GTFS-RT Cause enum names the text patterns above don't cover — added so a
# cause-set alert can land in a meaningful bucket without text matching.
_EXTRA_TYPES = ("weather", "strike", "demonstration")

# All type labels, with 0 counts — copy this when aggregating.
DELAY_BY_TYPE: dict[str, int] = {
    label: 0 for label in (*ALERT_PATTERNS.keys(), *_EXTRA_TYPES, "other")
}

# GTFS-RT Alert.Cause enum NAME (as MessageToDict produces it) -> DELAY_BY_TYPE key.
# UNKNOWN_CAUSE is intentionally omitted so it triggers the text fallback.
CAUSE_TO_TYPE: dict[str, str] = {
    "TECHNICAL_PROBLEM": "mechanical_problem",
    "ACCIDENT": "car_traffic",
    "MAINTENANCE": "track_work",
    "CONSTRUCTION": "track_work",
    "POLICE_ACTIVITY": "police_activity",
    "MEDICAL_EMERGENCY": "medical_emergency",
    "WEATHER": "weather",
    "STRIKE": "strike",
    "DEMONSTRATION": "demonstration",
    "HOLIDAY": "other",
    "OTHER_CAUSE": "other",
}

_DELAY_EFFECTS = frozenset({"SIGNIFICANT_DELAYS", "REDUCED_SERVICE"})
_AMBIGUOUS_EFFECTS = frozenset({"UNKNOWN_EFFECT", "OTHER_EFFECT"})

_MINUTE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"delays of about \d+ minutes",
        r"delays of up to \d+ minutes",
        r"\d+ minutes late",
        r"\d+ minutes behind schedule",
        r"\d+\s*-\s*\d+ minutes behind schedule",
        r"\d+\s*-\s*\d+ minutes late",
    )
)


def alert_text(alert: dict, language: str = "en") -> str:
    """Header + description concatenated, picking the requested language.

    Falls back to a language variant (e.g. en-US when en is requested), then to
    the first available translation. Returns "" if no text at all.
    """
    header = _translation(alert.get("header_text"), language)
    desc = _translation(alert.get("description_text"), language)
    parts = [p for p in (header, desc) if p]
    return ". ".join(parts)


def _translation(ts: dict | None, language: str) -> str:
    if not ts:
        return ""
    translations = ts.get("translation") or []
    for t in translations:
        if t.get("language") == language:
            return t.get("text", "")
    for t in translations:
        lang = t.get("language", "")
        if lang.startswith(f"{language}-") or lang.startswith(f"{language}_"):
            return t.get("text", "")
    return translations[0].get("text", "") if translations else ""


def alert_is_delay(alert: dict) -> bool:
    """Whether the alert describes a delay (step 1 of the pipeline).

    Trusts the GTFS-RT Effect enum when it's set and unambiguous; only when the
    effect is absent, UNKNOWN_EFFECT, or OTHER_EFFECT does it fall back to TM's
    text heuristics over the alert prose.
    """
    effect = alert.get("effect")
    if effect in _DELAY_EFFECTS:
        return True
    if effect and effect not in _AMBIGUOUS_EFFECTS:
        return False
    text = alert_text(alert).lower()
    return (
        ("delays" in text and "minutes" in text)
        or "minutes late" in text
        or "minutes behind schedule" in text
        or "behind schedule" in text
    )


def alert_type(alert: dict) -> tuple[str, str]:
    """Returns (type_label, source) where source is "cause" | "text" | "default"."""
    cause = alert.get("cause")
    if cause and cause != "UNKNOWN_CAUSE":
        return CAUSE_TO_TYPE.get(cause, "other"), "cause"
    text_lower = alert_text(alert).lower()
    if text_lower:
        for label, patterns in ALERT_PATTERNS.items():
            for pattern in patterns:
                if pattern in text_lower:
                    return label, "text"
    return "other", "default"


def extract_delay_minutes(alert: dict) -> int | None:
    """Highest number of minutes mentioned by any TM-port regex, or None."""
    text = alert_text(alert).lower()
    best: int | None = None
    for pat in _MINUTE_PATTERNS:
        for match in pat.findall(text):
            nums = [int(n) for n in re.findall(r"\d+", match)]
            if nums:
                top = max(nums)
                best = top if best is None else max(best, top)
    return best


def classify_alert(alert: dict) -> dict:
    """Run the full pipeline on one alert → is_delay / type / delay_minutes.

    delay_minutes is only attempted when the alert is classified as a delay;
    non-delay alerts get None.
    """
    type_label, type_source = alert_type(alert)
    is_delay = alert_is_delay(alert)
    delay_minutes = extract_delay_minutes(alert) if is_delay else None
    return {
        "is_delay": is_delay,
        "type": type_label,
        "type_source": type_source,
        "delay_minutes": delay_minutes,
    }


def summarize_snapshot(snapshot: dict) -> dict:
    """Aggregate classifications across every alert in a daily snapshot."""
    delay_by_type = {label: 0 for label in DELAY_BY_TYPE}
    count_by_type = {label: 0 for label in DELAY_BY_TYPE}
    total_delay_minutes = 0
    delay_alert_count = 0

    alerts = snapshot.get("alerts", {})
    for body in alerts.values():
        cls = classify_alert(body["alert"])
        count_by_type[cls["type"]] += 1
        if cls["is_delay"]:
            delay_alert_count += 1
            if cls["delay_minutes"] is not None:
                total_delay_minutes += cls["delay_minutes"]
                delay_by_type[cls["type"]] += cls["delay_minutes"]

    return {
        "feed": snapshot.get("feed"),
        "service_date": snapshot.get("service_date"),
        "alert_count": len(alerts),
        "delay_alert_count": delay_alert_count,
        "total_delay_minutes": total_delay_minutes,
        "delay_by_type": delay_by_type,
        "count_by_type": count_by_type,
    }
