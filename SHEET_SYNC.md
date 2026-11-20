# Syncing key exports into the shared Google Sheet

Runbook for an agent (or a human). The owner exports keys roughly every 1-2 months after a Humble
bundle, so don't rely on memory: follow this. Last done: 2026-11-20.

Sheet: https://docs.google.com/spreadsheets/d/1R8QwHHPwQlZjZrZFPhL5SW7ar7mLa9kxB1lxuiP-VhQ
(spreadsheetId `1R8QwHHPwQlZjZrZFPhL5SW7ar7mLa9kxB1lxuiP-VhQ`). It is shared with friends who claim keys.
Same file and link every time. Never create a new sheet, never trash anything the owner didn't ask about.

## Tools you need
- Google Sheets connector (`mcp__claude_ai_Google_Sheets__*`) and Google Drive connector. If the tools
  are missing, ask the owner to enable both in claude.ai connector settings and run `/mcp`, then restart
  Claude Code. Drive alone cannot edit an existing sheet.
- Read the `google-workspace` skill (references/sheets.md) before writing.
- Comments are only readable through Drive `read_file_content` with `includeComments: true`
  (the Sheets `get_spreadsheet` does not return them usefully).

## Source files (in this repo dir, git-ignored output of steam_redeem.py)
| File | Header? | Columns | Meaning |
|---|---|---|---|
| `already_owned.csv` | no | order_id, game, key_or_link | Steam rejected it as already owned by the owner: spare Steam key, shareable |
| `errored.csv` | no | order_id, game, key_or_link | Redeem errored: may still be valid, flag it |
| `redeemed.csv` | no | order_id, game, key_or_link | Used by the owner: NOT shareable |
| `humble_export_<ts>.csv` | yes | human_name, redeemed_key_val, is_gift, key_type_human_name, is_expired, steam_ownership | Full Humble export |
Files begin with a UTF-8 BOM (strip it). Many rows have an empty key: those cannot be handed out, skip them.

## Sheet layout (as of 2026-10-24)
- Tab order and visibility: `How to claim` is first so the shared link lands there (share
  `.../edit?gid=2010#gid=2010`; a bare link opens the first tab). Then `Keys`. ALL raw tabs are hidden
  (`hidden: true`); the owner can unhide via the "All sheets" menu. New raw tabs you add must be hidden too.
- `Keys` (sheetId 2008): THE tab friends use. Plain rows, sorted by Game. Columns:
  A Claimed (checkbox), B Claimed by, C Game, D Key, E Note, F Source. Row 1 header, frozen.
  Claimed rows go grey + strikethrough via a conditional format on `=$A2=TRUE` (A2:F1000).
  Two filter views: "Available only" (hides A=TRUE) and "Claimed only". Warning-only protection on C:F.
  Checkbox data validation is on A2:A<last row>.
- `How to claim` (sheetId 2010): instructions for friends. Don't change unless asked.
- Raw record tabs, never edit, only add: `humble_export_20260410-232712` (original ~533 rows; 30 rows hidden
  because they were claimed by comment), `added 2026-05-24`, `already_owned`, `errored`, `redeemed`,
  `humble_export_20260717-005328`, `humble_export_20261024-154453`, `humble_export_20261119-162827`, `humble_export_20261120-134831`.
- Keys is static on purpose (claims need stable rows). It is NOT a formula over the raw tabs.

## Procedure for a new export
1. Add each new CSV as its own raw tab named after the file minus `.csv` (e.g. `humble_export_20261215-101500`).
   Log CSVs (`already_owned`, `errored`, `redeemed`) already have tabs: for those, merge into the existing
   tab (append rows whose key is new) rather than creating a duplicate tab. Add the header
   `order_id, game, key_or_link` only if the tab is new. Bold header, freeze row 1.
   Write keys/order ids with a leading apostrophe via `update_values` so they stay text. Empty key = `""`.
   Convert Humble `True/False` to real booleans.
2. Read the current `Keys` tab (`Keys!A:F`). It is the source of truth for who claimed what.
3. Merge: for every key with a non-empty value in the new data that is NOT already in `Keys!D`,
   append a row to the bottom of `Keys`: Claimed = FALSE, Claimed by = "", Game, Key, Note, Source.
   - Note rules: `redeemed.csv` rows -> do not add as available; add with Claimed = TRUE and
     Claimed by = `owner (already redeemed)`. `already_owned` -> Note `Steam` (they are Steam keys; don't tell friends anything about the owner's library).
     `errored` -> Note `redeem errored, may not work`. Humble export rows -> Note = key type, plus
     ` (expired)` if is_expired.
   - Generic bundle names ("Humble Indie Bundle #N Steam Key", "Humble ... Bundle Steam Key") tell friends
     nothing: put the games in the name, e.g. `Humble Indie Bundle #1 (Aquaria, Gish, ...)`. HIB #1-#4 are
     already done (rows renamed 2026-10-24). Look up contents on the web (don't guess) and keep the name
     starting with the original so it sorts in the same place. Also done 2026-10-24: Frozenbyte, Introversion,
     Voxatron Debut. Still generic because web searches couldn't confirm contents: `Team Indie`,
     `Telltale Games Steam Key` (probably the 2012 Humble Telltale Bundle), `Prototype Steam Keys` and
     `Brazen Prototype Steam Key` (probably Double Fine Amnesia Fortnight prototypes). Ask the owner
     if they know, or leave as is.
   - Dedupe by key (case-insensitive). Existing rows win: NEVER overwrite Claimed/Claimed by.
   - If a key already in `Keys` now appears in `redeemed.csv`, set that row Claimed = TRUE and
     Claimed by = `owner (already redeemed)` (only if the box wasn't already ticked by a friend).
4. Re-sort the data range A2:F by column C ascending (`sortRange`, data rows only, never the header).
5. Extend the checkbox validation (`setDataValidation`, BOOLEAN) to the new last row, and extend the
   conditional-format range and filter-view ranges if rows passed row 1000 (grid is 1000 rows; use
   `appendDimension` first if needed). Clear validation/values below the last data row.
6. Comments: friends may also leave comments like "claimed". Read comments with Drive
   `read_file_content includeComments:true`. For any comment on a row in a raw tab that says claimed/taken,
   tick that key's Claimed box in `Keys` and put `claimed (comment)` in Claimed by. Do NOT resolve or
   delete comments. Optionally hide the raw row (`hiddenByUser`).
7. Verify: `get_values` on `Keys!A:F` (no error values, row count = old count + new keys, last row correct),
   confirm checkboxes display, tab list is unchanged except the new raw tabs.
8. Tell the owner the link and how many keys were added / how many were skipped (blank key, already present).

## Gotchas learned the hard way
- Never apply a sort/filter-sort on a range that contains a spilled formula; it moves the formula cell and
  leaves blank rows above the data. Keys is static so sorting is safe there.
- `get_values` returns checkbox cells as `"TRUE"`/`"FALSE"` strings, empty rows come back as `[]`.
- Field masks: no parentheses; comma-separated full paths. Tab names with spaces need single quotes in A1.
- Both write tools parse like the UI: prefix keys with `'`.
- Do not use `copyPaste PASTE_VALUES` from a formula scratch tab unless you also verify row counts; that is how
  Keys was first built (formula in a temporary `_build` tab, then pasted as values, then the tab was deleted).
- A key's claim state lives only in `Keys!A:B`. Don't drop or rebuild `Keys` from scratch.

## Optional upgrade the owner may want
Auto-hide claimed rows and timestamp claims need Google Apps Script (Extensions > Apps Script), which these
connectors can't deploy. If asked, hand the owner an `onEdit` script that hides the row when column A is ticked
and writes a timestamp to a new column G. The "Available only" filter view is the no-script alternative.
