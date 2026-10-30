"""Evidence-based dealbreakers. Unknown facts remain visible, never invented."""

import re
import math

ALIASES = {"bangalore": "bengaluru", "bombay": "mumbai", "gurgaon": "gurugram"}


def normalized(text):
    text = str(text or "").lower()
    for old, new in ALIASES.items():
        text = text.replace(old, new)
    return text


def location_ok(location, allowed):
    if not allowed or not location:
        return True
    loc = normalized(location)
    for place in allowed:
        place = normalized(place).split(",")[0].strip()
        if "anywhere" in place and "remote" not in place:
            return True
        if "remote" in place:
            if "remote" in loc and not any(x in loc for x in (
                    "united states", "usa", "us only", "united kingdom", "uk only", "europe only")):
                if "india" in place and "india" not in loc and loc.strip() not in ("remote", "remote worldwide", "worldwide remote"):
                    continue
                return True
        elif place and place in loc:
            return True
    return False


def salary_range(job):
    """INR annual lakhs only. Foreign currency and ambiguous numbers stay unknown."""
    lo, hi = job.get("salary_min_lpa"), job.get("salary_max_lpa")
    try:
        if hi is not None:
            low, high = float(hi if lo is None else lo), float(hi)
            if all(math.isfinite(x) and x >= 0 for x in (low, high)) and low <= high:
                return low, high
    except (TypeError, ValueError):
        pass
    text = str(job.get("salary_text") or "") + " " + str(job.get("description") or "")
    match = re.search(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*(?:[-–]\s*(\d{1,3}(?:\.\d+)?))?\s*(?:lpa|(?:lakhs?|lacs?)\s*(?:per annum|p\.?a\.?|annually))\b", text, re.IGNORECASE)
    if match:
        low, high = float(match[1]), float(match[2] or match[1])
        if 0 <= low <= high:
            return low, high
    return None, None


def assess(job, profile):
    hf = profile.get("hard_filters") or {}
    blockers, questions = [], []
    title = normalized(job.get("title"))
    description = normalized(job.get("description"))
    blob = title + " " + description
    ceiling = hf.get("max_years_required")
    if ceiling is not None:
        from core.prefilter import _years_required
        required = _years_required(blob)
        if required is not None and required > int(ceiling):
            blockers.append(f"Requires {required}+ years; above your experience range")
    # Explicit title restrictions beat inconsistent aggregator location fields.
    title_location = re.search(
        r"\b(remote\s*[-–:]?\s*(?:us|usa|uk|europe)|united states|singapore|london)\b",
        title, re.IGNORECASE)
    stated_location = title_location.group(0) if title_location else job.get("location")
    if not location_ok(stated_location, hf.get("locations") or []):
        blockers.append("Location outside your search")
    elif not stated_location:
        questions.append("Confirm work location")
    if hf.get("no_sales"):
        sales_title = re.search(r"\b(sales|business development|relationship manager|account executive|wealth manager|acquisition manager)\b", title)
        sales_blob = re.sub(r"\b(?:no|without)\s+(?:sales targets?|sales quota|revenue targets?|upselling|cross.selling)\b", "", blob)
        quota = re.search(r"\b(sales targets?|sales quota|revenue targets?|quota.carrying|upsell(?:ing)?|cross.sell(?:ing)?|new business acquisition)\b", sales_blob)
        if sales_title or quota:
            blockers.append("Sales or revenue ownership conflicts with your preference")
    if hf.get("no_heavy_travel") and re.search(r"(?:extensive|frequent|heavy)\s+(?:business\s+)?travel|travel\s*(?:up to\s*)?(?:[3-9]\d|100)\s*%", blob):
        blockers.append("Frequent travel required")
    if hf.get("no_night_shifts") and re.search(r"night shift|us shift|north america shift|6:30 pm to 3:30 am", blob):
        blockers.append("Night shift conflicts with your preference")
    elif re.search(r"night shift|us shift|rotational shifts|24.?7|late evening", blob):
        questions.append("Confirm shift hours and work-life balance")
    minimum, maximum = salary_range(job)
    floor = hf.get("min_ctc_lpa")
    if maximum is None:
        questions.append("Salary undisclosed; confirm the expected range")
        salary = "Salary unverified"
    else:
        salary = f"₹{minimum:g}–{maximum:g} LPA" if minimum != maximum else f"₹{maximum:g} LPA"
        if floor and maximum < float(floor):
            blockers.append(f"Published salary below ₹{float(floor):g} LPA")
        elif floor and minimum < float(floor):
            questions.append("Only the upper salary range meets your floor")
    if len(description) < 350:
        questions.append("Limited job description; verify responsibilities")
    return {"blockers": blockers, "questions": questions, "salary": salary,
            "description_complete": len(description) >= 350,
            "eligible": not blockers}
