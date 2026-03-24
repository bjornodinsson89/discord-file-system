- Added `/setup` admin key mode controls so bank calculator and jewelry alerts can use either the admin key pool or one selected admin's stored Torn API key.
# Changelog

## Unreleased
- Added Jewelry Store “wide open” alerts with Paul Wall meme, SHOPLIFT NOW button, and auto-delete when security returns.
- Updated `/setup` admin key settings so bank calculator and jewelry alerts can use either one selected admin key or a server-managed pool of selected eligible admins only.
- Hardened Discord startup login handling with retry/backoff for HTTP 429 and Cloudflare 1015 failures, keeping the process alive during cooldown instead of crash-looping.

## 2026-02-25 — Xanax Pools: Modal Creation + Unlimited Tickets + Auto End Date
Added a modal-based (form) pool creation flow to replace slash-command parameter entry, improving mobile usability. Pool ticket totals now accept either a numeric cap (e.g. 50) or the keyword UNLIMITED for no cap. Optional MM/DD Auto End Date triggers an automatic pool end + winner draw when reached.

## 2026-02-26 TC Bank Calc: Modal
Added: Torn City Bank Investment Calculator (/bank_calc)

Pulls live bank APRs from Torn API v2 (torn?selections=bank) using a single admin-configured “Bank rates API key” set inside the existing /setup panel modal.

Bank rates are cached for 1 hour to minimize API calls and prevent rate limiting.

Output is ephemeral-only (visible only to the user who ran the command).

Displays the best duration for the user’s settings, a clean per-duration profit table, and a compact “time to reach $2B” comparison for reinvesting with a fixed duration.
