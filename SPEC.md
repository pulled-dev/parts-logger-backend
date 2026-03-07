# Parts Logger — Accurate Identification System

## Vision
The Parts Logger tool currently uses Claude API to identify VAG part numbers but gets 87% wrong. This project replaces AI guessing with a three-layer identification system: a JSON database built from Naveed's own BreakerPro export history (hundreds of verified part number → description pairs), automated Partslink24 lookups via Chrome for unknown parts, and auto-learning so the database grows with every car stripped.

## Goals
- Build a JSON lookup database from BreakerPro CSV export data (verified, breaker-style descriptions)
- Extract "middle group" patterns from part numbers so the same part type across different platforms (5G0, 6J3, 6F0 etc.) all resolve correctly
- Wire the database into the existing Parts Logger backend as the primary identification source
- Add Partslink24 Chrome automation as fallback for parts not in the database
- Auto-save successful Partslink24 lookups back to the database so it grows over time
- Achieve 95%+ accuracy from day one using real historical data

## Tech Stack
- Python — CSV parser, database builder, backend integration
- JSON — lookup database file (`vag_parts_db.json`)
- Existing FastAPI backend (`parts-logger-backend/main.py`)
- Chrome automation (Playwright) for Partslink24 fallback (Phase 3 — separate project)
- Claude API — only used to translate OEM names from Partslink24 into breaker-style short names (not for raw part ID)

## Constraints
- Must integrate into existing `parts-logger-backend/main.py` without breaking current endpoints
- BreakerPro CSV format may vary — parser must handle column name variations
- Database must handle part number format variations (spaces, no spaces, mixed case)
- Side designation (OS/NS) should be derived from the part number suffix where possible
- Partslink24 automation is Phase 3 and will be specced separately

## Out of Scope
- GTmotive integration (Naveed uses this manually, separate from this tool)
- BreakerPro API integration (using CSV exports for now)
- UI for database management (v2)


---


# Requirements

## v1 (Must Have)
- [ ] REQ-001: Python script that parses BreakerPro CSV exports and extracts unique part number → description pairs
- [ ] REQ-002: Handle BreakerPro CSV format quirks — the CSV has a non-standard structure with vehicle info rows mixed with parts rows. Columns include: part name, make, model, year, part number, paint code, mileage, price. Part numbers may contain spaces, mixed case, or be engine/gearbox codes (e.g. CBZ, PKZ) rather than VAG part numbers
- [ ] REQ-003: Extract the "middle group" (6 digits identifying what the part IS) from each part number, ignoring the platform prefix (first 2-3 chars). Map each middle group to its breaker-style description
- [ ] REQ-004: Build a JSON database file with two lookup layers: (a) exact part number matches, and (b) middle-group matches for parts not seen before but in the same family
- [ ] REQ-005: Side-designation function — determine OS/NS/NSF/OSF/NSR/OSR from part number suffix digits using the odd=left/even=right VAG convention, and append to description
- [ ] REQ-006: Lookup function that normalises input (strips spaces, uppercases), checks exact match first, then middle-group match, then returns result with confidence level
- [ ] REQ-007: Integration into existing `main.py` — database lookup runs BEFORE Claude API call. Database hit = instant response (no API call). Database miss = fall back to Claude (with improved prompt)
- [ ] REQ-008: Improved Claude prompt as fallback — include VAG numbering rules, common group codes, and side-designation logic so Claude performs better on unknown parts
- [ ] REQ-009: Auto-learning — when Claude or Partslink24 identifies a part not in the database, save the mapping to a "learned" section of the JSON for Naveed to review
- [ ] REQ-010: The JSON database must be human-readable and easy for Naveed to manually edit/correct
- [ ] REQ-011: Store historical BreakerPro price alongside each part in the database. When a part is looked up, return the last known price from BreakerPro as `breakerpro_price` in the response alongside the eBay pricing data
- [ ] REQ-012: Frontend update — show BreakerPro historical price next to eBay average in the Parts Logger UI, so the user sees both "eBay avg: £24.99" and "Your last: £22.99" when logging a part
- [ ] REQ-013: Database file (`vag_parts_db.json`) lives on the Railway backend server alongside main.py. Auto-learning writes to this file on the server. No local storage needed — phones access everything via the API
- [ ] REQ-014: Editable descriptions — when a part is logged in the frontend, the description field must be editable (tap to edit). If the user changes the description, the frontend sends the correction to a new backend endpoint `POST /db-correct` with `{part_number, corrected_description}`. The backend updates the exact entry in the database (or creates one if it was a group/learned match). This means if Claude misidentifies a part, the user corrects it once on their phone and it's fixed permanently for all future lookups.

## v2 (Nice to Have)
- [ ] REQ-100: Partslink24 Chrome automation for unknown parts (separate spec — requires Playwright + session management)
- [ ] REQ-101: Confidence scoring displayed in the Parts Logger UI
- [ ] REQ-102: Dashboard showing database coverage stats (how many parts in DB vs falling back to Claude)
- [ ] REQ-103: Bulk import — drag multiple BreakerPro CSVs to grow the database at once

## Out of Scope
- Partslink24 API (doesn't exist)
- GTmotive integration
- BreakerPro direct API


---


# Roadmap

## Phase 1: BreakerPro CSV Parser + Database Builder
Parse BreakerPro CSV exports, extract unique part number → description pairs, handle format quirks, build the initial JSON database.
**Requirements:** REQ-001, REQ-002, REQ-003, REQ-004, REQ-010

## Phase 2: Lookup Function + Side Logic + Backend Integration
Build the lookup module with normalisation, exact match, middle-group match, and side designation. Wire it into the existing Parts Logger backend. Add improved Claude fallback prompt.
**Requirements:** REQ-005, REQ-006, REQ-007, REQ-008

## Phase 3: Auto-Learning
When a part is identified by Claude (fallback), auto-save the mapping to a "learned" section of the database. Over time this reduces Claude fallback usage.
**Requirements:** REQ-009

## Phase 4: Partslink24 Chrome Automation (separate project)
Build Playwright-based automation to look up unknown parts on Partslink24 using Naveed's existing subscription. Requires separate spec due to complexity of session management and page navigation.
**Requirements:** REQ-100


---


# Phase Plans

## Phase 1: BreakerPro CSV Parser + Database Builder

<task type="auto">
  <n>Build BreakerPro CSV parser</n>
  <files>parts-logger-backend/breakerpro_parser.py</files>
  <action>
    Create a Python script that parses BreakerPro CSV exports and extracts part number → description pairs.

    BREAKERPRO CSV FORMAT (from real exports):
    The BreakerPro CSV is non-standard. Key characteristics observed from real exports:
    
    1. File encoding is Latin-1 (not UTF-8). Use `encoding='latin-1'` when reading.
    2. First row is a title row like "Stock - Stock Reference veh1 - 070326 092552" — skip it
    3. Second and third rows are blank — skip them
    4. Fourth row is the header row with columns:
       `Part ID, þ, Part Name, Part Nett Sale, Make, Model, Part Description, Part Comments, Engine, Style, Year, Colour, Part Condition, Stock Reference, Fuel, Part Type, Part Location, Vehicle Id, Reg, Miles, Comments, Originator, Ebay_title, Ebay_number, Lockby, Lockexpirydate, Lockreason, Siteid, Part_settings, Invoice ID, Invoice Date`
    5. The `þ` column (column 2) is a BreakerPro delimiter character — ignore it
    6. **Part numbers are NOT in their own column.** They are embedded in the "Part Description" column as free text, typically in the format:
       - "Part Number is 6R2 880 201 B" 
       - "Part number is 6R0 959 801 T"
       - "Part number is 03C 906 024 CN"
       The parser must use regex to extract part numbers from this text:
       `re.search(r'[Pp]art\s*[Nn]umber\s*is\s+([A-Za-z0-9\s]+?)(?:\s*$|[A-Z]{2,}|[a-z]{2,})', description)`
       IMPORTANT: Part numbers in BreakerPro descriptions often have spaces (e.g. "6R2 880 201 B") — these must be preserved during extraction but normalised (spaces stripped) for database keys.
    7. Some descriptions contain paint codes instead of part numbers: "Paint code is LA7W SILVER" — extract these separately
    8. Some descriptions have engine/gearbox codes like "LNR", "CBZ", "CGG" in the Part Number field — these are typically 2-4 uppercase letters with NO digits. Filter these out as they are not VAG part numbers.
    9. The "Part Name" column contains the short breaker-style description we want: "AIR BAG (DRIVER SIDE)", "DOOR LOCK MECH (REAR DRIVER SIDE)", "HEATER CONTROL PANEL", "WIPER MOTOR (FRONT) & LINKAGE"
    10. The "Part Nett Sale" column contains the price (e.g. "24.99", "79.99")
    11. The "Make" and "Model" columns give vehicle context (e.g. "Volkswagen", "Polo Match 6r")
    12. Rows with Part Name = "Breaking For Spares" should be SKIPPED — these are whole-vehicle listings, not individual parts
    13. The "Part_settings" column contains massive strings of eBay compatibility numbers — IGNORE this column entirely
    14. There will be duplicate entries (same part listed multiple times with same part number) — deduplicate by keeping the first occurrence
    15. The "Year" column contains year ranges like "2010-2017"
    16. Lines end with `\r\n` (Windows line endings)

    The parser should:
    
    a) Accept a CSV file path (or multiple) as input
    b) Auto-detect which columns contain part numbers and descriptions by checking common column name variations
    c) Extract every row that has both a non-empty part number AND a non-empty description
    d) Filter out engine/gearbox codes — these are typically 3-4 uppercase letters with no numbers (CBZ, PKZ, DAD, CJAA, etc.). VAG part numbers always contain numbers.
    e) Filter out rows where part number is "N/A" or blank
    f) Normalise part numbers: strip spaces, uppercase
    g) Clean descriptions: trim whitespace, remove excessive detail, keep it breaker-style short (if the BreakerPro description is already short like "Door Lock OSF", keep it as-is)
    h) Deduplicate: if the same part number appears multiple times (from different vehicles), keep the most common description
    i) Output a dict of {normalised_part_number: description}

    Also extract the "middle group" for each part number:
    - VAG part numbers follow: [2-3 char prefix][6 digit group][revision suffix]
    - Examples: 5G0959857A → prefix=5G0, group=959857, suffix=A
    - Examples: 6J3837401AJ → prefix=6J3, group=837401, suffix=AJ  
    - Examples: 6F0839461A → prefix=6F, group=839461, suffix=A (2 char prefix: 6F)
    - Strategy: try extracting 6 consecutive digits from the part number. The first sequence of exactly 6 digits IS the middle group.
    - Build a second dict: {middle_group: description} — this is the generalised lookup
    - When multiple part numbers share the same middle group but have different descriptions, keep ALL descriptions and pick the most common one

    Include a CLI interface:
    ```
    python breakerpro_parser.py input.csv [input2.csv ...] --output vag_parts_db.json
    ```

    Can also accept a directory of CSVs:
    ```
    python breakerpro_parser.py --dir ./exports/ --output vag_parts_db.json
    ```
  </action>
  <verify>
    Test with a mock CSV that mimics the REAL BreakerPro format:
    ```csv
    Stock - Stock Reference veh1 - 070326 092552


    Part ID,þ,Part Name,Part Nett Sale,Make,Model,Part Description,Part Comments,Engine,Style,Year,Colour,Part Condition,Stock Reference,Fuel,Part Type,Part Location,Vehicle Id,Reg,Miles,Comments,Originator,Ebay_title,Ebay_number
    1659-001,þ,AIR BAG (DRIVER SIDE),24.99,Volkswagen,Polo Match 6r,Fully TestedGood conditionPart Number is 6R2 880 201 B,,1.4 CGG,,2010-2017,Silver,,VEH1,Petrol,INTERIOR,,VH-001,mj61fvv,103000,,,test title,123
    1659-002,þ,DOOR LOCK MECH (REAR DRIVER SIDE),14.99,VOLKSWAGEN,POLO MATCH 6R,Fully TestedGood conditionPart Number is 6R4 839 016,,1.4 CGG,Doors,2010-2017,SILVER,,veh1,Petrol,MECHANICAL,,VH-001,mj61fvv,103000,,,test title,456
    1659-003,þ,HEATER CONTROL PANEL,19.99,Volkswagen,Polo Match 6r,Fully TestedGood working orderPart Number is 6R0 820 045 G,,1.4 CGG,,2010-2017,Silver,,VEH1,Petrol,MECHANICAL,,VH-001,mj61fvv,103000,,,test title,789
    1659-004,þ,BONNET,79.99,VOLKSWAGEN,POLO MATCH 6R,Complete with hingesBonnet has some damage SEE PICPaint code is LA7W SILVER,,1.4 CGG,,2010-2017,SILVER,,veh1,Petrol,BODY PARTS,,VH-001,mj61fvv,103000,,,test title,012
    1659-005,þ,GEARBOX - MANUAL,99.99,VOLKSWAGEN,POLO MATCH 6R,Fully TestedGood working orderPart Number is LNR,,1.4 CGG,Doors,2010-2017,SILVER,,veh1,Petrol,MECHANICAL,,VH-001,mj61fvv,103000,,,test title,345
    1659-006,þ,Breaking For Spares,4.99,VOLKSWAGEN,POLO MATCH 6R,6R Polo description here,,1.4 CGG,,2010-2017,SILVER,,VEH1,Petrol,ENGINE BAY,,VH-001,mj61fvv,103000,,,test title,678
    ```
    
    Expected output:
    - 3 parts with valid VAG part numbers extracted: 6R2880201B (Air Bag), 6R4839016 (Door Lock Mech), 6R0820045G (Heater Control Panel)
    - BONNET kept but flagged as no part number (paint code only)
    - LNR filtered out as gearbox code (letters only, no digits)
    - "Breaking For Spares" row skipped entirely
    - Middle groups extracted: 880201, 839016, 820045
    - Prices preserved: 24.99, 14.99, 19.99
    - Descriptions preserved from Part Name column: "AIR BAG (DRIVER SIDE)", "DOOR LOCK MECH (REAR DRIVER SIDE)", "HEATER CONTROL PANEL"
  </verify>
  <done>Parser script exists, handles BreakerPro format quirks, extracts and deduplicates part number → description pairs, builds middle-group mappings.</done>
</task>

<task type="auto">
  <n>Build JSON database from parsed data</n>
  <files>parts-logger-backend/vag_parts_db.json, parts-logger-backend/build_db.py</files>
  <action>
    Create a `build_db.py` script that takes the parser output and builds the final JSON database file.

    JSON structure:
    ```json
    {
      "_meta": {
        "version": "1.0",
        "description": "VAG part number lookup database built from BreakerPro export history",
        "source": "BreakerPro CSV exports from Pulled Apart Ltd",
        "last_updated": "2026-03-06",
        "total_exact_entries": 0,
        "total_group_entries": 0
      },
      "exact": {
        "6J3837401AJ": {"description": "Door Lock OSF", "breakerpro_price": 24.99, "vehicle": "Seat Ibiza 2015"},
        "6F2819403K": {"description": "Heater Blower Motor", "breakerpro_price": 34.99, "vehicle": "Skoda Fabia 2018"},
        "5G0959857A": {"description": "Window Switch Pack", "breakerpro_price": 29.99, "vehicle": "VW Golf 2017"}
      },
      "groups": {
        "837401": {"description": "Door Lock", "avg_price": 24.99},
        "839461": {"description": "Interior Handle", "avg_price": 12.99},
        "819403": {"description": "Heater Blower Motor", "avg_price": 34.99},
        "955409": {"description": "Rear Wiper Motor", "avg_price": 19.99}
      },
      "learned": {}
    }
    ```

    Three sections:
    1. `exact` — full part number → {description, breakerpro_price, vehicle}. Fastest, most accurate. Price is what Naveed listed it for last time.
    2. `groups` — middle group → {description, avg_price}. The avg_price is averaged across all exact entries sharing that group. No side suffix — side is determined at lookup time.
    3. `learned` — auto-populated by the system when Claude identifies new parts. Format: {description: str, learned_at: ISO timestamp}. Naveed can review and promote entries to `exact` if correct.

    The `groups` descriptions should NOT include side designations (OS/NS etc.) — those get appended by the lookup function based on the odd/even rule.

    The `exact` descriptions CAN include side designations since they're from real BreakerPro data where Naveed already wrote them correctly.

    The build script should:
    1. Run the parser on provided CSVs
    2. Build the exact and groups dicts
    3. If a `vag_parts_db.json` already exists, MERGE new entries (don't overwrite existing ones — existing entries are considered verified)
    4. Update the _meta counts
    5. Save the JSON with indentation for readability

    CLI:
    ```
    python build_db.py input.csv [input2.csv ...] --db vag_parts_db.json
    ```
    Or:
    ```
    python build_db.py --dir ./exports/ --db vag_parts_db.json
    ```

    If --db file exists, merge. If not, create new.
  </action>
  <verify>
    1. Run build_db.py on the mock CSV from the previous task
    2. Verify vag_parts_db.json is created with correct structure
    3. Run again on a second mock CSV — verify entries are MERGED not overwritten
    4. Verify _meta counts are accurate
    5. Verify the JSON is valid and human-readable (indented)
  </verify>
  <done>build_db.py and vag_parts_db.json exist. Database correctly built from CSV data with exact matches, group matches, and empty learned section. Merge works correctly on subsequent runs.</done>
</task>


## Phase 2: Lookup Function + Backend Integration

<task type="auto">
  <n>Build lookup module with side logic</n>
  <files>parts-logger-backend/vag_lookup.py</files>
  <action>
    Create a Python module that provides the part number lookup function.

    1. Load the JSON database at module import time (stays in memory for fast lookups):
    ```python
    import json, os, re

    DB_PATH = os.path.join(os.path.dirname(__file__), "vag_parts_db.json")
    _db = None

    def _load_db():
        global _db
        with open(DB_PATH, "r") as f:
            _db = json.load(f)
        return _db

    def get_db():
        if _db is None:
            _load_db()
        return _db

    def reload_db():
        """Reload after auto-learning adds new entries"""
        return _load_db()
    ```

    2. `normalise(raw: str) -> str`:
       - Strip all spaces, uppercase
       - Return clean string like "6J3837401AJ"

    3. `extract_middle_group(part_number: str) -> str | None`:
       - Find the first sequence of exactly 6 consecutive digits in the normalised part number
       - "6J3837401AJ" → finds "837401"
       - "5Q0407272C" → finds "407272"
       - "5NA945096E" → finds "945096"
       - Return None if no 6-digit sequence found

    4. `determine_side(part_number: str, group_code: str, base_description: str) -> str`:
       VAG side rules:
       - Parts that come in pairs (doors, lights, mirrors, handles, hubs, wishbones) have side designation
       - Parts that are single (heater blower, wiper motor, handbrake switch, ECU) do NOT
       
       Determining if a part is paired:
       - Check if the base description already contains a side indicator (OS, NS, NSF, OSF, NSR, OSR, left, right, driver, passenger) — if so, it's already handled, return ""
       - Maintain a list of group prefixes that are typically paired:
         - 837 = front door parts (paired, use NSF/OSF)
         - 839 = rear door parts (paired, use NSR/OSR)
         - 857 = mirrors (paired, use NS/OS)
         - 941 = front lights (paired, use NSF/OSF)
         - 945 = rear lights (paired, use NSR/OSR or NS/OS)
         - 407 = front suspension (paired, use NS/OS)
         - 505 = rear suspension (paired, use NS/OS)
         - 959 = window motors/switches (some paired, some not — 959801/802 paired, 959857 single)
       
       Determining the side:
       - Look at the last digit of the sub-group number (last 3 digits of the 6-digit group)
       - Odd = Left = Nearside (NS) = Passenger in UK
       - Even = Right = Offside (OS) = Driver in UK
       - Example: 837401 → sub-group 401 → last digit 1 → odd → NS → for front door = "NSF"
       - Example: 945096 → sub-group 096 → last digit 6 → even → OS → for rear light = "OS"
       
       Return the side suffix string ("NSF", "OSF", "NSR", "OSR", "NS", "OS") or "" if not a paired part

    5. `lookup_part(raw: str) -> dict | None`:
       Main lookup function. Returns dict or None.
       
       Steps:
       a) Normalise input
       b) Check `exact` dict first — if found, return {"description": entry["description"], "source": "database", "confidence": "high", "breakerpro_price": entry.get("breakerpro_price"), "vehicle": entry.get("vehicle")}
       c) Extract middle group
       d) Check `groups` dict — if found:
          - Get base description and avg_price
          - Determine side
          - Combine: f"{base_description} {side}".strip()
          - Return {"description": combined, "source": "database", "confidence": "medium", "breakerpro_price": entry.get("avg_price"), "vehicle": None}
       e) Check `learned` dict — if found, return {"description": description, "source": "learned", "confidence": "medium", "breakerpro_price": None, "vehicle": None}
       f) Return None (caller falls back to Claude)

    6. `save_learned(part_number: str, description: str)`:
       - Add to the "learned" section of the JSON database
       - Write the file back to disk
       - Reload the in-memory database
       - This is called when Claude identifies a part not in the database
  </action>
  <verify>
    Run tests:
    ```python
    from vag_lookup import lookup_part, normalise, extract_middle_group

    # Test normalisation
    assert normalise("6j3 837 401 aj") == "6J3837401AJ"
    assert normalise("5NA945096e") == "5NA945096E"

    # Test middle group extraction
    assert extract_middle_group("6J3837401AJ") == "837401"
    assert extract_middle_group("5Q0407272C") == "407272"
    assert extract_middle_group("5NA945096E") == "945096"

    # Test lookups (after building DB from mock data)
    result = lookup_part("6J3837401AJ")
    assert result is not None
    assert result["source"] == "database"
    # Description should be correct (from exact match)

    # Test group-level lookup with side
    # A new part number with same group but different prefix should still match
    result2 = lookup_part("5G0837401AJ")  # Different prefix, same group
    assert result2 is not None
    assert "Door Lock" in result2["description"]
    assert "NSF" in result2["description"] or "NS" in result2["description"]  # odd=left=NS
    ```
  </verify>
  <done>vag_lookup.py module works correctly with exact match, group match, side designation, and learned entry saving.</done>
</task>

<task type="auto">
  <n>Improved Claude fallback prompt</n>
  <files>parts-logger-backend/claude_prompt.py</files>
  <action>
    Create a module containing the improved Claude prompt for when a part number is NOT in the database.

    The prompt must include:
    - VAG part number anatomy (prefix, main group, sub-group, suffix)
    - Common platform prefixes (5G0=Golf Mk7, 6J0=Ibiza, 6F0=Ibiza Mk5/Arona, 5TA=Tiguan, etc.)
    - Side designation rules (odd=NS/left, even=OS/right)
    - Front/rear group rules (837=front door, 839=rear door, 941=front lights, 945=rear lights)
    - Common main group codes (837=door, 857=mirror, 941=headlights, 945=tail lights, 819=heating, 955=wipers, 959=window electrics, 907=ECUs, 925=fuse box, 927=handbrake, 953=stalks)
    - Instruction to output SHORT breaker-style names (2-5 words max)
    - Instruction to include correct side designation
    - Instruction to respond "Unknown Part" if unsure rather than guess

    Export as:
    ```python
    VAG_SYSTEM_PROMPT = "..."  # The comprehensive prompt string
    
    def build_identification_prompt(part_number: str) -> str:
        """Build the full prompt for Claude API call"""
        return VAG_SYSTEM_PROMPT + f"\n\nIdentify this VAG part number: {part_number}"
    ```

    Keep the prompt under 1500 words — comprehensive but not wasteful on tokens.
  </action>
  <verify>
    - Import module successfully
    - Prompt string is well-formed and under 1500 words
    - build_identification_prompt returns a string containing both the system knowledge and the part number
  </verify>
  <done>claude_prompt.py exists with comprehensive VAG identification prompt.</done>
</task>

<task type="auto">
  <n>Wire everything into main.py</n>
  <files>parts-logger-backend/main.py</files>
  <action>
    Modify the existing `main.py` to use the new lookup system.

    1. Add imports at the top:
    ```python
    from vag_lookup import lookup_part as db_lookup, save_learned, reload_db
    from claude_prompt import build_identification_prompt
    ```

    2. Update the `identify_with_claude()` function:
       - Replace the old hardcoded prompt with `build_identification_prompt(part_number)`
       - Keep the same HTTP call structure
       - After successful Claude identification, call `save_learned(clean_part_number, description)` to auto-save to the database

    3. Update the `/lookup` endpoint:
    ```
    New flow:
    1. Normalise input
    2. Paint code? → Claude only (unchanged)
    3. Engine/gearbox code? → Skip database, use Claude with engine-specific prompt
    4. Database lookup (exact → group → learned)
       a. HIT → Use database description, skip Claude. Still run eBay for pricing.
       b. MISS → Fall back to improved Claude prompt + eBay for pricing (parallel)
       c. When Claude identifies successfully → auto-save to learned section
    ```

    4. Update response to include source field: "database", "learned", "claude", or "ebay"

    5. Add a new endpoint for database stats:
    ```python
    @app.get("/db-stats")
    async def db_stats():
        db = get_db()
        return {
            "exact_entries": len(db.get("exact", {})),
            "group_entries": len(db.get("groups", {})),
            "learned_entries": len(db.get("learned", {})),
            "last_updated": db.get("_meta", {}).get("last_updated", "unknown")
        }
    ```

    6. Add an endpoint to reload the database (for when Naveed manually edits the JSON):
    ```python
    @app.post("/db-reload")
    async def db_reload():
        reload_db()
        return {"status": "reloaded"}
    ```

    7. Add a correction endpoint for when users fix a description on their phone:
    ```python
    @app.post("/db-correct")
    async def db_correct(req: CorrectionRequest):
        """User corrected a description in the frontend. 
        Save it as an exact match — overrides any previous entry."""
        clean = req.part_number.strip().upper().replace(" ", "")
        db = get_db()
        
        # Always save to exact — user corrections are the highest authority
        db["exact"][clean] = {
            "description": req.corrected_description,
            "breakerpro_price": req.price,  # optional, may be null
            "vehicle": None,
            "corrected": True,
            "corrected_at": datetime.utcnow().isoformat()
        }
        
        # Also remove from learned if it was there (it's now verified)
        if clean in db.get("learned", {}):
            del db["learned"][clean]
        
        save_db(db)
        reload_db()
        return {"status": "saved", "part_number": clean, "description": req.corrected_description}
    ```
    
    Add the request model:
    ```python
    class CorrectionRequest(BaseModel):
        part_number: str
        corrected_description: str
        price: Optional[float] = None
    ```

    DO NOT rewrite the entire main.py. Only modify:
    - Imports
    - identify_with_claude() function
    - lookup_part() endpoint
    - Add db-stats and db-reload endpoints
  </action>
  <verify>
    1. Start backend: `python main.py`
    2. Health check: `curl http://localhost:8000/health`
    3. DB stats: `curl http://localhost:8000/db-stats` — should show entry counts
    4. Test known part (in database):
       ```bash
       curl -X POST http://localhost:8000/lookup \
         -H "Content-Type: application/json" \
         -d '{"part_number": "6J3837401AJ"}'
       ```
       Expected: correct description, source="database"
    5. Test unknown part (not in database):
       ```bash
       curl -X POST http://localhost:8000/lookup \
         -H "Content-Type: application/json" \
         -d '{"part_number": "1K0199262CE"}'
       ```
       Expected: Claude fallback, source="claude"
    6. Check vag_parts_db.json — the unknown part should now appear in the "learned" section
    7. Test the same unknown part again — should now come from "learned", not Claude
  </verify>
  <done>Backend uses database-first identification. Database hits skip Claude entirely. Claude fallback auto-saves to learned section. All existing endpoints still work.</done>
</task>


## Phase 3: Auto-Learning Verification

<task type="auto">
  <n>Test auto-learning end-to-end</n>
  <files>parts-logger-backend/test_autolearn.py</files>
  <action>
    Create a test script that verifies the full auto-learning cycle:
    
    1. Start with a fresh database (only BreakerPro data)
    2. Look up a part NOT in the database
    3. Verify Claude identifies it
    4. Verify the identification is saved to learned section
    5. Look up the same part again
    6. Verify it now comes from "learned" (no Claude call)
    7. Check the JSON file on disk has the new entry

    Also test edge cases:
    - Part number with spaces
    - Part number with lowercase
    - Paint code (should still go to Claude, not database)
    - Engine code like "CBZ" (should be handled gracefully)
    - Empty part number
    - "N/A" part number
  </action>
  <verify>
    All tests pass. The auto-learning cycle works end-to-end.
  </verify>
  <done>Auto-learning verified working. Database grows automatically with each new part identified.</done>
</task>


## Phase 4: Frontend — Show BreakerPro Price + Source Badge

<task type="auto">
  <n>Update Parts Logger frontend to show BreakerPro price and source</n>
  <files>parts-logger-frontend/index.html</files>
  <action>
    Update the existing Parts Logger frontend (HTML/CSS/JS) to display two new pieces of info for each logged part:

    1. **BreakerPro historical price** — shown alongside the eBay average price.
       - The `/lookup` response now includes `breakerpro_price` (number or null)
       - Display it as: "Your last: £22.99" next to the existing "Avg: £24.99"
       - If null (part not in BreakerPro history), just show the eBay average as before
       - Style: slightly muted text, smaller than the eBay average, to indicate it's historical reference not current market

    2. **Source badge** — small indicator showing where the identification came from
       - The `/lookup` response now includes `source` field: "database", "learned", or "claude"
       - "database" → small green dot or "DB" badge (high confidence)
       - "learned" → small amber dot or "AI" badge (previously learned from Claude)
       - "claude" → small blue dot or "AI" badge (live Claude lookup)
       - Keep it subtle — a small coloured dot or 2-letter badge next to the part description

    3. **Pre-fill "Your Price" field** — when BreakerPro price is available, pre-fill the editable price field with that value instead of the eBay average. The user can still override it.

    4. **Editable description** — the part description text must be tappable/editable on mobile:
       - Default state: description shows as normal text
       - Tap/click on description: transforms into an editable text input, pre-filled with current description
       - User types correction → taps away or hits enter
       - On blur/enter: if the text changed, send `POST /db-correct` to the backend with `{part_number, corrected_description, price}` (price is the current value in the price field, or null)
       - Show a brief "Saved ✓" confirmation flash so user knows the correction was stored
       - The corrected description replaces the original in the current session immediately
       - Keep it simple — no edit button needed, just make the text tappable. A subtle pencil icon or underline can hint that it's editable.
       - IMPORTANT: this must work well on mobile. Use a text input that's large enough to tap on a phone screen.

    DO NOT rewrite the entire frontend. Only modify:
    - The part card rendering function to show the BreakerPro price and source badge
    - The price pre-fill logic
    - Add minimal CSS for the source badge
  </action>
  <verify>
    1. Open the frontend in a browser
    2. Look up a part that's in the database — verify:
       - Description is correct
       - BreakerPro price shows as "Your last: £XX.XX"
       - Source badge shows green/DB
       - Price field is pre-filled with BreakerPro price
    3. Look up a part NOT in the database — verify:
       - Description comes from Claude
       - No BreakerPro price shown (or shows "New part")
       - Source badge shows blue/AI
       - Price field uses eBay average as before
  </verify>
  <done>Frontend displays BreakerPro historical price and source badge. Price field pre-fills from BreakerPro data when available.</done>
</task>


---


# Infrastructure Notes (for Claude Code reference)

## Where everything lives
- **Backend:** Hosted on Railway (`parts-logger-backend/`)
- **Frontend:** Hosted on Vercel (`parts-logger-frontend/`)
- **Database:** `vag_parts_db.json` sits in the `parts-logger-backend/` directory on Railway, alongside `main.py`
- **Phones:** Access the frontend via Vercel URL → frontend calls Railway backend API → backend reads database from local filesystem

## How the database grows
1. User looks up a part on their phone
2. Frontend calls `POST /lookup` on the Railway backend
3. Backend checks `vag_parts_db.json` in memory
4. If found → returns instantly, no API call
5. If NOT found → calls Claude API, returns result, AND writes the new mapping to `vag_parts_db.json` on Railway's filesystem
6. Next time anyone looks up that part → comes from database, no Claude call

## Deployment flow for this update
1. Run `build_db.py` locally with BreakerPro CSV export → generates `vag_parts_db.json`
2. Add new files to the GitHub repo: `vag_parts_db.json`, `vag_lookup.py`, `claude_prompt.py`, `breakerpro_parser.py`, `build_db.py`
3. Update `main.py` with new imports and lookup flow
4. Push to GitHub → Railway auto-deploys backend
5. Update `index.html` with price display + source badge
6. Push to GitHub → Vercel auto-deploys frontend
7. Test on phone

## Railway filesystem note
Railway containers are ephemeral — if the service restarts, filesystem changes (like auto-learned entries) could be lost. Two options:
- **Option A (simple):** Accept that learned entries may reset on redeploy. The core BreakerPro database is in the Git repo and always survives. Learned entries are a bonus.
- **Option B (robust):** Use a small SQLite database or Railway's persistent volume to store learned entries separately. More complex but data survives restarts.
- **Recommendation:** Start with Option A. The BreakerPro database covers 95% of parts. Learned entries are gravy. If it becomes an issue, switch to Option B later.

