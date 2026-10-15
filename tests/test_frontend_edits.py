"""Description edits in the logger page (index.html), run in Node.

The real persistence, render, edit, export-log and CSV export code is cut out
of index.html and run against a tiny fake DOM, so these check what the page
actually does: an edit is saved as it is typed (no blur needed, which is what
Android and iPhone both get wrong when the box is lost), survives a reload and
a re-render, goes into the CSV, and is reported to the server with retries.
"""

import json
import os
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")
NODE = shutil.which("node")


def _src() -> str:
    with open(INDEX, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def _cut(text: str, start: str, end: str) -> str:
    a = text.index(start)
    return text[a:text.index(end, a)]


def _sections() -> str:
    s = _src()
    return "\n".join([
        _cut(s, "// ── Persistence", "// ── DOM"),
        _cut(s, "// ── Export log", "// ── Add part"),
        _cut(s, "// ── Helpers", "// ── Delegated events"),  # helpers, edit, render
        _cut(s, "// ── Export CSV", "// ── Settings panel"),
    ])


HARNESS = r"""
const out = {};
let parts = [];
// ── fake DOM ──
const store = {};
const localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
const unesc = s => s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&');
class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.parent = null; this.dataset = {}; this.listeners = {};
    this.className = ''; this.value = ''; this.textContent = ''; this._html = ''; this.style = {};
    this.classList = { contains: c => this.className.split(' ').includes(c), add() {}, remove() {} };
  }
  set innerHTML(h) {
    this._html = h;
    this.children.forEach(c => { c.parent = null; });
    this.children = [];
    const d = h.match(/<div class="card-desc"[^>]*>([\s\S]*?)<\/div>/);
    if (d) { const e = new El('div'); e.className = 'card-desc'; e.textContent = unesc(d[1]); this.appendChild(e); }
    const t = h.match(/<div class="card-tags">([\s\S]*?)<\/div>/);
    if (t) { const e = new El('div'); e.className = 'card-tags'; e._html = t[1]; this.appendChild(e); }
  }
  get innerHTML() { return this._html; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === partsList; }
  replaceWith(n) { const i = this.parent.children.indexOf(this); n.parent = this.parent; this.parent.children[i] = n; this.parent = null; }
  after(n) { const i = this.parent.children.indexOf(this); n.parent = this.parent; this.parent.children.splice(i + 1, 0, n); }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
  matches(sel) {
    if (sel.startsWith('.')) return this.classList.contains(sel.slice(1));
    const m = sel.match(/^\[data-id="(.+)"\]$/);
    return !!m && String(this.dataset.id) === m[1];
  }
  querySelector(sel) {
    for (const c of this.children) { if (c.matches(sel)) return c; const r = c.querySelector(sel); if (r) return r; }
    return null;
  }
  addEventListener(type, fn, opts) { (this.listeners[type] = this.listeners[type] || []).push({ fn, once: opts && opts.once }); }
  dispatch(type, ev) {
    const ls = this.listeners[type] || [];
    this.listeners[type] = ls.filter(l => !l.once);
    ls.forEach(l => l.fn(Object.assign({ preventDefault() {} }, ev || {})));
  }
  focus() { document.activeElement = this; }
  blur() { if (document.activeElement === this) { document.activeElement = null; this.dispatch('blur'); } }
  select() {}
  setSelectionRange(a) { this._caret = a; }
  get selectionStart() { return this.value.length; }
  click() {}
}
const captureClicks = [];
const document = {
  activeElement: null,
  createElement: tag => new El(tag),
  addEventListener: (type, fn, capture) => { if (type === 'click' && capture === true) captureClicks.push(fn); },
  visibilityState: 'visible',
};
const window = { addEventListener() {}, PA_PRICING_MODE: 'shadow' };
const partsList = new El('div');
const emptyState = new El('div'), clearBtn = new El('div'), countLabel = new El('div');
let exportHandler = null;
const exportBtn = { addEventListener: (_e, fn) => { exportHandler = fn; }, closest: sel => (sel === '#export-btn' ? exportBtn : null) };
let csvBlob = null;
const URL = { createObjectURL: b => { csvBlob = b; return 'blob:x'; }, revokeObjectURL() {} };
const toast = () => {}, updateStats = () => {};
const suggestionHtml = () => '', nwlHtml = () => '', engineLive = () => false;
const cfg = { url: 'http://api' };
const posts = [];
let fetchOk = true;
const fetch = (url, opts) => {
  posts.push({ url, body: JSON.parse(opts.body), ok: fetchOk });
  return fetchOk ? Promise.resolve({ ok: true, json: () => Promise.resolve({}) }) : Promise.reject(new Error('offline'));
};
const realSetTimeout = setTimeout;
const timers = [];
setTimeout = (fn, ms) => { timers.push(fn); return timers.length; };  // eslint-disable-line no-global-assign
clearTimeout = () => {};  // eslint-disable-line no-global-assign

__SECTIONS__

const tick = () => new Promise(r => realSetTimeout(r, 0));
function card(extra) {
  return Object.assign({ id: 1, part_number: '6R0941531F', description: 'Headlight Switch Passenger Side',
    ai_description: 'Headlight Switch Passenger Side', source: 'claude', confidence: 'medium',
    vehicle_ref: 'VEH101', your_price: 12.5, part_note: '', avg_price: null, total_listings: 0 }, extra || {});
}
function descInput() { const a = document.activeElement; return a && a.className === 'card-desc-input' ? a : null; }
function type(text) { const i = descInput(); i.value = text; i.dispatch('input'); }
function openEdit(id) { partsList.querySelector(`[data-id="${id}"]`).querySelector('.card-desc').dispatch('click'); }
async function exportNow() {
  captureClicks.forEach(fn => fn({ target: exportBtn }));
  exportHandler();
  const text = Buffer.from(await csvBlob.arrayBuffer()).toString('utf8');
  await tick(); await tick();
  return text;
}
function reload() {  // a new page load: memory gone, localStorage kept
  parts = [];
  partsList.innerHTML = '';
  document.activeElement = null;
  loadParts();
  render();
}

(async () => {
__SCENARIO__
process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _run(tmp_path, scenario: str) -> dict:
    assert NODE, "Node.js is required for the description-edit behaviour checks"
    js = HARNESS.replace("__SECTIONS__", _sections()).replace("__SCENARIO__", scenario)
    script = tmp_path / "edits.js"
    script.write_text(js, encoding="utf-8")
    res = subprocess.run([NODE, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_edit_saved_as_typed_and_survives_reload(tmp_path):
    out = _run(tmp_path, """
      parts = [card()]; render();
      openEdit(1);
      type('Headlight Switch');            // no blur, no Enter: the phone just stops
      reload();
      out.desc = parts[0].description;
      out.ai = parts[0].ai_description;
      out.edited = parts[0].edited;
      out.shown = partsList.querySelector('[data-id="1"]').querySelector('.card-desc').textContent;
      out.tags = partsList.querySelector('[data-id="1"]').querySelector('.card-tags').innerHTML;
      out.queued = JSON.parse(localStorage.getItem('pulledapart_pending_corrections'));
    """)
    assert out["desc"] == "Headlight Switch" and out["shown"] == "Headlight Switch"
    assert out["ai"] == "Headlight Switch Passenger Side" and out["edited"] is True
    assert "Edited" in out["tags"]
    assert out["queued"]["6R0941531F"]["corrected_description"] == "Headlight Switch"
    assert out["queued"]["6R0941531F"]["ai_description"] == "Headlight Switch Passenger Side"


def test_edit_survives_a_rerender_while_typing(tmp_path):
    out = _run(tmp_path, """
      parts = [card()]; render();
      openEdit(1);
      type('Headlight Sw');
      parts.unshift(card({ id: 2, part_number: '5Q0959802A', description: 'Window Motor', ai_description: 'Window Motor' }));
      render();                           // another lookup landed mid-edit
      out.stillEditing = !!descInput() && descInput().value === 'Headlight Sw';
      type('Headlight Switch');
      descInput().blur();
      out.desc = parts.find(p => p.id === 1).description;
      out.shown = partsList.querySelector('[data-id="1"]').querySelector('.card-desc').textContent;
      await tick(); await tick();
      out.posted = posts.filter(p => p.url.endsWith('/db-correct')).map(p => p.body.corrected_description);
      out.queueLeft = JSON.parse(localStorage.getItem('pulledapart_pending_corrections'));
    """)
    assert out["stillEditing"] is True
    assert out["desc"] == "Headlight Switch" and out["shown"] == "Headlight Switch"
    assert out["posted"][-1] == "Headlight Switch"
    assert out["queueLeft"] == {}


def test_export_uses_the_edited_value_and_logs_it(tmp_path):
    out = _run(tmp_path, """
      parts = [card(), card({ id: 2, part_number: '5Q0959802A', description: 'Window Motor',
                              ai_description: 'Window Motor', vehicle_ref: 'VEH101' })];
      render();
      openEdit(1);
      type('Headlight Switch');
      out.csv = await exportNow();
      const log = posts.find(p => p.url.endsWith('/parts/exports'));
      out.rows = log && log.body.rows;
      out.exportQueue = JSON.parse(localStorage.getItem('pulledapart_export_queue'));
      out.partsAfter = parts.length;
    """)
    assert "Headlight Switch Passenger Side" not in out["csv"]
    assert "6R0 941 531 F,,Headlight Switch,12.50,VEH101" in out["csv"]
    r = out["rows"][0]
    assert r == {"part_number": "6R0941531F", "ai_description": "Headlight Switch Passenger Side",
                 "final_description": "Headlight Switch", "edited": True, "correction_applied": False,
                 "vehicle_ref": "VEH101"}
    assert out["rows"][1]["edited"] is False
    assert out["exportQueue"] == [] and out["partsAfter"] == 0


def test_export_still_works_when_logging_fails_and_retries_later(tmp_path):
    out = _run(tmp_path, """
      fetchOk = false;
      parts = [card({ description: 'Headlight Switch' })]; render();
      out.csv = await exportNow();
      out.queued = JSON.parse(localStorage.getItem('pulledapart_export_queue')).length;
      fetchOk = true;
      reload();
      await flushExports();               // next page load / reconnect
      const sent = posts.filter(p => p.url.endsWith('/parts/exports'));
      out.sameBatch = sent[0].body.export_id === sent[sent.length - 1].body.export_id;
      out.queuedAfter = JSON.parse(localStorage.getItem('pulledapart_export_queue')).length;
    """)
    assert "Headlight Switch" in out["csv"]
    assert out["queued"] == 1 and out["queuedAfter"] == 0 and out["sameBatch"] is True


def test_correction_retried_until_the_server_has_it(tmp_path):
    out = _run(tmp_path, """
      fetchOk = false;
      parts = [card()]; render();
      openEdit(1); type('Headlight Switch'); descInput().blur();
      await tick(); await tick();
      out.queuedOffline = Object.keys(JSON.parse(localStorage.getItem('pulledapart_pending_corrections')));
      fetchOk = true;
      await flushCorrections();
      out.queuedAfter = JSON.parse(localStorage.getItem('pulledapart_pending_corrections'));
    """)
    assert out["queuedOffline"] == ["6R0941531F"] and out["queuedAfter"] == {}


def test_stored_correction_shows_as_corrected_and_stays_editable(tmp_path):
    out = _run(tmp_path, """
      parts = [card({ description: 'Headlight Switch', ai_description: 'Headlight Switch Passenger Side',
                      source: 'corrected', corrected: true, confidence: 'high' })];
      render();
      out.tags = partsList.querySelector('[data-id="1"]').querySelector('.card-tags').innerHTML;
      openEdit(1); type('Headlight Switch With Fog'); descInput().blur();
      out.desc = parts[0].description;
      out.csv = await exportNow();
      out.row = posts.find(p => p.url.endsWith('/parts/exports')).body.rows[0];
    """)
    assert "Corrected" in out["tags"]
    assert out["desc"] == "Headlight Switch With Fog"
    assert "Headlight Switch With Fog" in out["csv"]
    assert out["row"]["correction_applied"] is True and out["row"]["edited"] is True


def test_escape_puts_the_old_name_back(tmp_path):
    out = _run(tmp_path, """
      parts = [card()]; render();
      openEdit(1); type('Typo Name');
      descInput().dispatch('keydown', { key: 'Escape' });
      out.desc = parts[0].description;
      out.edited = parts[0].edited;
    """)
    assert out["desc"] == "Headlight Switch Passenger Side" and out["edited"] is False


def test_lookup_answer_keeps_ai_name_and_correction_flag():
    s = _src()
    assert "ai_description:  d.ai_description != null ? d.ai_description : d.description," in s
    assert "corrected:       !!d.corrected," in s
    assert "'/parts/exports'" in s and "'/db-correct'" in s
