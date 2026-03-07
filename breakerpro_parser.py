"""
BreakerPro CSV Parser — Pulled Apart Ltd
Parses BreakerPro CSV exports and extracts part number → description pairs.
Handles both raw BreakerPro format and pre-converted clean CSVs.
"""

import csv
import re
import os
import sys
import argparse
from collections import Counter


# ── Part number extraction ──────────────────────────────────────────────────

# Regex to pull part number from BreakerPro "Part Description" field
# Matches: "Part Number is 6R2 880 201 B" or "Part number is 03C 906 024 CN"
PART_NUM_RE = re.compile(
    r'[Pp]art\s*[Nn]umber\s*is\s+([A-Za-z0-9][A-Za-z0-9 ]{3,25})',
    re.IGNORECASE
)

# Paint code pattern: "Paint code is LA7W SILVER"
PAINT_CODE_RE = re.compile(
    r'[Pp]aint\s*[Cc]ode\s*is\s+([A-Z][A-Z0-9]{2,5})',
    re.IGNORECASE
)

# Junk text that BreakerPro appends to part numbers
JUNK_SUFFIXES = [
    'ThispartwillfitawiderangeofVAGcars',
    'Thispartwillfitawiderangeofvagcars',
    'ThiswillfitawiderangeofVAGcars',
    'FullyTestedGoodworkingorder',
    'FullyTestedGoodcondition',
]

# Engine/gearbox codes: 2-5 uppercase letters with NO digits
ENGINE_CODE_RE = re.compile(r'^[A-Za-z]{2,5}$')

# Valid VAG part number: must contain at least one digit AND at least one letter
VAG_PN_RE = re.compile(r'^(?=.*[0-9])(?=.*[A-Za-z])[A-Za-z0-9]{6,16}$')

# Middle group: first sequence of exactly 6 consecutive digits
MIDDLE_GROUP_RE = re.compile(r'(\d{6})')


def clean_part_number(raw: str) -> str | None:
    """Clean and normalise a raw part number string.
    Returns normalised part number or None if invalid."""
    if not raw or not raw.strip():
        return None

    pn = raw.strip()

    # Remove junk suffixes BreakerPro appends
    for junk in JUNK_SUFFIXES:
        idx = pn.find(junk)
        if idx == -1:
            idx = pn.lower().find(junk.lower())
        if idx > 0:
            pn = pn[:idx]

    # Strip spaces and uppercase
    pn = pn.replace(' ', '').upper()

    # Filter out too-short or too-long
    if len(pn) < 5 or len(pn) > 16:
        return None

    # Filter out N/A
    if pn in ('N/A', 'NA', 'NONE', 'TBC', 'TBA', ''):
        return None

    # Filter out engine/gearbox codes (only letters, no digits)
    if ENGINE_CODE_RE.match(pn):
        return None

    # Must look like a VAG part number (has both letters and digits)
    if not VAG_PN_RE.match(pn):
        return None

    return pn


def extract_middle_group(part_number: str) -> str | None:
    """Extract the 6-digit middle group from a normalised part number.
    E.g. '6J3837401AJ' → '837401', '5Q0407272C' → '407272'"""
    m = MIDDLE_GROUP_RE.search(part_number)
    return m.group(1) if m else None


def extract_part_number_from_description(desc: str) -> str | None:
    """Extract part number from BreakerPro Part Description field.
    E.g. 'Fully TestedGood conditionPart Number is 6R2 880 201 B' → '6R2880201B'"""
    m = PART_NUM_RE.search(desc)
    if not m:
        return None
    raw = m.group(1).strip()
    # Trim trailing lowercase/description text
    raw = re.sub(r'\s+[a-z].*$', '', raw)
    raw = re.sub(r'\s+$', '', raw)
    return clean_part_number(raw)


def extract_paint_code(desc: str) -> str | None:
    """Extract paint code from description. E.g. 'Paint code is LA7W SILVER' → 'LA7W'"""
    m = PAINT_CODE_RE.search(desc)
    return m.group(1).upper() if m else None


# ── CSV Parsing ─────────────────────────────────────────────────────────────

def detect_format(header_row: list[str]) -> str:
    """Detect CSV format: 'raw' for BreakerPro exports, 'converted' for clean CSVs."""
    header_lower = [h.strip().lower() for h in header_row]
    if 'part id' in header_lower and 'part description' in header_lower:
        return 'raw'
    if header_lower and header_lower[0] == 'part number':
        return 'converted'
    # Fallback: check for Part Name column
    if 'part name' in header_lower:
        return 'converted'
    return 'unknown'


def parse_raw_breakerpro(filepath: str) -> list[dict]:
    """Parse a raw BreakerPro CSV export file.
    Returns list of dicts with keys: part_number, description, price, make, model, year, paint_code"""
    results = []

    # Try different encodings
    for encoding in ('latin-1', 'utf-8', 'cp1252'):
        try:
            with open(filepath, 'r', encoding=encoding, errors='replace') as f:
                content = f.read()
            break
        except Exception:
            continue
    else:
        print(f"  WARNING: Could not read {filepath}")
        return results

    lines = content.splitlines()

    # Skip title row and blank lines to find header
    header_idx = None
    for i, line in enumerate(lines):
        if 'Part ID' in line and 'Part Name' in line:
            header_idx = i
            break

    if header_idx is None:
        print(f"  WARNING: No header row found in {filepath}")
        return results

    # Parse CSV from header onwards
    csv_text = '\n'.join(lines[header_idx:])
    reader = csv.DictReader(csv_text.splitlines())

    for row in reader:
        # Get part name (the breaker-style short description)
        part_name = (row.get('Part Name') or '').strip()

        # Skip "Breaking For Spares" rows
        if not part_name or 'breaking for spares' in part_name.lower():
            continue

        # Get description field (contains "Part Number is XXX")
        description = (row.get('Part Description') or '').strip()

        # Extract part number from description
        part_number = extract_part_number_from_description(description)

        # Also try extracting paint code
        paint_code = extract_paint_code(description)

        # Get price
        price_str = (row.get('Part Nett Sale') or '').strip()
        try:
            price = float(price_str) if price_str else None
        except ValueError:
            price = None

        # Get vehicle info
        make = (row.get('Make') or '').strip()
        model = (row.get('Model') or '').strip()
        year = (row.get('Year') or '').strip()
        vehicle = f"{make} {model}".strip() if make else None

        if part_number:
            results.append({
                'part_number': part_number,
                'description': part_name,
                'price': price,
                'make': make,
                'model': model,
                'year': year,
                'vehicle': vehicle,
                'paint_code': paint_code,
            })
        elif paint_code:
            # Part with paint code only (e.g. BONNET)
            results.append({
                'part_number': None,
                'description': part_name,
                'price': price,
                'make': make,
                'model': model,
                'year': year,
                'vehicle': vehicle,
                'paint_code': paint_code,
            })

    return results


def parse_converted_csv(filepath: str) -> list[dict]:
    """Parse a pre-converted clean CSV (Part Number, Make, Model, Part Name, Paint Code)."""
    results = []

    for encoding in ('utf-8', 'latin-1', 'cp1252'):
        try:
            with open(filepath, 'r', encoding=encoding, errors='replace') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    raw_pn = (row.get('Part Number') or '').strip()
                    part_name = (row.get('Part Name') or '').strip()
                    make = (row.get('Make') or '').strip()
                    model = (row.get('Model') or '').strip()
                    paint_code = (row.get('Paint Code') or '').strip() or None

                    if not part_name or 'breaking for spares' in part_name.lower():
                        continue

                    pn = clean_part_number(raw_pn)

                    if pn:
                        results.append({
                            'part_number': pn,
                            'description': part_name,
                            'price': None,
                            'make': make,
                            'model': model,
                            'year': None,
                            'vehicle': f"{make} {model}".strip() if make else None,
                            'paint_code': paint_code,
                        })
            break
        except Exception:
            continue

    return results


def parse_csv(filepath: str) -> list[dict]:
    """Auto-detect CSV format and parse accordingly."""
    # Read first few lines to detect format
    for encoding in ('latin-1', 'utf-8', 'cp1252'):
        try:
            with open(filepath, 'r', encoding=encoding, errors='replace') as f:
                lines = [f.readline() for _ in range(10)]
            break
        except Exception:
            continue
    else:
        print(f"  ERROR: Cannot read {filepath}")
        return []

    # Find header line
    for line in lines:
        if 'Part ID' in line and 'Part Name' in line:
            return parse_raw_breakerpro(filepath)
        if line.strip().startswith('Part Number,'):
            return parse_converted_csv(filepath)

    # If we can't detect, try both
    print(f"  WARNING: Unknown format for {filepath}, trying raw parser...")
    result = parse_raw_breakerpro(filepath)
    if not result:
        result = parse_converted_csv(filepath)
    return result


def parse_directory(dirpath: str) -> list[dict]:
    """Parse all CSVs in a directory."""
    all_results = []
    csv_files = sorted([f for f in os.listdir(dirpath) if f.lower().endswith('.csv')])
    print(f"Found {len(csv_files)} CSV files in {dirpath}")

    for filename in csv_files:
        filepath = os.path.join(dirpath, filename)
        print(f"  Parsing: {filename}...")
        results = parse_csv(filepath)
        print(f"    -> {len(results)} parts extracted")
        all_results.extend(results)

    return all_results


# ── Deduplication & Group Building ──────────────────────────────────────────

def deduplicate(parts: list[dict]) -> dict:
    """Deduplicate parts by part number. Returns {normalised_pn: {description, price, vehicle}}.
    Keeps the most common description and latest price for each part number."""
    pn_descs = {}   # pn → [description, ...]
    pn_prices = {}  # pn → [price, ...]
    pn_vehicles = {}  # pn → [vehicle, ...]

    for p in parts:
        pn = p['part_number']
        if not pn:
            continue

        if pn not in pn_descs:
            pn_descs[pn] = []
            pn_prices[pn] = []
            pn_vehicles[pn] = []

        pn_descs[pn].append(p['description'])
        if p['price'] is not None and p['price'] > 0:
            pn_prices[pn].append(p['price'])
        if p['vehicle']:
            pn_vehicles[pn].append(p['vehicle'])

    result = {}
    for pn, descs in pn_descs.items():
        # Pick most common description
        desc_counter = Counter(descs)
        best_desc = desc_counter.most_common(1)[0][0]

        # Pick latest/most common price
        prices = pn_prices.get(pn, [])
        price = prices[-1] if prices else None

        # Pick first vehicle
        vehicles = pn_vehicles.get(pn, [])
        vehicle = vehicles[0] if vehicles else None

        result[pn] = {
            'description': best_desc,
            'price': price,
            'vehicle': vehicle,
        }

    return result


def build_groups(exact_entries: dict) -> dict:
    """Build middle-group → description mappings from exact entries.
    Returns {group_code: {description, avg_price}}."""
    group_descs = {}   # group → [description, ...]
    group_prices = {}  # group → [price, ...]

    for pn, entry in exact_entries.items():
        group = extract_middle_group(pn)
        if not group:
            continue

        if group not in group_descs:
            group_descs[group] = []
            group_prices[group] = []

        # Strip side designations from description for group-level mapping
        desc = entry['description']
        desc = re.sub(r'\s*\((?:FRONT\s+)?(?:REAR\s+)?(?:DRIVER|PASSENGER)\s+SIDE\)', '', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\s*\((?:NSF|OSF|NSR|OSR|NS|OS)\)', '', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\s+(?:NSF|OSF|NSR|OSR|NS|OS)$', '', desc, flags=re.IGNORECASE)
        desc = desc.strip()
        group_descs[group].append(desc)

        if entry.get('price') is not None:
            group_prices[group].append(entry['price'])

    result = {}
    for group, descs in group_descs.items():
        desc_counter = Counter(descs)
        best_desc = desc_counter.most_common(1)[0][0]

        prices = group_prices.get(group, [])
        avg_price = round(sum(prices) / len(prices), 2) if prices else None

        result[group] = {
            'description': best_desc,
            'avg_price': avg_price,
        }

    return result


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Parse BreakerPro CSV exports and extract part number → description pairs'
    )
    parser.add_argument('files', nargs='*', help='CSV file(s) to parse')
    parser.add_argument('--dir', help='Directory containing CSV files')
    parser.add_argument('--output', default='parsed_parts.json',
                        help='Output JSON file (default: parsed_parts.json)')
    args = parser.parse_args()

    all_parts = []

    if args.dir:
        all_parts.extend(parse_directory(args.dir))

    for filepath in (args.files or []):
        print(f"Parsing: {filepath}...")
        results = parse_csv(filepath)
        print(f"  → {len(results)} parts extracted")
        all_parts.extend(results)

    if not all_parts:
        print("No parts found. Provide CSV files or use --dir.")
        sys.exit(1)

    print(f"\nTotal raw parts extracted: {len(all_parts)}")

    # Deduplicate
    exact = deduplicate(all_parts)
    print(f"Unique part numbers: {len(exact)}")

    # Build groups
    groups = build_groups(exact)
    print(f"Unique middle groups: {len(groups)}")

    # Save output
    import json
    output = {
        'exact': exact,
        'groups': groups,
        'total_raw': len(all_parts),
    }

    with open(args.output, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\nSaved to {args.output}")


if __name__ == '__main__':
    main()
