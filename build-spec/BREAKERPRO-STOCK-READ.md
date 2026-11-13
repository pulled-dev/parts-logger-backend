# BreakerPro stock read: investigation findings

Status: investigation only (2026-10-27). Nothing syncs or deploys yet. No BreakerPro data was changed.

## Verdict

Yes: we can read live stock from BreakerPro's own SQL Server database, read-only.
It is BreakerPro's live store (no cache layer), so a change made in BreakerPro is
visible as soon as BreakerPro saves it. One query answers "do we have part X from
VEHnnn, and is it listed / sold / on the shelf".

## Access

| Item | Value |
|---|---|
| Server | `.\SQLEXPRESS` (SQL Server 2022 Express, mixed-mode auth, on this PC) |
| Database | `BreakerPro` (~715 MB) |
| Login | `breakerpro_readonly`: SQL login, `db_datareader` + `db_denydatawriter` on BreakerPro only, no server roles |
| Password | `C:\Users\ACER\.secrets\breakerpro-readonly\password.txt` (ACL: ACER only). Pass via `SQLCMDPASSWORD` env / driver param, never on a command line |
| Rollback | `USE BreakerPro; DROP USER breakerpro_readonly; USE master; DROP LOGIN breakerpro_readonly;` |

Verified: `fn_my_permissions` = CONNECT + SELECT only; not sysadmin/db_owner.
`BUILTIN\Users` has a server login but no way into BreakerPro (guest has no CONNECT).

## Schema: what matters

All BreakerPro columns are `nvarchar` (prices, dates-as-text included) except the
`*Date` audit columns. No primary keys or foreign keys; `Part ID` is unique in practice.
Column names contain spaces, so always bracket them: `[Part ID]`.

### `dbo.Stock`: everything currently in stock (5,134 rows, 168 MB)
One table holds **both vehicles and parts**:

- **Vehicle row:** `[Part Type] = 'Breaking For Spares'` (162 rows). It is also the eBay "breaking for spares" ad.
- **Part rows:** all other Part Types (INTERIOR, MECHANICAL, BODY PARTS, ENGINE BAY, LIGHTS, GLASS, WHEELS): 4,972 parts across 170 vehicles.

| Need | Column(s) |
|---|---|
| Part key | `[Part ID]` (e.g. `1659-070726-070016-361`) |
| Vehicle link | `[Vehicle ID]` (e.g. `VH-120326-160137-01`): the real join key |
| Stock ref VEHnnn | `[Stock Reference]`: **mixed case** (`veh113` / `VEH113`), compare with `UPPER()` |
| Part name / type | `[Part Name]`, `[Part Type]` (free text, e.g. `DOOR WINDOW (FRONT PASSENGER SIDE)`) |
| Location | `[Part Location]` (e.g. `C-2-7`, `DD PF15`, `cc top shelf`). **44% blank** |
| Price | `Ebay_FixedPrice` (listed price), else `[Part Nett Sale]` |
| eBay item ID | `Ebay_Number`. **Cleared when a listing is ended** |
| eBay state | `Ebay_Status` (free-text API response), `Ebay_StatusDate`, `EbayListedDate` |
| Custom label / SKU | No separate column. BreakerPro uses `[Part ID]` as the SKU |
| Batch | `Marketting` / `Ebay_BatchName` (e.g. `EBAY_RELISTED_210926`) |

### `dbo.Invoices`: sales (78,529 rows, 129 MB)
Each invoice is many rows keyed by `[Invoice ID]` (e.g. `Pul5354`), one per `[Transaction Type]`:

- **`PART`:** a full snapshot of the Stock row at sale time (same columns). `[Transaction Amount]` = net of VAT.
- **`PAYMENT`:** gross paid (negative number).
- **`STATUS`:** pick/pack/dispatch notes.
- **`NOTES`:** eBay item ID + buyer's vehicle.
- Other types: `PAYPAL DETAILS`, `DELIVERY`, and so on.

### `dbo.StockDeleted`: parts removed without a sale (873 rows)
Same columns as Stock plus `DeletedDate`, `DeletedBy`. The reasons are free text, and
mostly `Deleted Manually by Mobile App` (651); others are `scrap`, `damaged`, `faulty`, `ERROR`, `sold` and blank.

### `dbo.AuditHistory`: event log (192,368 rows)
`[DateTime]`, `[User]`, `PartID`, `Ebay_Number`, `[Event]`, `Field`, `ValueCurrent`, `ValueNew`, `Details`.
Useful events: `Order`, `Invoice` (User `AUTO` = automatic eBay/PayPal import), `Ebay Ended`, `Ebay Listed`,
`Deleted***<user>`, `Mobile App - Manage Part`.

No separate vehicles table: our parts-logger `vehicles` API remains the vehicle master.

## How a part's status is derived

| Status | Rule |
|---|---|
| **LISTED** | in `Stock`, `Ebay_Number` set, `Ebay_Status` not a dead-listing failure |
| **IN STOCK - LISTING DEAD** | in `Stock`, `Ebay_Number` set, but `Ebay_Status` starts `Failed: This listing has been removed…` (98) or `Failed: This item cannot be accessed…` (15). eBay pulled it; the part is physically here |
| **ON SHELF (UNLISTED)** | in `Stock`, `Ebay_Number` empty |
| **SOLD** | not in `Stock`, has an `Invoices` row with `[Transaction Type]='PART'` |
| **REMOVED** | not in `Stock`/`Invoices`, in `StockDeleted` (scrapped / damaged / manually deleted) |

How a sale flows: BreakerPro's `AUTO` user imports the eBay/PayPal order, writes `Order` and
`Invoice` audit events and the `Invoices` rows, then **deletes the part row from `Stock`**.
Of 4,373 invoiced parts, only 33 are still in Stock. Those are edge cases (returns, cancellations or
re-stocks) that the query reports by their Stock state.

## The query

Inputs: `@ref` (VEHnnn, any case), `@part` (LIKE pattern on part name, `'%'` = all),
`@part_id` (exact Part ID or NULL).

```sql
DECLARE @ref nvarchar(50) = 'VEH113', @part nvarchar(200) = '%door window%', @part_id nvarchar(100) = NULL;

WITH veh AS (   -- every Vehicle ID that has ever carried this stock ref
  SELECT [Vehicle ID] FROM Stock        WHERE UPPER(LTRIM(RTRIM([Stock Reference]))) = UPPER(@ref)
  UNION SELECT [Vehicle ID] FROM StockDeleted WHERE UPPER(LTRIM(RTRIM([Stock Reference]))) = UPPER(@ref)
  UNION SELECT [Vehicle ID] FROM Invoices WHERE [Transaction Type] = 'PART'
                                           AND UPPER(LTRIM(RTRIM([Stock Reference]))) = UPPER(@ref)
),
inv_status AS (
  SELECT [Invoice ID], [Transaction Details] AS dispatch_status,
         ROW_NUMBER() OVER (PARTITION BY [Invoice ID]
                            ORDER BY [Transaction Date] DESC, TRY_CONVERT(int,[Transaction No]) DESC) rn
  FROM Invoices WHERE [Transaction Type] = 'STATUS'
),
inv_paid AS (
  SELECT [Invoice ID], -SUM(TRY_CONVERT(decimal(10,2), [Transaction Amount])) AS paid
  FROM Invoices WHERE [Transaction Type] = 'PAYMENT' GROUP BY [Invoice ID]
),
rows_ AS (
  SELECT s.[Part ID], UPPER(s.[Stock Reference]) AS ref, s.[Part Name],
         CASE WHEN s.[Part Type] = 'Breaking For Spares' THEN 'VEHICLE RECORD'
              WHEN ISNULL(s.Ebay_Number,'') <> ''
                   AND (CAST(s.Ebay_Status AS nvarchar(60)) LIKE 'Failed: This listing has been removed%'
                     OR CAST(s.Ebay_Status AS nvarchar(60)) LIKE 'Failed: This item cannot be accessed%')
                   THEN 'IN STOCK - LISTING DEAD'
              WHEN ISNULL(s.Ebay_Number,'') <> '' THEN 'LISTED'
              ELSE 'ON SHELF (UNLISTED)' END AS status,
         s.[Part Location] AS location, COALESCE(NULLIF(s.Ebay_FixedPrice,''), s.[Part Nett Sale]) AS price,
         s.Ebay_Number, CONVERT(varchar(16), s.EbayListedDate, 120) AS since, NULL AS invoice, NULL AS detail
  FROM Stock s WHERE s.[Vehicle ID] IN (SELECT [Vehicle ID] FROM veh)
  UNION ALL
  SELECT i.[Part ID], UPPER(i.[Stock Reference]), i.[Part Name], 'SOLD',
         i.[Part Location], CAST(p.paid AS nvarchar(20)), i.Ebay_Number,
         CONVERT(varchar(10), i.[Invoice Date], 120) + ' ' + CONVERT(varchar(5), i.[Invoice Time], 108),
         i.[Invoice ID], st.dispatch_status
  FROM Invoices i
  LEFT JOIN inv_status st ON st.[Invoice ID] = i.[Invoice ID] AND st.rn = 1
  LEFT JOIN inv_paid  p  ON p.[Invoice ID]  = i.[Invoice ID]
  WHERE i.[Transaction Type] = 'PART' AND i.[Vehicle ID] IN (SELECT [Vehicle ID] FROM veh)
    AND NOT EXISTS (SELECT 1 FROM Stock s WHERE s.[Part ID] = i.[Part ID])
  UNION ALL
  SELECT d.[Part ID], UPPER(d.[Stock Reference]), d.[Part Name], 'REMOVED',
         d.[Part Location], d.[Part Nett Sale], d.Ebay_Number, CONVERT(varchar(16), d.DeletedDate, 120),
         NULL, ISNULL(NULLIF(d.DeletedReason,''), '(no reason)') + ' by ' + ISNULL(d.DeletedBy,'?')
  FROM StockDeleted d
  WHERE d.[Vehicle ID] IN (SELECT [Vehicle ID] FROM veh)
    AND NOT EXISTS (SELECT 1 FROM Stock s WHERE s.[Part ID] = d.[Part ID])
    AND NOT EXISTS (SELECT 1 FROM Invoices i WHERE i.[Transaction Type]='PART' AND i.[Part ID] = d.[Part ID])
)
SELECT * FROM rows_
WHERE [Part Name] LIKE @part AND (@part_id IS NULL OR [Part ID] = @part_id)
ORDER BY CASE status WHEN 'VEHICLE RECORD' THEN 0 ELSE 1 END, status, [Part Name];
```

For the bot, "do we have it?" = any row with status `LISTED`, `ON SHELF (UNLISTED)` or
`IN STOCK - LISTING DEAD`. `SOLD` / `REMOVED` rows explain why we don't.

## Proof with real data (pending Naveed's check in BreakerPro screens)

| # | Case | Part ID | DB says |
|---|---|---|---|
| 1 | Listed | `1659-070726-070016-361` | VEH113 TAILGATE STRUTS (PAIR): LISTED, loc `C-2-7`, £14.99, eBay `278395879637`, listed 2026-09-21 16:12 |
| 2 | Sold recently | `1659-210826-145901-21` | VEH121 SEAT BELT BUCKLE FRONT PASSENGER SIDE: SOLD, invoice `Pul5354` 2026-10-27 15:10, £17.00 paid, eBay `940037909458`, status Pending Picking, was at `C-3-8` |
| 3 | On shelf, unlisted | `1659-240926-070014545-341` | VEH126 FUEL/PETROL FLAP: ON SHELF (UNLISTED), loc `AA-2-1`, £39.99, added 2026-09-24 |

**VEH113** (Vehicle ID `VH-120326-160137-01`): 1 vehicle record + 62 parts ever logged:
26 LISTED, 1 IN STOCK - LISTING DEAD (KNEE A BAG, eBay removed it for policy), 2 ON SHELF
(DRIVESHAFT passenger front, WINDOW MOTOR driver front), 19 SOLD, 14 REMOVED.
Bot-style example `VEH113` + `%door window%`: 3 listed (front passenger, rear driver,
rear passenger); front driver SOLD 2026-08-07 (`Pul4692`).

## Freshness

- The DB *is* BreakerPro's store. The latest audit row was under 1 minute old when queried. So a part
  BreakerPro marks sold or deleted disappears from `Stock` immediately (all 15 sales
  from 26–27 Oct verified gone from Stock).
- Sales are imported by BreakerPro's `AUTO` user at irregular times (00:00, 03:07, 06:05, 10:22…).
  It looks payment-triggered, not a fixed batch. The order ID embeds its creation time
  (`ORAUTO271026150939` → 15:09:39, audit-logged 15:10).
- **Not yet measured:** the lag between the eBay sale and BreakerPro importing it. The DB has no
  eBay order timestamp. (The `DEL…` token in delivery details is *not* the sale time; checked
  and ruled out.) Measure it by comparing eBay order times (Trading/Fulfillment API) with
  `Invoice` audit times, or by watching one live sale.

## Gaps

1. **Location is blank on 44% of parts** (2,194 / 4,972). The bot can't promise shelf positions.
2. **eBay price is blank on 770 parts.** The query falls back to `[Part Nett Sale]` (never blank).
3. **REMOVED is ambiguous.** "Deleted Manually by Mobile App" covers scrap, damage *and* possibly
   off-eBay or counter sales. Treat it as "not available", reason unknown.
4. **225 parts have no vehicle row** in Stock (e.g. `veh1`, `veh109`, `VEH23`). The vehicle row was sold or deleted;
   parts still resolve by `[Stock Reference]` through the `veh` CTE.
5. **Duplicate vehicle row:** VEH14 has 2 vehicle rows (same Vehicle ID). This is harmless for the query.
6. ~~Dispatch status looks stale~~ **Corrected in part 2 (b).** "Pending Packing" means *already
   picked, waiting to be packed*. Only "Pending Picking" means not yet picked.
7. **33 invoiced parts still in Stock.** Part 2 (e) confirmed all 33 are voided or cancelled sales put back into stock; Stock state wins.
8. **Part names are free text.** Matching "passenger door window" needs fuzzy/keyword matching
   on our side (`DOOR WINDOW (FRONT PASSENGER SIDE)`, `WINDOW REGULATOR/MECH ELECTRIC (FRONT PAS…`).
9. **No dedicated custom-label column.** `[Part ID]` is the SKU. Confirm it's what eBay listings
   carry as Custom Label before relying on it for eBay → part lookups (`Ebay_Number` works today).

## Risks

- **Schema changes on BreakerPro updates.** No views or contracts; the tables are BreakerPro's internals.
  Mitigation: a startup self-check that the columns used here exist (query `sys.columns`)
  and fail closed ("can't confirm stock, a human will reply") rather than guessing.
- **"Sold = row deleted" is an implementation detail.** If a BreakerPro update starts
  soft-deleting instead, an absent row could be misread. The self-check should also assert
  that recent `Invoice` audit events have no matching Stock row.
- **Load on the shop PC.** The full query reads Stock (168 MB with HTML descriptions) and
  Invoices (129 MB). It's fine at bot volumes; select only the needed columns and don't poll in a tight loop.
  SQL Express caps at 1 socket / ~1.4 GB buffer, shared with BreakerPro.
- **Locking.** Reads take shared locks. Use short queries; `READ UNCOMMITTED` / `NOLOCK` would
  avoid blocking BreakerPro's writes at the cost of occasional dirty reads. Decide before building.
- **Exposure.** The DB is local. The cloud bot will need a path in (a local agent pushing a snapshot,
  or a tunnel). Never open SQL Server to the internet. The read-only login limits damage but
  still exposes customer names and addresses in `Invoices`/`Customers`.
- **Password strength/rotation.** 32-char random password with CHECK_POLICY on and no expiry. Rotate if the PC is
  shared or the secrets folder is copied anywhere.

---

# Phase 1 discovery part 2

Run 2026-10-27 as `breakerpro_readonly`, SELECT only. Every query took under 1.5s.
Samples are anonymised: part, invoice and staff identifiers only, with no customer data.

## a) Date added: `Stock.[AddedDate]` (smalldatetime) exists

- It is set on **every** in-stock part (4,972 / 4,972, 0 null), ranging from 2024-10-04 to today.
- The Part ID's second segment **is** a DDMMYY logging date (`1659-070726-…` = 07/07/26).
  It matches `AddedDate` on 4,972 / 4,972 parts (100%, same day).
- **Independent check:** `AuditHistory` "Added Stock" / "Mobile App - Added Stock" events
  (from 2024-12-07) cover 831 in-stock parts. All 831 (100%) match the Part ID date on the same day.
  In a 20-part spread sample, 4 had an audit event and all matched to the minute. The other 16
  predate the audit log or were added through other paths.
- Samples:

  | Part ID | DDMMYY | AddedDate | Audit added |
  |---|---|---|---|
  | `1659-081026-190707164-21` | 081026 | 2026-10-08 19:07 | 2026-10-08 19:07 |
  | `1659-150926-141004228-141` | 150926 | 2026-09-15 14:10 | 2026-09-15 14:10 |
  | `1659-041025-121107-21` | 041025 | 2025-10-04 12:11 | (none) |

- **Use `AddedDate` for ageing** (Section 6). The Part ID segment is a fallback.

## b) What invoice lines keep

Each `[Invoice ID]` (prefix `Pul`, 4,355 invoices) has one row per `[Transaction Type]`:
STATUS 8,129 · DELIVERY DETAILS 7,806 · PAYMENT 4,447 · PART 4,426 · BALANCE/CURRENCY/CUSTOMER/SALESMAN 4,355 each ·
MARGINAL VAT 4,001 · DELIVERY, PART SHIPPING OPTION, PAYPAL, PAYPAL ACCOUNT, PAYPAL DETAILS and SALE FEE COMMISSION 3,903 each ·
ESTIMATED DELIVERY 3,868 · VAT 3,760 · NOTES 919 · MARKED 140 · CONSIGNMENT TRACKING 96 · VOID 96 ·
MARGINAL VAT - VEHICLE 3.

| Needed | Kept? | Where |
|---|---|---|
| Part ID | yes (4,426/4,426) | PART line `[Part ID]` |
| VEH ref | mostly: **225 blank** (MISC / non-stock sales) | PART line `[Stock Reference]` |
| Part name | yes | PART line `[Part Name]` |
| Sale price | yes. Net of VAT on the PART line `[Transaction Amount]` (2 unparseable); gross = −PAYMENT amount | PART / PAYMENT |
| Sale date | yes | `[Invoice Date]` (date) + `[Invoice Time]` (1900-01-01 hh:mm) |
| Picking status | yes | STATUS lines `[Transaction Details]` |
| Channel | **no column.** Derived, see below | SALESMAN + PAYMENT + PAYPAL lines |
| eBay item no. | 3,991 of 4,426 | PART line `Ebay_Number` |

Samples (PART lines): `Pul5348` 2026-10-27 `1659-311224-171212-1` VEH14 FUEL PUMP (IN TANK) £22.49 net ·
`Pul5349` 2026-10-27 `1659-091125-070422-401` VEH63 REAR/TAIL LIGHT (DRIVER SIDE) £24.99 ·
`Pul5350` 2026-10-27 `1659-060526-095008-141` VEH90 AUTOMATIC GEAR SHIFTER SELECTOR £20.83.

**Picking status values** (text before ` - `; the staff note after it is dropped):

| Status | Rows |
|---|---|
| PENDING PACKING | 3,771 |
| MARK AS PICKED BY: KAY | 2,006 |
| MARK AS PICKED BY: NAVEED | 1,617 |
| PENDING DISPATCH | 277 |
| MARK AS PICKED AND PACKED BY: NAVEED | 268 |
| MARK AS DISPATCHED BY: EBAY | 111 |
| PENDING PICKING | 60 |
| MARK AS PACKED BY: NAVEED | 10 |
| MARKED AS ASSIGNED | 4 |
| MARK AS PACKED BY: KAY | 3 |
| MARK AS DISPATCHED BY: KAY / NAVEED | 1 / 1 |

How to read it: each stage adds a STATUS line. `[Transaction No]` embeds the time
(`TR-150626-07040671` = 15/06/26 07:04:06), and "Mark as Picked By: Kay" is followed by "Pending Packing".
So **Pending Packing = picked**, and only an invoice whose sole stage is **Pending Picking** is unpicked.
Most invoices have 2 STATUS lines (3,682). 207 have 1, 182 have 3–5, and **284 have none** (older or
manual invoices, 2024-10-02 → 2026-10-24).

**Channel / customer type:** there is no channel or customer-type column on Invoices or Customers.
`Customers` has `CreditLimit`, `CustomerVATExempt` and `SalesPerson`, but none is set on any of
7,645 customers, so there is no trade marker. The channel can be derived cleanly:

| Raised by (SALESMAN) | Payment | PayPal import line | Invoices | = Channel |
|---|---|---|---|---|
| INVOICED BY AUTO | Pay Pal | AUTOMATIC PAYPAL IMPORT | 3,903 | **eBay** |
| INVOICED BY NAVEED MOHAMMED / ADMIN | Cash | none | 448 | **Counter** |
| staff | none (refund only) | none | 4 | voided |

Trade sales, if any, are indistinguishable from counter sales.

## c) Purchase cost / date / supplier on vehicle rows: effectively none

Across 162 vehicle rows: `[Total Nett Cost]` = 0.00 on all 162, `[Part Nett Cost]` > 0 on 0,
`Originator` blank on all, `RECMETHOD` blank on all. `[On-Site Date]` is set on all 162 (text, `dd/mm/yy`).
`CollectionDate`, `DepollutionDate` and `CodDate` are set on 6 each. `V5REC` is set on 162.
There's no supplier field. Purchase cost has to come from elsewhere (e.g. our vehicles API).
Samples: VEH171 cost 0.00 on-site 17/10/26 · VEH170 0.00, 17/10/26 · VEH169 0.00, 12/10/26.

## d) Who created / removed rows: yes

| Column | Set | Values |
|---|---|---|
| `Stock.AddedBy` | 4,969/4,972 | MOBILEAPP-MEENA 3,345 · MOBILEAPP-KAY 632 · MOBILEAPP-NAVEED 622 · NAVEED MOHAMMED 251 · **MOBILEAPP- (no user) 79** · ADMIN 40 · blank 3 |
| `Stock.UpdatedBy` | 4,625/4,972 | (last editor) |
| `StockDeleted.DeletedBy` | 873/873 | KAY 460 · NAVEED MOHAMMED 210 · NAVEED 191 · ADMIN 12 |
| `StockDeleted.AddedBy` | 872/873 | original creator is kept |

Samples (StockDeleted): `1659-131026-140509592-121` VEH155 DOOR BARE (REAR PASSENGER SIDE), KAY 2026-10-27 ·
`1659-110526-121503-21` VEH51 ENGINE ECU, NAVEED 2026-10-27 · `1659-290926-070046794-1361` VEH164 HEADLIGHT (DRIVER SIDE), KAY 2026-10-27.

## e) Returns / credit notes: voids and refunds on the original invoice

There are no credit-note documents (`Statements` is empty) and no return/refund/credit events in AuditHistory.
Cancellations and refunds are lines on the **same `[Invoice ID]`**, which links them to the part
through that invoice's PART line (the VOID line itself has no Part ID):

| Line | Detail | Invoices |
|---|---|---|
| VOID | Ebay CANCEL | 65 |
| VOID | Pay Pal | 24 |
| VOID | Cash | 4 |
| VOID | Ebay REFUND | 2 |
| VOID | PAYMENT REFUND BY NAVEED MOHAMMED | 1 |
| PAYMENT | EBAY CANCEL / PAYMENT REFUND BY NAVEED / EBAY REFUND | 65 / 27 / 2 |

What happened to the parts on voided or refunded invoices (94 part lines): **33 back in Stock**
(these are exactly the 33 "invoiced but in stock" parts), 9 in StockDeleted, 52 gone (refund without return).
Samples: `PUL5328` 2026-10-24 Ebay CANCEL −£12.19 → `1659-040926-091405420-221` back in Stock ·
`PUL5273` 2026-10-19 Ebay CANCEL −£638.52 → `1659-200626-070119-501` back in Stock ·
`PUL5169` 2026-10-08 Ebay CANCEL −£16.42 → `1659-230726-175606-181` back in Stock.

## f) StockDeleted by reason, last 6 months

Only two reason values were used in this period:

| Month | Deleted Manually by Mobile App | Last listed price total* | (blank) |
|---|---|---|---|
| 2026-04 | 52 | £5,737.48 | 1 |
| 2026-05 | 40 | £4,649.60 | 1 |
| 2026-06 | 58 | £4,918.45 | 9 |
| 2026-07 | 123 | £8,165.81 | 9 |
| 2026-08 | 91 | £8,986.70 | 4 |
| 2026-09 | 93 | £10,459.08 | 3 |
| 2026-10 | 67 | £8,514.80 | 3 |

\*Label: **mixed yard sales/damaged, pre-cutover.** "Deleted Manually by Mobile App" covers both
yard sales and damaged parts and is deliberately not split historically. The price is the last stored
`Ebay_FixedPrice`, else `[Part Nett Sale]`, and is set on every row. Cutover is 2026-10-28
(`EXC_MOBILE_REMOVAL_CUTOVER` in `.env`). The older reasons (`scrap`, `damaged`, `faulty`, `sold`, `ERROR`,
`test`) all predate April 2026. Samples: see d).

## g) The 225 orphan parts (in Stock, no vehicle row)

| Explanation | Vehicles | Parts |
|---|---|---|
| No vehicle row anywhere (Stock, StockDeleted, Invoices) | 8 | 188 |
| Vehicle row sold, i.e. **the £4.99 "breaking for spares" ad was bought**, not the car | 1 | 37 |
| Vehicle row in StockDeleted | 0 | 0 |

The 8 "nowhere" vehicles are VEH1, VEH2, VEH3, VEH19, VEH20, VEH21, VEH22 and VEH23. They are early mobile-app
vehicles (`VH-MOBVHC2024…`, Oct–Dec 2024) with 57–241 audit rows each but no deletion event,
so they most likely never had a vehicle row. VEH109 (`Pul4416`, 2026-07-11, £4.16 net) is the one whose
vehicle row was sold. VEH140's ad was also bought (`Pul4364`), but it has no orphan parts left.

---

# Exceptions report (`scripts/exceptions_report.py`)

`python scripts/exceptions_report.py [--ref VEH113]` writes `reports/exceptions_YYYY-MM-DD.csv` + `.html`
(git-ignored; nothing leaves the PC). Thresholds are in `.env` (template: `.env.example`).

Safeguards: SELECT-only SQL with `?` parameters; read-only ODBC intent and autocommit;
refuses any login that can write; 5s per-query timeout; every `[bracketed]` column must be on an
allowlist (no customer, delivery, PayPal or note columns) **and** exist in the live schema, otherwise it
stops before writing anything. The connection closes in `finally`.

First run, 2026-10-27 ~16:30 (0.4s total, slowest query 0.08–0.12s):

| # | Section | Count |
|---|---|---|
| 1 | Sold but not picked (> 24h, voids excluded) | 61 lines / 58 invoices, oldest 2025-07-23 |
| 2 | Dead listings (98 policy-removed, 2 deleted on eBay, 36 last call failed) | 136 |
| 3 | On the shelf, unlisted | 841 |
| 4 | No shelf location | 2,194 (44.1%) |
| 5 | Priced under £8 | 65 |
| 6 | Aged > 180 days | 2,251, shelf value £115,426.73 |
| 7 | Orphan parts | 225 |
| 8 | Live eBay listing with no Stock row | TODO (Trading API GetMyeBaySelling / GetSellerList, or Sell Feed LMS_ACTIVE_INVENTORY_REPORT; no new credentials yet) |
| 9 | Mobile-app deletions, last 7 days | 17 (12 flagged £30+, £2,718.30) |
| 10 | Aged × location × listing grid + probable phantom stock | 174 phantom parts, £7,662.18, 66 VEH refs |
| 11 | Policy-removal pattern | 98 parts in 14 groups, all airbags |

Cross-checks: listed 3,995 + dead/error 136 + unlisted 841 = 4,972 in-stock parts.
VEH113 = 26 listed / 1 dead / 2 unlisted (matches part 1).
Section 1 is probably mostly stale: 58 "Pending Picking" invoices going back to July 2025 that
were never marked picked. Worth one pass to clear them in BreakerPro so the section becomes useful.

## Sections 10–11 (added 2026-10-27)

### 10. Aged × location × listing

A grid of every in-stock part (vehicle rows excluded): one cell per age band × located × listing state,
with count and total price in each.

- **Age bands:** by `AddedDate`, `DATEDIFF(day, AddedDate, GETDATE())` = <30, 30–90, 90–180, 180–365, >365.
- **Location:** located = non-blank `[Part Location]`.
- **Listing:** listed / unlisted / dead. "Dead" = item number + any `Failed…` eBay status (the same set as Section 2).
- One `GROUP BY` query (0.03s). The cells always sum to the in-stock count (4,972 on 2026-10-27, £303,943).

**Probable phantom stock** = age ≥ 180 days AND no location AND (unlisted OR dead).
It is grouped by VEH ref and by `AddedBy`, sorted by VEH ref number then price descending.
First run: **174 parts, £7,662.18, 66 VEH refs**, of which 110 unlisted, 59 policy-removed and 5 eBay errors.
Top by value: VEH81 (12, £1,080.88), VEH9 (4, £979.96), VEH50 (2, £489.98), VEH84 (2, £424.98), VEH31 (2, £339.98).

Observation: only 30 of the 554 parts logged in the last 30 days have a location. Most unlocated stock
is new and listed, so it's a logging habit rather than old stock.

**Crushed cross-check: TODO.** Parts Logger's yard status (`on_yard`/`stripped`/`crushed`) lives only
on the Railway API. The local `vehicles.db` is a 2-row test copy without a status column.

### 11. Policy-removal pattern

The Section 2 rows with `Failed: This listing has been removed…` (98 on 2026-10-27), grouped by a simple
keyword key (`part_group()` in the script):
- lowercase;
- airbag spellings merged (`air bag`, `a bag`, `abag` → `airbag`);
- side/position words and `srs` dropped;
- punctuation split;
- word order ignored.

Top 15 are shown. Groups always sum to the policy-removal count.
First run: 14 groups. Airbag curtain 30, airbag (plain) 23, airbag steering 17, airbag knee 10,
airbag curtain roof 4, and so on: **every one is an airbag part**, i.e. eBay's airbag rule.
