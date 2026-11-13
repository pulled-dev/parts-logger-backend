"""
Part-name classifier: raw BreakerPro part names -> canonical part type +
position, using analytics/part_types.yaml.

  1. Rules (regex/keyword, first match wins) on a normalised name.
  2. Names no rule matched go to the Claude API in batches of 100 distinct
     names (structured JSON output, part_type constrained to the taxonomy).
     Answers are cached in the analytics DB, so a name is never sent twice.
  3. Anything with confidence < REVIEW_BELOW is listed for human review.

Only part names are sent to the API (no IDs, prices or customer data).
"""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_PATH = ROOT / "analytics" / "part_types.yaml"

LLM_MODEL = "claude-haiku-5-5"
LLM_BATCH = 100
REVIEW_BELOW = 0.7
RULE_CONFIDENCE = 0.95
# USD per million tokens, used only to report the run's cost. Set to Haiku
# list prices ($1 in / $5 out, cache reads $0.10); check them against the
# current price list for LLM_MODEL.
PRICE_IN, PRICE_OUT, PRICE_CACHE_READ = 1.00, 5.00, 0.10
# The API key lives in this file (one line). It is read here and passed to the
# client; it is never printed or logged.
API_KEY_FILE = Path.home() / ".secrets" / "anthropic" / "api_key.txt"

# Normalisation applied before rules run (order matters).
_SUBS = [
    (r"\bA[\s.\-]*BAGS?\b|\bABAGS?\b|\bAIR[\s\-]*BAGS?\b", "AIRBAG"),
    (r"\bSEAT[\s\-]*BELTS?\b", "SEATBELT"),
    (r"\bHEAD[\s\-]*LAMPS?\b|\bHEAD[\s\-]*LIGHTS?\b", "HEADLIGHT"),
    (r"\bTAIL[\s\-]*LAMPS?\b", "TAIL LIGHT"),
    (r"\bHAND[\s\-]*BR(?:AKE|EAK)\b", "HANDBRAKE"),
    (r"\bBOOT[\s\-]*LID\b", "BOOTLID"),
    (r"\bAIR[\s\-]*CON\b", "AIR CON"),
]

# Position vocabulary. UK cars: driver = offside (right), passenger = nearside (left).
_SIDE_CODES = {  # compact codes, after punctuation is removed: O/S/F -> OSF
    "OSF": ("front", "driver"), "OSR": ("rear", "driver"), "NSF": ("front", "passenger"),
    "NSR": ("rear", "passenger"), "OS": (None, "driver"), "NS": (None, "passenger"),
    "RH": (None, "driver"), "LH": (None, "passenger"), "RHS": (None, "driver"), "LHS": (None, "passenger"),
}


def load_taxonomy(path: Path = TAXONOMY_PATH) -> list[dict]:
    types = yaml.safe_load(path.read_text(encoding="utf-8"))["types"]
    ids = [t["id"] for t in types]
    assert len(ids) == len(set(ids)), "duplicate part type id in part_types.yaml"
    for t in types:
        t["_rx"] = [re.compile(p) for p in t.get("patterns") or []]
        t.setdefault("confidence", RULE_CONFIDENCE)
    return types


def normalise(name: str) -> str:
    n = (name or "").upper()
    for pat, rep in _SUBS:
        n = re.sub(pat, rep, n)
    n = re.sub(r"[^A-Z0-9 ]+", " ", n.replace("/", " "))
    return re.sub(r"\s+", " ", n).strip()


def position(name: str) -> str:
    """'(FRONT DRIVER SIDE)' -> 'front driver'; 'N/S/R' -> 'rear passenger'; 'PAIR' -> 'pair'."""
    raw = (name or "").upper()
    compact = re.sub(r"[^A-Z0-9 ]", "", raw.replace("/", ""))  # O/S/F -> OSF
    words = set(re.sub(r"[^A-Z0-9]+", " ", raw).split())
    fr = side = None
    for w in compact.split():
        if w in _SIDE_CODES:
            f, s = _SIDE_CODES[w]
            fr, side = fr or f, side or s
    if "FRONT" in words:
        fr = fr or "front"
    if "REAR" in words and not re.search(r"REAR ?VIEW", raw):
        fr = fr or "rear"
    if words & {"DRIVER", "DRIVERS", "RIGHT"}:
        side = side or "driver"
    if words & {"PASSENGER", "PASSENGERS", "LEFT"}:
        side = side or "passenger"
    parts = [p for p in (fr, side) if p]
    if words & {"PAIR", "PAIRS", "SET"}:
        parts.append("pair")
    return " ".join(parts)


def classify_rules(name: str, types: list[dict]):
    """(part_type, confidence) from the first matching rule, or None."""
    n = normalise(name)
    for t in types:
        if any(rx.search(n) for rx in t["_rx"]):
            return t["id"], float(t["confidence"])
    return None


# --------------------------------------------------------------------------
# Claude API step
# --------------------------------------------------------------------------

def _schema(type_ids: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "raw_name": {"type": "string"},
                        "part_type": {"type": "string", "enum": type_ids},
                        "position": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["raw_name", "part_type", "position", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def _system_prompt(types: list[dict]) -> str:
    lines = "\n".join(f"- {t['id']}: {t['label']}" for t in types)
    return (
        "You classify part names written by staff at a UK car breaker that dismantles "
        "VW-group cars (VW, Audi, SEAT, Skoda). Names are short, upper case, often "
        "misspelt or abbreviated (e.g. 'A BAG' = airbag, 'N/S/F' = nearside front = "
        "front passenger, 'O/S' = offside = driver side, 'MECH' = mechanism).\n\n"
        "For each name return:\n"
        "- part_type: exactly one id from the list below. Use misc_unknown if none fits; "
        "never invent ids. Side/position is never a reason to pick a different type.\n"
        "- position: lower-case words from front/rear, driver/passenger, pair "
        "(e.g. 'front driver', 'rear', 'pair'), or empty string if none is stated.\n"
        "- confidence: 0 to 1, how sure you are of part_type. Use below 0.7 when the "
        "name is ambiguous, too vague, or not clearly a car part.\n"
        "- raw_name: the name exactly as given.\n\n"
        f"Part types:\n{lines}"
    )


def classify_llm(names: list[str], types: list[dict], log=print):
    """Classify names with Claude. Returns (results, usage) where results maps
    raw_name -> (part_type, position, confidence) and usage totals tokens/cost.
    Needs the key in API_KEY_FILE (see llm_available)."""
    import anthropic

    client = anthropic.Anthropic(api_key=_read_key())
    type_ids = [t["id"] for t in types]
    system = _system_prompt(types)
    schema = _schema(type_ids)
    results, usage = {}, {"batches": 0, "input": 0, "output": 0, "cache_read": 0,
                          "cache_write": 0, "refused": 0, "failed": 0}
    for i in range(0, len(names), LLM_BATCH):
        batch = names[i:i + LLM_BATCH]
        try:
            resp = client.messages.create(
                model=LLM_MODEL,
                max_tokens=16000,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": "Classify these part names:\n" + json.dumps(batch)}],
            )
        except anthropic.APIStatusError as e:
            log(f"  batch {i // LLM_BATCH + 1}: API error {e.status_code}: {e.message}")
            usage["failed"] += len(batch)
            continue
        except anthropic.APIConnectionError as e:
            log(f"  batch {i // LLM_BATCH + 1}: connection error: {e}")
            usage["failed"] += len(batch)
            continue
        usage["batches"] += 1
        u = resp.usage
        usage["input"] += u.input_tokens or 0
        usage["output"] += u.output_tokens or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        if resp.stop_reason in ("refusal", "max_tokens"):
            log(f"  batch {i // LLM_BATCH + 1}: stopped ({resp.stop_reason}); names left unclassified")
            usage["refused" if resp.stop_reason == "refusal" else "failed"] += len(batch)
            continue
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            items = json.loads(text)["items"]
        except (json.JSONDecodeError, KeyError) as e:
            log(f"  batch {i // LLM_BATCH + 1}: unreadable output ({e}); stop_reason={resp.stop_reason}")
            usage["failed"] += len(batch)
            continue
        wanted = set(batch)
        for it in items:
            if it["raw_name"] in wanted and it["part_type"] in type_ids:
                conf = max(0.0, min(1.0, float(it["confidence"])))
                results[it["raw_name"]] = (it["part_type"], it["position"].strip().lower(), conf)
        missing = wanted - set(results)
        if missing:
            log(f"  batch {i // LLM_BATCH + 1}: {len(missing)} names not returned; left unclassified")
        log(f"  batch {i // LLM_BATCH + 1}/{(len(names) + LLM_BATCH - 1) // LLM_BATCH}: "
            f"{len(batch) - len(missing)} classified")
    usage["cost_usd"] = (usage["input"] * PRICE_IN + usage["output"] * PRICE_OUT
                         + usage["cache_read"] * PRICE_CACHE_READ
                         + usage["cache_write"] * PRICE_IN * 1.25) / 1_000_000
    return results, usage


def _read_key() -> str:
    return API_KEY_FILE.read_text(encoding="utf-8-sig").strip()


def llm_available() -> tuple[bool, str]:
    """(True, "") when the key file exists and is not empty, else (False, reason).
    The reason never contains the key."""
    if not API_KEY_FILE.is_file():
        return False, f"key file not found: {API_KEY_FILE}"
    try:
        if not _read_key():
            return False, f"key file is empty: {API_KEY_FILE}"
    except OSError as e:
        return False, f"key file not readable: {API_KEY_FILE} ({e.strerror})"
    return True, ""
