# Handover

**Updated:** 2026-11-22

## Done 2026-11-22

- `a744587` Build stability: fed latch, undo guard (5 min), order keeper; validator and reason logging.
- `a744587` Towers: `parse_structure` reads both ID formats (`Turret_TOrder_L2_P3_…` and old `Turret_T1_L_03_A`). Before this, `tower_taken` never fired and an inhib loss could read as "push their nexus". Coach gets a per-side BASE line.
- `99c272a` Macro: `macro.md` per champion plus a repo-root General page, shown in the idle page's Macro tab. Top role quest TP folded in.

## Next (after Jay's next game)

1. `grep "structure:" tools/leeg.log`: every line parsed, none `UNPARSED`. If one is, add its pattern to `parse_structure` and to `test_parse_structure_both_formats`.
2. `grep "trigger=tower_taken" tools/leeg.log`: should appear.
3. `grep "summoner spells:" tools/leeg.log`: does the Live API report the quest's Unleashed Teleport, and at what minute? If yes, tell the coach when TP is up (user message, not system prompt) and replace the "13-14 min" estimate in `macro.md` with the observed time.
4. After about a week of games, re-measure build churn against the baseline (181 item-set changes, 28 games with an undo, 25 pure reorders across `tools/leeg.log*`, Oct 29 to Nov 22). The analysis scripts were in a session scratchpad and are gone. To redo it: split the logs into games at `new game started` / `game saved`, then diff consecutive `response trigger=… build=[…]` lists.

## Not code

- API credits ran out about 12 times in a month, killing coaching for those games. Top up or set auto-reload.
- The coach error `TypeError: 'NoneType' object is not iterable` (Nov 8) is unexplained; coach errors now log a traceback, so check the log if it recurs.
