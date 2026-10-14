"""Pricing engine for the Parts Logger (eBay listings only).

See PRICING-ENGINE-SPEC.md. Modules:
    normalise  - part number normalisation / extraction
    titles     - parse our eBay titles into part type + model (+ engine code)
    store      - SQLite tables (sold_history, market_snapshots, price_suggestions)
    sales_import - eBay Seller Hub CSV importer (PII stripped on read)
    ebay_orders  - Fulfillment API order sync (needs a user refresh token)
    market     - Browse API competitor comps
    engine     - the blend (§5)
"""
