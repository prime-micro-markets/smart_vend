# Product Inventory Overhaul — Implementation Plan (v3)

> **STATUS (2026-06-05): PAGE SHELVED until Nayax is live.** Decision: the page's
> core goal — programmatic best-price cost sourcing — is unreachable today. A
> live test confirmed Sam's blocks server-side pulls (HTTP 412 / Akamai) even
> from a residential IP, and every other real distributor (Vendors Supply,
> Vistar, McLane) gates pricing behind a login. With no machines, no orders, and
> no Nayax feed, there is nothing to source from. The page was **hidden** (router
> unmounted in `app/main.py`, nav + dashboard card removed) — code, templates,
> and the `Product`/`Supplier`/`ProductSource`/`InventoryLog` data model are kept
> intact. **This plan below is the blueprint for the eventual Nayax-API-driven
> rebuild**, not work to start now. The Nayax Core API is the missing piece: it
> provides live stock + real sales/cost data per machine.

Author: planning pass, 2026-06-05.

## Decisions locked with operator

1. **Merge Catalog + Find Product Prices into one "Product Price Book."** Rows of
   products; each row shows best cost (case + unit), vendor + direct link, sell
   price, margin, and a sell-price market reference.
2. **Cost sourcing = Sam's-first + one online wholesaler.** Sam's Club is the
   primary cost source (actual paid prices via paste; optional local fetch for
   shelf prices). Keep one reliable online wholesaler (WebstaurantStore via
   Firecrawl) as a second comparison column. Manual entry always available.
3. **Slim the Vendors tab to real buying sources.** Remove AI "Discover vendors"
   and the national-wholesaler noise. Vendors exist only to attribute costs.
4. **Defer stock tracking to a Nayax-driven Phase 2.** Hide on-hand / par /
   Restock Run behind a "coming with Nayax" state (keep the data model). Remove
   the eBay market source and the Seasonal toggle now.

## Key research findings driving the design

- **"Source missing prices" targets the wrong vendors.** It dispatches only
  `webstaurantstore` + `candy_machines` (`COMPARATOR_FETCH_KEYS`). The operator
  HAS a `FIRECRAWL_API_KEY` set locally and on Render, so WebstaurantStore IS
  dispatched (Firecrawl scrape then Claude extraction via `fetch_via_firecrawl`);
  the "needs Firecrawl / defaults off" caveat does NOT apply here. Yet the button
  still returned nothing useful in practice, so WebstaurantStore Firecrawl
  extraction is under-delivering and CandyMachines is a brittle ASP scraper that
  mostly returns off-domain noise. Sam's (the vendor that matters) is never
  dispatched (Akamai). The real problem is vendor selection plus Firecrawl
  extraction quality, not a missing key. **Phase C must include a verification
  spike: run a WebstaurantStore Firecrawl fetch for a few real SKUs and confirm
  it returns case + unit prices reliably before committing it as the second
  column.**
- **VendGuys sells no consumables.** Live catalog collections are Machines,
  Drinks *Machines*, Micromarket Kiosks, Parts & Accessories, Payment Hardware,
  Nayax, Moneta, Bill Validators. There is no snack/drink pricing to ingest.
  The original "ingest VendGuys product prices" idea is not feasible; VendGuys
  stays an **equipment** source only (already used by the equipment catalog).
- **"Fill UPCs" is near-useless.** Keyless UPCitemdb trial (100/day, rate-limited;
  local SSL interception also kills it). Only feeds a weak market panel.
- **Three redundant cost-entry paths** (Bulk Costs grid, CSV import, Sam's paste).
  Sam's paste is the only one worth keeping — it captures *actual paid prices*.
- **eBay market reference is noise** (resale listings). Remove.
- **Data model is sound and stays.** `Product` + `ProductSource` (per-supplier
  offer, case/unit cost math via `effective_unit_cost`) already model exactly the
  multi-vendor price book we want. No schema change needed for the core rebuild.

## Target page structure

Single page `/inventory/`, renamed in the UI to **"Product Price Book"** (URL
stays `/inventory/` for compatibility). Two tabs:

### Tab 1 — Price Book (default)
The merged view. One row per active product:

| Column | Source |
|---|---|
| Product (name / brand / size) | `Product` |
| Best cost — **case price** | cheapest `ProductSource.case_price` |
| Best cost — **unit price** | `ProductSource.effective_unit_cost` |
| Vendor (+ direct link) | `best_source.supplier`, `supplier_url` |
| Other vendors | count badge → expands per-source rows |
| Sell price (editable inline) | `Product.sell_price` |
| Margin | `Product.margin_pct` |
| Market sell ref | UPCitemdb / Open Prices / BLS (no eBay) |
| Actions | refresh price · open detail · edit · archive |

Behaviors:
- **Inline cost + sell editing** so the operator never leaves the row to set a
  price. Each row's "refresh price" runs the new sourcing engine (below) and
  auto-saves the best result, exactly the per-row pattern that already exists
  (`/{id}/refresh-prices`), but pointed at reliable vendors.
- **Drill-down preserved.** Clicking the product keeps today's detail page
  (history, all sources, sell price, etc.) — the operator explicitly likes this.
- **Programmatic + manual rows.** "Seed starter SKUs" stays (programmatic);
  Add/Edit/Archive stays (manual). This satisfies "rows that can be
  programmatically set or individually edited/added/removed."
- **Both case and unit price always shown and accurate.** The normalize() math
  already backfills the missing side from `case_pack_qty`; the rebuild makes sure
  every saved source has a pack so neither column is ever blank.

### Tab 2 — Vendors (slim + streamlined linking)
Curated buying sources only (Sam's Club, Vendors Supply, local distributors
actually used). Card = contact + account status + linked-product count.

**Cleanup (operator asked for this):**
- Remove the AI "Discover vendors" panel entirely (it's what seeded the national
  noise in the first place).
- One-time prune of the irrelevant/out-of-area vendors already on file. Add a
  lightweight **multi-select + "delete selected"** so the operator can clear the
  noise in one pass instead of deleting cards one at a time. (Single-delete
  already works well per the operator; this just makes the bulk cleanup fast.)
- After cleanup, the list should be short: the handful of places the business
  actually buys from.

**Streamlined product linking (the painful part today):** linking currently
means opening a product → add-source form, or digging into supplier edit. But the
machinery for the *good* path already exists and just needs to be surfaced:
`supplier_import.ingest_supplier_offers` takes a pasted CSV **or** an AI-extracted
order-guide paste and **auto-creates/updates Products and links ProductSources in
one round-trip** (idempotent, tolerant of header spellings, infers category).
Plan:
- Promote **"Paste this vendor's order guide"** to a first-class button on each
  vendor card (and the price-book), not buried at `suppliers/{id}/edit#import`.
  Operator pastes the rep's price sheet (CSV or raw text) → products appear and
  link automatically. This is the answer to "linking products needs to be much
  more streamlined."
- For Sam's specifically, the existing purchase-history paste already does this
  with actual paid prices — keep it as the Sam's-flavored version of the same
  flow.
- From a product row, an inline **"+ add vendor price"** mini-form (vendor picker
  + case price + pack) writes a `ProductSource` without leaving the page, for
  one-off links.
- Keep manual add/edit/delete vendor.

### Removed from the page
- Seasonal toggle + filter.
- eBay market source (delete `fetch_ebay` wiring from the gather call).
- "Fill UPCs" bulk button (UPC still settable by hand on the form for the
  market lookup; just not the broken bulk auto-fill).
- Bulk Costs grid and standalone CSV cost import as separate buttons — folded
  into inline editing + Sam's paste. (CSV import route can stay server-side as a
  power-user URL, just not surfaced as a primary button.)

### Deferred (Phase 2, Nayax)
- On-hand qty, par level, low-stock highlighting, Restock Run.
- Render these behind a single muted "Stock & restock arrives with Nayax"
  placeholder card. Keep `on_hand_qty` / `par_level` columns and `InventoryLog`
  in the model so Phase 2 can light them up without a migration.

## Sourcing engine rebuild

Replace the "scrape WebstaurantStore + CandyMachines + AI guess" dispatch with a
**Sam's-first** model:

1. **Sam's Club paste (primary, already built).** Keep
   `/inventory/costs/sams-paste` exactly as-is — actual paid prices, `origin=
   "sams_purchase"`. Promote it as *the* cost-entry path. This is the most
   accurate cost the business has.
2. **Sam's Club local fetch (new, optional).** The operator's home IP is not
   Akamai-blocked (`sams_club.search_products` already works locally). Add a
   small **local-only** lookup: when the app runs on the operator's machine
   (not Render), allow a one-click Sam's club-search for a SKU that fills
   case/unit price. Guard it so it never runs on Render (detect via a
   `DATABASE_URL` scheme / explicit `LOCAL_SOURCING_ENABLED` flag) and degrades
   to "run this locally" messaging in prod. Results upsert a Sam's
   `ProductSource` and sync to the live DB on the operator's next deploy/backup.
3. **WebstaurantStore (secondary comparison).** Keep as the single online
   wholesaler. `FIRECRAWL_API_KEY` is already set (local + Render), so it runs
   today; the gate logic stays only as a defensive fallback message if the key is
   ever removed. **Pending the Phase-C verification spike** — if Firecrawl
   extraction proves unreliable for WebstaurantStore's markup, swap to manual
   entry as the second column rather than ship a flaky scraper. Drop CandyMachines
   entirely (low-quality, off-domain noise).
4. **Order-guide import (universal path for account-gated distributors).** This is
   the honest answer for Vendors Supply, Vistar, McLane, and any real vending
   distributor. **Verified findings:**
   - **Vendors Supply** carries the right products (snacks, drinks, candy, micro-
     market) but **every price is behind a wholesale login** ("You must be logged
     in to order this item"). No public prices to scrape. (This is why it was
     archived as a live scraper.) The only non-fabricated path is importing the
     operator's own logged-in pricing / order guide.
   - **A&A Global** shows public prices but is a **bulk/gumball/toy/redemption**
     supplier (capsule toys, plush, crane prizes, bulk candy), the wrong category
     for smart-cooler snacks/drinks. Wiring it into search would mostly return
     toys, not the product mix. **Recommend: do NOT add as a search fetcher.** If
     the operator ever wants bulk-candy pricing specifically, it can be an
     optional, clearly-scoped extra, not part of the main snack/drink search.
   - General rule: serious vending distributors gate pricing behind accounts, so
     the reliable mechanism for ALL of them is "paste/import your order guide,"
     reusing `supplier_import.ingest_supplier_offers` (already creates products +
     links sources). Make this importer first-class (see Vendor section), rather
     than chasing anonymous scrapers that either return nothing (login wall) or
     the wrong category.
5. **Manual entry.** Inline case-price + pack editing on each row writes a
   `ProductSource` (default vendor = Sam's Club). Always available, never blocked.

`COMPARATOR_FETCH_KEYS` becomes `["webstaurantstore"]` (Firecrawl-gated); Sam's
flows through paste + local fetch; account-gated distributors (Vendors Supply
etc.) flow through order-guide import. None of these go through anonymous-scrape
dispatch. The AI-fallback web search stays only as an explicit "search the web"
button on the detail page, not as the silent fallback that produced confusing
guesses. **Do not wire VendGuys or A&A Global into product search.**

## Market sell-price reference (BLS national only — no fabricated local)

The operator wants help setting **sell** prices. **Decision: national BLS prices
only. No "local" price feed will be invented.** True city-level (Panama City)
retail price is not freely/honestly available per-SKU — BLS only publishes
national US-city-average and coarse region data, and inferring a local number
would be fabrication, which the operator explicitly rejected. Better to show one
honest national reference than a made-up local one.

- **National average (keep, expand):** BLS Average Price series already wired
  (`fetch_bls_average`), but only 2 categories map. **Expand `_BLS_SERIES`** to
  cover the real vending mix (bottled water, soda 2L/12pk, potato chips, cookies,
  candy, coffee) using published APU series IDs. Label clearly as "US national
  average (BLS)."
- **No local average.** Do not add a "local"/"Panama City" column. Do not relabel
  Open Prices as local. (Open Prices may stay as an optional, honestly-labeled
  "community-submitted shelf prices, US" row — but it is NOT presented as local
  and is omitted entirely if that framing risks confusion.)
- Surface the national reference as a compact, clearly-labeled hint on each
  price-book row / detail page so the operator can sanity-check sell prices.
  Remove eBay from `gather_market_reference` entirely.
- Requires `Product.upc` for barcode lookups — keep the manual UPC field on the
  form (drop the broken bulk auto-fill button).

## Phased build

**Phase A — Strip & reframe (low risk, high clarity).**
- Rename page to "Product Price Book"; collapse to 2 tabs.
- Remove Seasonal (toggle, filter, form field stays in model), eBay, Fill-UPCs
  button, Bulk Costs button, standalone CSV button.
- Move on-hand/par/Restock Run behind the Nayax Phase-2 placeholder.
- Remove the broken "Source missing prices" bulk button (re-added in Phase C
  pointed at the new engine).
- Vendors tab: remove AI discovery panel; keep slim directory; add bulk
  multi-select delete for the one-time noise cleanup.

**Phase B — Merge into the price-book row view.**
- Rebuild the catalog table to show case price + unit price + vendor link +
  sell + margin + national sell-ref, with inline cost/sell editing.
- Keep product detail page and drill-down intact.
- Surface "Paste vendor order guide" as a first-class linking action + inline
  "+ add vendor price" mini-form on each row.

**Phase C — Sourcing engine.**
- `COMPARATOR_FETCH_KEYS = ["webstaurantstore"]`, Firecrawl-gated; drop
  CandyMachines. **Do not add VendGuys or A&A Global to product search.**
- **Verification spike first:** run WebstaurantStore Firecrawl on real SKUs;
  confirm reliable case + unit prices. If unreliable, drop it to manual-only.
- Add local-only Sam's fetch (guarded off on Render).
- Re-add a per-row + bulk "refresh price" that uses the new engine and
  auto-saves the best `ProductSource`.
- Promote Sam's paste + the order-guide importer as the headline cost paths
  (the honest answer for account-gated distributors like Vendors Supply).

**Phase D — Market sell reference (national only).**
- Expand `_BLS_SERIES`; remove eBay from `gather_market_reference`. Add the
  per-row national sell-ref hint, clearly labeled "US national average (BLS)."
- **No local price feed** — do not fabricate or relabel anything as local.

**Phase E — Polish & copy.**
- Rewrite the Quick-start / help copy to the new 2-tab reality (and to the
  operator's no-em-dash style for any user-facing strings).
- Verify both case and unit price render for every seeded SKU.

## Files in scope (anchors)

- `app/routers/inventory.py` — tab routing, remove seasonal/eBay/UPC-fill/bulk
  buttons, rebuild row view + sourcing endpoints, Nayax-defer the stock routes.
- `app/templates/inventory/index.html`, `_tab_comparator.html`,
  `_tab_suppliers.html`, `_market_reference.html`, `_bulk_costs.html`,
  `detail.html` — restructure to 2 tabs + price-book rows.
- `app/services/price_comparator.py` — `COMPARATOR_FETCH_KEYS`, drop
  CandyMachines, gate WebstaurantStore on Firecrawl.
- `app/services/price_fetcher/sams_club.py` — local-fetch entry guarded off on
  Render.
- `app/services/market_reference.py` — expand `_BLS_SERIES`, drop eBay from the
  gather. No local price source.
- `app/services/supplier_import.py` — already does auto-create+link; surface it
  as a first-class linking action (no logic change needed, mostly UI wiring).
- Model unchanged (`Product`, `ProductSource`, `InventoryLog` all stay).

## Explicitly NOT doing

- No VendGuys product-price ingestion (no consumables there).
- No A&A Global in product search (public prices, but wrong category — bulk
  toys/gumball/redemption, not smart-cooler snacks/drinks).
- No anonymous scrape of Vendors Supply (all pricing is login-gated) — it comes
  in via order-guide import instead.
- No fabricated/relabeled "local" sell-price data — BLS national only.
- No fake on-hand tracking pre-Nayax.
- No schema migration for the core rebuild (model already fits).
- No eBay, no CandyMachines, no Seasonal.
