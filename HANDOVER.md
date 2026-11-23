# Handover

**Updated:** 2026-11-23

## State

- Towers parse in both ID formats and `tower_taken` fires on enemy outer/inner towers only (confirmed on the
  2026-11-22 21:30 game). Build stability (fed latch, undo guard, order keeper) is in since `a744587`.
- Missing results: when `GameEnd` never reaches the poll, `infer_result` (`tools/live.py`) calls the game from both
  nexus turrets of one side falling in the last `NEXUS_END_WINDOW_S`; surrenders stay `None`. `finish` logs
  `game end: GameEnd seen|missed, result …` (`bf708b0`).
- Shen keeps the guide's go-to with Riftmaker in slot 5 (Jay wants the damage) and Flash/Ignite (the top quest grants
  TP anyway). Thornmail in slot 5 was tried and reverted (`a0cff1f`); don't suggest it again unprompted.
- Jay's rule since 2026-11-22: commit and push finished work once `python tests/run.py` passes (in the local `CLAUDE.md`).

## Next (after Jay's next game)

1. `grep "game end:" tools/leeg.log`: if a game ended on the nexus but still saved `None`, widen `NEXUS_END_WINDOW_S`
   or find why the last polls dropped.
2. Quest TP: the 40-min 21:30 game logged `summoner spells:` only at 0:00, so Unleashed TP probably doesn't show as a
   spell change. After a top game that completes the quest, look for it elsewhere in `allgamedata` (active player,
   abilities). If found, tell the coach when TP is up (user message, not system prompt) and replace the "13-14 min"
   estimate in `macro.md`.
3. About 2026-11-29: re-measure build churn against the baseline (181 item-set changes, 28 games with an undo, 25 pure
   reorders across `tools/leeg.log*`, Oct 29 to Nov 22). Split the logs into games at `new game started` /
   `game saved`, then diff consecutive `response trigger=… build=[…]` lists.

## Not code

- API credits ran out about 12 times in a month, killing coaching for those games. Top up or set auto-reload.
- The coach error `TypeError: 'NoneType' object is not iterable` (Nov 8) is unexplained; coach errors now log a
  traceback, so check the log if it recurs.
