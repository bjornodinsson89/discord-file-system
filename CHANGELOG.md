# Changelog

## Unreleased
- Added Jewelry Store “wide open” alerts with Paul Wall meme, SHOPLIFT NOW button, and auto-delete when security returns.

## 2026-02-25 — Xanax Pools: Modal Creation + Unlimited Tickets + Auto End Date
Added a modal-based (form) pool creation flow to replace slash-command parameter entry, improving mobile usability. Pool ticket totals now accept either a numeric cap (e.g. 50) or the keyword UNLIMITED for no cap. Optional MM/DD Auto End Date triggers an automatic pool end + winner draw when reached.

## 2026-02-26 TC Bank Calc: Modal
Added: Torn City Bank Investment Calculator (/bank_calc)

Pulls live bank APRs from Torn API v2 (torn?selections=bank) using a single admin-configured “Bank rates API key” set inside the existing /setup panel modal.

Bank rates are cached for 1 hour to minimize API calls and prevent rate limiting.

Output is ephemeral-only (visible only to the user who ran the command).

Displays the best duration for the user’s settings, a clean per-duration profit table, and a compact “time to reach $2B” comparison for reinvesting with a fixed duration.
