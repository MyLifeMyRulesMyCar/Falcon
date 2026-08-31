# v1.4 — Events Gallery + Panel Visual Pass

Note on numbering: this absorbed the "v1.4" label in git ahead of the
originally-planned open-source-readiness milestone, which is why that work
is now labeled **v1.5** — see `docs/ROADMAP.md` for the versioning note.

Not originally scoped in the roadmap — added because v1.1 and v1.3 built
real event evidence (`/snapshots`, `/clips`) with no way to browse it
short of knowing an exact filename and hitting the URL by hand.

## Layout
No new files — additive to the existing single-file panel.
```
nvr/control/api.py                  # + GET /api/cameras/<name>/events
nvr/control/static/index.html       # + events button/modal, visual pass
```

## Events endpoint
`GET /api/cameras/<name>/events` — pairs snapshot (`.jpg`) and clip
(`.mp4`) by their shared `{zone}_{ts}_{track_id}` filename stem (both
stores already use this convention). Built from a live directory listing
each call, not from `recent_zone_events`, so a rotated-away file never
shows up as a dead link. Zone names can contain underscores
(`entry_path` does) — handled via `parts[:-2]` for the zone rather than a
naive split. Stray/malformed filenames are skipped (`len(parts) < 3` or a
non-numeric ts/track_id), sorted numerically by `(timestamp, track_id)`
descending, capped at 100.

## Panel
Per-row `events` button (mirrors the existing `zones` button) opens
`#events-modal` — same `.modal-box` pattern as the zone/MQTT modals. Each
entry renders as a `<figure>`: snapshot thumbnail, plus a
`<video controls preload="none">` when a clip is paired, so nothing
downloads until the operator actually presses play.

## Visual pass
Preview column moved to position 3 (identity/controls/stats grouping via
CSS-only dividers), proper `h1`/`h2` type scale + line-height, brighter
badge/dot contrast (`#2f7d2f`/`#7d2f2f` running/stopped,
`#2ea82e` on-dot), a shared `.btn` class across every modal, output-bar,
and dynamically-created button. Fixed a pre-existing `colSpan=15` vs.
actual 17-column mismatch found along the way (error/empty rows were
under-spanning by 2 columns).

## Test results
169 passed on the board (161 + 8 hardware-only) — 6 new: stem pairing,
clip not-yet-finalized, empty-dirs, unknown-camera-404, plus the
stray-file and sort-order cases.

## Live acceptance (board)
- Right after an event: `clip: null`, snapshot present — no broken player
  before the mux finishes.
- ~12s later: clip correctly paired under the same stem.
- After 4 events: newest-first order, newest clip still correctly
  `null`/pending, every returned URL → 200, API count matched on-disk
  stem count exactly.
- Visual pass verified live at `http://127.0.0.1:5050`.

## Known limitations / next levers
- Gallery caps at 100 entries per camera — fine at current
  `max_per_camera` values (30/30), would need pagination if those grow
  substantially.
