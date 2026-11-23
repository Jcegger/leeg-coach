# Handover

**Updated:** 2026-11-22 (evening)

## Done 2026-11-22

- `a744587` Build stability: fed latch, undo guard (5 min), order keeper; validator and reason logging.
- `a744587` Towers: `parse_structure` reads both ID formats (`Turret_TOrder_L2_P3_…` and old `Turret_T1_L_03_A`). Before this, `tower_taken` never fired and an inhib loss could read as "push their nexus". Coach gets a per-side BASE line.
- `99c272a` Macro: `macro.md` per champion plus a repo-root General page, shown in the idle page's Macro tab. Top role quest TP folded in.
- Checked on the 21:30 game: all 20 structure IDs parsed, none `UNPARSED`; `tower_taken` fired 5 times, enemy outer/inner towers only.
- Result fallback: `GameEnd` was missed in 5 of 38 saved games (the Live API can close before the poll sees it). `infer_result` now calls the game from both nexus turrets of one side falling in the last `NEXUS_END_WINDOW_S` (60s); surrenders stay `None`. `finish` logs `game end: GameEnd seen|missed, result …`. The 21:30 game was backfilled to Win in `data/games.jsonl` (gitignored).
- Shen go-to keeps the guide's Riftmaker in slot 5 (Jay wants the damage); Thornmail stays the swap into auto-attackers and healers. Tried Thornmail in slot 5 (`9c66818`) and reverted it. Spells stay Flash/Ignite (the top quest grants TP anyway).

## Next (after Jay's next game)

1. `grep "game end:" tools/leeg.log`: if a game ended on the nexus but still saved `None`, widen `NEXUS_END_WINDOW_S` or find why the last polls dropped.
2. After a top game that completes the quest (Jay: it grants TP even without TP taken; the 21:30 game only logged spells at 0:00): `grep "summoner spells:" tools/leeg.log`: does the Live API report the quest's Unleashed Teleport, and at what minute? If yes, tell the coach when TP is up (user message, not system prompt) and replace the "13-14 min" estimate in `macro.md` with the observed time.
3. After about a week of games, re-measure build churn against the baseline (181 item-set changes, 28 games with an undo, 25 pure reorders across `tools/leeg.log*`, Oct 29 to Nov 22). The analysis scripts were in a session scratchpad and are gone. To redo it: split the logs into games at `new game started` / `game saved`, then diff consecutive `response trigger=… build=[…]` lists.

## Not code

- API credits ran out about 12 times in a month, killing coaching for those games. Top up or set auto-reload.
- The coach error `TypeError: 'NoneType' object is not iterable` (Nov 8) is unexplained; coach errors now log a traceback, so check the log if it recurs.
