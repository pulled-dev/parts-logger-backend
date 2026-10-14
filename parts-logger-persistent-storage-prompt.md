# Parts Logger — Add Persistent Storage for Logged Parts

## Context
This is the Pulled Apart Parts Logger — a mobile-first HTML frontend (index.html) that connects to a FastAPI backend on Railway. The frontend is a single-file HTML page hosted on GitHub Pages. It lets yard workers type VAG part numbers, get descriptions and eBay pricing from the backend, log parts with custom prices, and export to CSV.

## The Problem
The `parts` array in JavaScript is stored only in memory (`let parts = [];`). When the user navigates away from the page, refreshes, or closes the browser, all logged parts are lost. They have to re-enter everything. This is a real problem in the yard when workers need to switch between apps on their phone mid-strip.

## What To Build
Add localStorage persistence so logged parts survive page refreshes, browser closes, and navigation. Parts should only be cleared when the user explicitly exports CSV or hits "Clear All".

## Requirements

1. **Save parts to localStorage** — every time the `parts` array changes (add, delete, edit price), serialise it to JSON and save to `localStorage` under key `pulledapart_logged_parts`
2. **Restore on page load** — when the page loads, check localStorage for `pulledapart_logged_parts`. If found, parse it and restore the parts array, then re-render the parts list, stats bar, and footer exactly as if they'd just been added
3. **Clear on CSV export** — when the user clicks "Export CSV" and the CSV is generated/downloaded, clear the localStorage key AND the in-memory array, then re-render to show the empty state
4. **Clear on "Clear All"** — same as above, clear both localStorage and memory
5. **Also persist the vehicle ref** — save the last used vehicle ref value to `localStorage` under key `pulledapart_last_ref` so it pre-fills when the page reloads (saves re-typing it for every part on the same car)
6. **Storage quota safety** — wrap localStorage writes in a try/catch. If storage is full (very unlikely but possible), log a console warning but don't break the app. The tool should still work normally, just without persistence.

## Implementation Guidance

- Create a `saveParts()` function that does `localStorage.setItem('pulledapart_logged_parts', JSON.stringify(parts))` — call this at the end of every function that modifies the parts array
- Create a `loadParts()` function that reads and parses from localStorage, called once on page init
- After `loadParts()`, call the existing render/update functions to rebuild the UI (stats bar, parts list, footer visibility, export button count)
- The existing `clearAll()` function should call `localStorage.removeItem('pulledapart_logged_parts')` and `localStorage.removeItem('pulledapart_last_ref')`
- The existing `exportCSV()` function should call the same localStorage clear AFTER the CSV has been generated and download triggered
- For vehicle ref: save on every change (input event on the ref field), restore on page load by setting the input value

## Do NOT Change
- Do not modify the backend (main.py) — this is frontend only
- Do not change the visual design, layout, colours, or fonts
- Do not change how the API calls work
- Do not add any new dependencies or libraries
- Do not restructure the HTML file into multiple files
- Keep it as a single index.html file

## Feedback Loop
After making changes:
1. **Read** the current index.html to understand the existing code structure
2. **Implement** the localStorage changes
3. **Test** by searching for any syntax errors — run a quick grep/check that all function calls reference existing functions, all brackets are closed, JSON.parse has try/catch wrapping
4. **Verify** that the saveParts() call exists in every function that modifies the parts array (look for all places where `parts.push`, `parts.splice`, `parts.filter`, `parts =`, or `parts[index]` appear and confirm saveParts() is called after each)
5. **If any issues found** — diagnose the root cause, fix it, then re-run steps 3-4
6. **Commit** with message: "feat: persist logged parts to localStorage until CSV export"
7. **Push** to main branch (this auto-deploys via GitHub Pages)
