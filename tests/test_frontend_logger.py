"""Guards for the Parts Logger frontend (index.html).

The CSV export layout must be byte-for-byte identical to main, so we run main's
export code and the branch's export code through Node on the same synthetic
parts and compare the bytes. Vehicle-ref persistence and the non-blocking
pricing call are checked against the source.
"""

import json
import os
import re
import shutil
import subprocess

from conftest import fixture_path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
NODE = shutil.which("node")


def _src() -> str:
    with open(INDEX, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def _export_section(text: str) -> str:
    a = text.index("// ── Export CSV")
    b = text.index("// ── Settings panel")
    return text[a:b]


def _golden_section() -> str:
    with open(fixture_path("index_main_export_section.js"), encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


PARTS = [
    # non-painted, code-like part number -> 3-3-3 spacing
    {"id": 1, "part_number": "5Q0959802A", "description": "Window Motor OSF", "vehicle_ref": "VEH101",
     "your_price": 32.99, "part_note": "", "engine": {"state": "ok", "confidence": "high"}},
    # non-painted with a free-text note containing a comma and quotes
    {"id": 2, "part_number": "1K0959653D", "description": 'Slip Ring, "with" squib', "vehicle_ref": "VEH101",
     "your_price": None, "part_note": "see notes, tested"},
    # painted body panel (part_number blank, paint code column used)
    {"id": 3, "part_number": "", "description": "OSF Door", "vehicle_ref": "VEH101", "paint": True,
     "paint_code": "LC9X", "paint_name": "Deep Black", "your_price": 120, "part_note": "dent"},
    # second vehicle, painted without a name
    {"id": 4, "part_number": "", "description": "Bonnet", "vehicle_ref": "VEH202", "paint": True,
     "paint_code": "LA7W", "paint_name": "", "your_price": 85.5, "part_note": ""},
    {"id": 5, "part_number": "N/A", "description": "Engine Diesel", "vehicle_ref": "", "your_price": 299.99,
     "part_note": "CAYC"},
]

HARNESS = r"""
let captured = null;
let parts = __PARTS__;
const toast = () => {};
const render = () => {};
const updateStats = () => {};
const localStorage = { removeItem() {}, setItem() {}, getItem() { return null; } };
const URL = { createObjectURL(b) { captured = b; return 'blob:x'; }, revokeObjectURL() {} };
const document = { createElement() { return { click() {} }; } };
let handler = null;
const exportBtn = { addEventListener(_e, fn) { handler = fn; } };
__SECTION__
handler();
captured.arrayBuffer().then(buf => {
  process.stdout.write(Buffer.from(buf).toString('base64'));
});
"""


def _run_export(section: str, tmp_path) -> bytes:
    import base64
    js = HARNESS.replace("__PARTS__", json.dumps(PARTS)).replace("__SECTION__", section)
    script = tmp_path / "export_harness.js"
    script.write_text(js, encoding="utf-8")
    out = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return base64.b64decode(out.stdout)


def test_export_section_text_unchanged():
    assert _export_section(_src()) == _golden_section()


def test_csv_export_bytes_identical_to_main(tmp_path):
    assert NODE, "Node.js is required to run the CSV export byte-for-byte check"
    golden = _run_export(_golden_section(), tmp_path)
    current = _run_export(_export_section(_src()), tmp_path)
    assert current == golden
    text = current.decode("utf-8")
    assert text.startswith("﻿Part Number,Paint Code,Part Description,Price,Ref\r\n")
    for line in text.lstrip("﻿").split("\r\n"):
        # 5 columns on every row (quoted commas don't count)
        cells = re.findall(r'("([^"]|"")*"|[^,]*)(,|$)', line)
        assert len([c for c in cells if c[2] == ","]) == 4, line


def test_recording_does_not_live_in_export_handler():
    """Phase 6 final-price recording must stay outside the export block."""
    assert "/price/suggestions" not in _export_section(_src())


# ── Vehicle ref persistence (batch working style) ──────────────────────

def _add_part_body(text: str) -> str:
    a = text.index("async function addPart()")
    b = text.index("pnInput.addEventListener('keydown'", a)
    return text[a:b]


def test_vehicle_ref_not_cleared_when_adding():
    body = _add_part_body(_src())
    assert "refInput.value =" not in body and "refInput.value=" not in body
    assert "pnInput.value = '';" in body


def test_vehicle_ref_saved_and_restored():
    s = _src()
    assert "localStorage.setItem('pulledapart_last_ref', refInput.value)" in s
    assert "const savedRef = localStorage.getItem('pulledapart_last_ref');" in s
    assert "if (savedRef) refInput.value = savedRef;" in s


# ── Pricing call behaviour ─────────────────────────────────────────────

def test_pricing_call_is_parallel_and_cannot_throw():
    body = _add_part_body(_src())
    start = body.index("const suggestP = fetchSuggestion(pn, ref).catch(() => null);")
    lookup = body.index("await lookup(pn)")
    assert start < lookup  # fired before the lookup is awaited
    assert "await suggestP" not in body  # never awaited inside the logging flow
    assert "suggestP.then(s => applySuggestion(newId, s));" in body


def test_suggestion_respects_manual_price():
    s = _src()
    assert ("if (mode === 'live' && !p.price_touched && s.suggested_price != null) "
            "p.your_price = s.suggested_price;") in s
    assert "p.price_touched = true;" in s


def test_badge_reason_and_check_marker_rendered():
    s = _src()
    assert "${suggestionHtml(p)}" in s
    assert "Check price" in s
    assert "/price/suggest?" in s


def test_final_prices_recorded_via_capture_listener():
    s = _src()
    assert "'/price/suggestions/final'" in s
    assert "keepalive: true" in s
    # capture phase on document -> runs before the export handler clears `parts`
    assert re.search(r"closest\('#export-btn'\)\) recordFinalPrices\(\);\n\}, true\);", s)



# ── Shadow vs live: run the real pricing code in Node ──────────────────

MODE_HARNESS = r"""
const out = {};
let parts = [];
const window = { PA_PRICING_MODE: __DEFAULT_MODE__ };
const cfg = { url: 'http://x' };
let domTouched = 0, statsCalls = 0;
const partsList = { querySelector() { domTouched++; return null; } };
const saveParts = () => {};
const updateStats = () => { statsCalls++; };
const esc = s => String(s);
const document = { activeElement: null, addEventListener() {} };
__SECTION__
function card(extra) {
  return Object.assign({ id: 1, part_number: '5G1941006', your_price: 24.99, current_price: 24.99,
                         price_touched: false, engine: { state: 'loading', mode: window.PA_PRICING_MODE } }, extra || {});
}
// 1. loading state html
parts = [card()];
out.loadingHtml = suggestionHtml(parts[0]);
// 2. suggestion arrives with a price
applySuggestion(1, { mode: __RESP_MODE__, suggested_price: 84.99, confidence: 'high', reason: 'r', source: 'own_sold', suggestion_id: 7 });
out.priceAfter = parts[0].your_price;
out.okHtml = suggestionHtml(parts[0]);
out.suggestionId = parts[0].engine.suggestion_id;
out.domTouched = domTouched;
out.statsCalls = statsCalls;
// 3. engine failure
parts = [card()];
applySuggestion(1, null);
out.failedHtml = suggestionHtml(parts[0]);
out.priceAfterFail = parts[0].your_price;
// 4. logger typed a price first
parts = [card({ price_touched: true, your_price: 30 })];
applySuggestion(1, { mode: __RESP_MODE__, suggested_price: 84.99, confidence: 'high', reason: 'r', suggestion_id: 8 });
out.touchedPrice = parts[0].your_price;
// 5. no-price (variant-sensitive) answer
parts = [card()];
applySuggestion(1, { mode: __RESP_MODE__, suggested_price: null, confidence: 'low', reason: 'x · check price', suggestion_id: 9 });
out.noPricePrice = parts[0].your_price;
out.noPriceHtml = suggestionHtml(parts[0]);
process.stdout.write(JSON.stringify(out));
"""


def _pricing_section(text: str) -> str:
    a = text.index("// ── Pricing engine (non-blocking)")
    b = text.index("// ── Pricing feedback")
    return text[a:b]


def _run_modes(tmp_path, default_mode: str, resp_mode: str) -> dict:
    assert NODE, "Node.js is required for the shadow/live behaviour check"
    js = (MODE_HARNESS.replace("__SECTION__", _pricing_section(_src()))
          .replace("__DEFAULT_MODE__", json.dumps(default_mode)).replace("__RESP_MODE__", json.dumps(resp_mode)))
    script = tmp_path / f"modes_{default_mode}_{resp_mode}.js"
    script.write_text(js, encoding="utf-8")
    res = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_shadow_mode_leaves_screen_exactly_as_today(tmp_path):
    out = _run_modes(tmp_path, "shadow", "shadow")
    assert out["loadingHtml"] == ""            # no "Pricing..." text
    assert out["priceAfter"] == 24.99          # current price untouched
    assert out["okHtml"] == ""                 # no badge / reason
    assert out["failedHtml"] == ""             # no "engine unavailable" text
    assert out["noPriceHtml"] == "" and out["noPricePrice"] == 24.99
    assert out["domTouched"] == 0 and out["statsCalls"] == 0  # card DOM never patched
    assert out["suggestionId"] == 7            # still kept for the export log


def test_live_mode_shows_engine_price_badge_and_reason(tmp_path):
    out = _run_modes(tmp_path, "live", "live")
    assert "Pricing" in out["loadingHtml"]
    assert out["priceAfter"] == 84.99
    assert "PRICE &middot; HIGH" in out["okHtml"]
    assert "unavailable" in out["failedHtml"] and out["priceAfterFail"] == 24.99
    assert out["touchedPrice"] == 30           # a typed price is never overwritten
    assert out["noPricePrice"] == 24.99 and "Check price" in out["noPriceHtml"]


def test_server_mode_wins_over_cached_mode(tmp_path):
    # Page still thinks 'live' but the server answers 'shadow': stay silent.
    out = _run_modes(tmp_path, "live", "shadow")
    assert out["priceAfter"] == 24.99 and out["okHtml"] == ""


def test_shadow_is_default_and_export_sends_current_price():
    s = _src()
    assert "window.PA_PRICING_MODE = 'shadow';" in s
    assert "window.PA_PRICING_MODE = d.pricing_mode === 'live' ? 'live' : 'shadow';" in s
    assert "current_price:   prefill," in s
    assert "current_price: p.current_price != null ? Number(p.current_price) : null," in s
