# leeg tools

## live.py

CLI that auto-detects which League state is active and renders a live tactical assistant. Three modes, in priority order:

1. **In game** — polls the Live Client Data API on `https://127.0.0.1:2999`. Shows the full assistant: rule-based DO advice, objective timers, recent events, enemies with KDA/CS/items/level sorted by threat, plus an optional Claude-powered coach.
2. **Champ select** — reads the LCU lockfile and queries the local League client. Shows bans + enemy picks as they lock in, with `← your lane` highlighted. Requires WSL2 mirrored networking (Win11 only) or running natively on Windows — the basic Win10 portproxy setup doesn't cover the LCU's dynamic port.
3. **Waiting** — idle banner with diagnostic info, including which champ profiles are available.

The script also auto-detects **which champion you're playing** and loads the matching `leeg/<champ>/matchups.md` automatically (see "Champ resolution" below). No `--champ` flag needed for the common case.

### Run

```bash
python3 ~/projects/leeg/tools/live.py
```

Options:

```bash
python3 live.py --champ mundo                 # force a profile (default: auto-detect)
python3 live.py --host 192.168.1.5            # override API host
python3 live.py --lockfile '/mnt/c/...'       # override lockfile path
python3 live.py --max-chars 800               # body truncation per matchup
python3 live.py --poll 2                      # poll interval (seconds)
```

Quit with Ctrl-C.

### How it works

- **Live Client Data API**: localhost service on port 2999, no auth needed, self-signed cert.
- **LCU API**: localhost service on a random port chosen at client startup, auth via the lockfile (`username:pid:port:password:protocol`). Endpoint used: `/lol-champ-select/v1/session`.
- **Champion ID → name**: fetched once at startup from CommunityDragon's `champion-summary.json` (Cloudflare blocks default urllib UA, so we send a browser-ish User-Agent).
- **Matchup data**: parsed from `~/projects/leeg/<champ>/matchups.md` — `## Tier` headings define threat tiers, `### Champion` sections hold the body. Parsed lazily and cached the first time a champ's notes are loaded.
- The script polls every few seconds and re-renders only when state changes (signature-based diff).

### Damage-type build picker

Once enemies are visible (champ select or in-game), the script computes an **AP / AD damage profile** of the enemy team and picks the matching variant from your champ's `build.md`. You'll see a line like:

```
comp: AP (4 AP / 1 AD)
build: vs Heavy AP — Yun Tal · Mercury's Treads · Navori · Infinity Edge · …
```

How it works:

1. Each enemy's primary damage type comes from `CHAMP_DAMAGE` in `live.py` (AD / AP / Mixed). Mixed contributes 0.5 to each side.
2. Comp is labeled `AP` if AP count ≥ 3.5, `AD` if AD count ≥ 3.5, otherwise `Standard`.
3. The script parses `### variant` headings under any `## ... build` / `## example` section in `build.md`. Headings get classified by keywords:
   - `vs/heavy/full ap`, `magic damage`, `stacked mr` → AP variant
   - `vs/heavy/full ad`, `physical damage` → AD variant
   - `standard`, `core`, `current` → Standard variant
4. The first matching variant is picked. If a champ has no AP/AD variant, it falls back to Standard.

If you queue a champion that's not in `CHAMP_DAMAGE`, that enemy contributes 0 to the profile. Add new champs to the `_AD` / `_AP` / `_MIXED` lists at the top of `live.py`.

#### Item-aware override (in-game only)

In champ select, only the archetype is available. **In game**, the script also reads each enemy's items and overrides the archetype when an enemy is clearly building cross-class:

1. At startup, fetches `items.json` from CommunityDragon and classifies every item as `AP` / `AD` / `Tank` / `Other` from its `categories` (`SpellDamage`/`MagicPenetration` → AP; `Damage`/`CriticalStrike`/`ArmorPenetration`/`AttackSpeed` → AD; `Health`/`Armor`/`SpellBlock` → Tank).
2. For each enemy, if they have ≥ 2 items of a single offensive type, that overrides their archetype (`≥ 2 AP and ≥ 2 AD` → Mixed).
3. With < 2 stat-bearing items, falls back to archetype.

So an off-meta AP Yi flips from `AD` to `AP` once they finish Riftmaker + Liandry's. The header shows a `· N from items` qualifier when overrides happened, e.g. `comp: AP (4 AP / 1 AD · 1 from items)`.

If items.json fails to fetch, the script falls back gracefully to archetype-only.

### Champ resolution

The script discovers all `leeg/<dir>/matchups.md` at startup and uses them as the available profiles. When champ select or in-game state is detected, it reads your champion ID/name from the API and resolves it to a folder:

1. Normalize both sides (strip non-alphanumerics, lowercase). `Sivir` → `sivir`.
2. Match against folder names. Folder `sivir` → match.
3. If no match, fall back to `CHAMP_ALIASES` in `live.py` (e.g., `DrMundo` → `mundo`, `MonkeyKing` → `wukong`). Add an entry there if a new champ's API name diverges from your folder name.

If `--champ X` is passed, auto-detection is skipped and `X` is used regardless of the in-game champion. Useful for studying matchups outside of a game (`python3 live.py --champ sivir` and read the file output).

If you queue a champ with no folder, the header shows `no notes` and a warning prints. The previously-loaded champ's notes (if any) stay loaded as a fallback so the screen isn't blank.

### What it shows

**In-game:**
```
leeg live · IN GAME · Mundo · 14:23 · 172.22.64.1 · notes: mundo
━━━━━━━━━━━━━━━

coach (laner_died, 3s ago):
PUSH wave fast — Mord respawn 18s, plate timer hot.
WARD river bush before he comes back to lane.
RECALL after wave only if HP < 60%; otherwise hold prio.

▶ PUSH WAVE — Mordekaiser dead (18s)
▶ Drake spawns in 0:42 — reset & rotate

comp: AP (3.5 AP / 1.5 AD)
build: Full AP enemy team — Heartsteel · Boots of Swiftness · Hollow Radiance · Spirit Visage · Force of Nature · Jak'Sho

objectives: drake 0:42  ·  baron 10:37  ·  drakes: 1

recent:
  14:15  KILL    Garen → Mordekaiser
  13:00  DRAKE   Cloud → Garen
  12:42  TOWER   T2 mid outer

[Even] Mordekaiser (top)  4/2/1 · 102cs · lvl 11 · 3 items · DEAD 18s  ← your lane
<matchup notes body>

[Minor] Akali (mid)  3/3/0 · 89cs · lvl 9 · 2 items
<matchup notes body>
```

Sorted Extreme → Major → Even → Minor → Tiny.

**Champ select:**
```
Bans  you: A, B  ·  them: C
You: Dr. Mundo (top)

[tier] Champion (position)  ← your lane
<matchup notes body>
```

### Tactical assistant components

Layered top → bottom in the in-game view, each adds different signal:

1. **Coach (LLM)** — 1–3 short imperative bullets from Claude Haiku 4.5, fired on significant events (opening, drake/baron taken, your death, laner death, drake spawn windows, kills involving you, ace, periodic). Optional; off if `LEEG_ANTHROPIC_API_KEY` is unset. See "LLM coach setup" below.
2. **DO panel** — deterministic rule-based advice (red = immediate, yellow = action window, white = macro). Triggers: you/laner dead → push or track minimap, drake/baron <60s, CS/item gap with laner, fed enemies (≥5 kill lead).
3. **Build/comp line** — same damage-profile build picker as before. Updates as enemies buy items.
4. **Objectives line** — drake / baron timers (approximate; based on event-driven last-kill + 5-min/6-min respawn). Also shows count of drakes already taken.
5. **Recent events feed** — last 5 events from the API: kills, drakes, towers, inhibs, first blood, multikills, aces. Each line is `[time] EVENT_TYPE actor → target`.
6. **Enemies** — extras line now includes KDA, CS, level, item count, and respawn timer if dead. Same threat-tier sort.

### LLM coach setup

For richer coaching that synthesizes your matchup notes + build guide + current game state into actionable bullets:

```bash
pip install anthropic
echo 'export LEEG_ANTHROPIC_API_KEY=sk-ant-...' >> ~/.bashrc && source ~/.bashrc
```

Get an API key at console.anthropic.com → API Keys, and add a few dollars of credit (Plans & Billing → Add credit). Note: Claude Pro is a separate billing system from the API; Pro doesn't include API credits.

#### How it works

- **System prompt** (cached, 1-hour TTL): `<champ>/matchups.md` + `<champ>/build.md` plus style/consistency rules. ~12K tokens for Mundo, written once per session at ~$0.02, then ~$0.001 reads thereafter.
- **User message** (per-call): trigger reason, game time, your stats + items, enemies, recent events, objective timers, team score summary, current build commitment, last 5 calls of tactical advice.
- **Structured output**: coach returns JSON via `output_config.format`:
  - `bullets`: 1–3 short imperative tactical lines for RIGHT NOW
  - `live_build`: 4–6 item path (committed across the game, not just this call)
  - `build_diverged` + `build_change_reason`: whether/why we deviated from rule-based default
- **Per-call timeout**: 20s, no SDK retries — fail-fast for real-time use.
- **Watchdog**: if a call is somehow still in-flight after 30s, force-clear so the coach can keep firing.
- **Game-transition detection**: when game time jumps backward (new match), all per-game state (build commitment, recent advice, event counter) resets automatically.

#### Memory model

| Memory | Scope | Mechanism |
|---|---|---|
| Build commitment | Whole game | `Coach.committed_build` — set the first time the LLM returns a `live_build`, only replaced when the LLM commits to a different path. Surfaced prominently in every prompt. |
| Tactical advice | Last 5 calls (~2–4 min) | Rolling ring buffer of bullets + trigger context. Surfaced in user message. |
| Game-state context | Per call | Score totals, recent 8 events, current items, drake/baron timers — recomputed every trigger. |

#### Triggers

The coach fires on these events (cooldown-gated, default 25s between calls):

- `opening` — first call after game time > 60s
- `firstblood`, `ace`, `baronkill`, `inhibkilled`, `drake_taken`
- `you_died`, `you_killed`
- `laner_died` — your lane opponent's `isDead` transitions to true
- `drake_soon` — drake spawning in 20–40s
- `periodic` — fallback if nothing else triggered for 90s

#### Cost ballpark

Per game (~25–30 minutes, 10–15 coach calls):
- 1 cache write × ~12K tokens ≈ $0.015
- 9–14 cache reads × ~12K tokens ≈ $0.012
- 10–15 user messages × ~600 tokens ≈ $0.006
- 10–15 outputs × ~150 tokens ≈ $0.011
- **Total: ~$0.05–$0.10/game**

#### Tunables

- `LEEG_ANTHROPIC_API_KEY` — required (falls back to `ANTHROPIC_API_KEY` if unset, but using the leeg-specific name avoids conflicting with Claude Code subscription auth)
- `LEEG_COACH_MODEL` — default `claude-haiku-4-5`. Try `claude-sonnet-4-6` for sharper analysis at ~3x cost.
- `LEEG_COACH_COOLDOWN` — default 25 seconds between calls. Lower = more reactive but more cost.

If `LEEG_ANTHROPIC_API_KEY` is unset or `pip install anthropic` hasn't been run, the coach disables silently — the rest of the assistant (DO panel, timers, events, enemy details) still works.

### Planned: text-to-speech for coach output

Not yet implemented. Easiest path on Win10/WSL: invoke Windows SAPI from WSL via `powershell.exe`. Test from a WSL prompt:

```bash
powershell.exe -NoProfile -Command "(New-Object -ComObject SAPI.SpVoice).Speak('Push wave, Mord respawn 18 seconds')"
```

Implementation sketch:
1. Add a `speak` field to `COACH_SCHEMA` — TTS-optimized 1-sentence summary (≤80 chars, no abbreviations, simpler grammar than the on-screen bullets).
2. New helper that fires `powershell.exe ... SAPI.SpVoice.Speak(...)` in a background thread, with previous-speech cancellation when a new event triggers.
3. In `Coach._call`, after parsing JSON: if `speak` is non-empty, call the helper.
4. Env var `LEEG_COACH_TTS=0` to disable (default on once shipped).
5. Champion-name phonetic substitutions (Kha'Zix → "kuh-ZIX") because SAPI mangles them.

Latency: ~500ms (PowerShell cold-start dominates). Voice quality: fine-but-robotic. Higher quality later via ElevenLabs (~$1–2/game) or OpenAI TTS (~$0.10/game) — same interface, different backend.

### WSL2 caveat (important)

By default, **WSL2 cannot reach Windows-side `127.0.0.1`** because each runs in its own network namespace. League's services bind to `127.0.0.1` on the Windows host, so neither the Live Client API nor the LCU API are reachable from a WSL2 process out of the box.

**Win11 22H2+ — preferred fix: enable WSL2 mirrored networking.** Add to `%USERPROFILE%\.wslconfig` on the Windows side:

```
[wsl2]
networkingMode=mirrored
```

Then run `wsl --shutdown` from PowerShell to restart WSL with the new mode. After that, `127.0.0.1` from WSL2 maps to `127.0.0.1` on Windows, and **both** APIs work directly (in-game + champ select).

**Win10 — portproxy workaround (in-game only).** From Admin PowerShell:

```powershell
netsh interface portproxy add v4tov4 listenport=2999 listenaddress=172.22.64.1 connectport=2999 connectaddress=127.0.0.1
New-NetFirewallRule -DisplayName "WSL LeagueLiveAPI" -Direction Inbound -Protocol TCP -LocalPort 2999 -Action Allow
```

Replace `172.22.64.1` with your WSL gateway IP if different (`ip route | grep default` from WSL prints it). The `listenaddress=` must be the WSL gateway IP, **not** `0.0.0.0` — binding to `0.0.0.0` causes the proxy to intercept Windows-side `127.0.0.1:2999` traffic and self-loop instead of forwarding cleanly to the real Live API listener. Champ select uses a dynamic LCU port so this workaround doesn't cover it; for champ select on Win10 either upgrade to Win11 or run the script natively on Windows.

**Alternative — run on Windows:** Copy or symlink this script to a Windows-accessible path and run with Windows Python. From PowerShell:

```powershell
python "\\wsl$\Ubuntu\home\jayegger\projects\leeg\tools\live.py" --lockfile "C:\Riot Games\League of Legends\lockfile"
```

(Substitute your distro name and username.) The `--lockfile` flag is needed because the script's auto-detect scans `/mnt/<letter>` paths that don't exist on Windows. Both APIs work this way since the script runs as a Windows process accessing `127.0.0.1` directly.

### Limitations

- **In champ select, enemy picks may be hidden** in Blind Pick or until they lock in. Draft / Ranked show picks as they happen.
- **Champion index is fetched once at startup** — restart the script after a new patch to pick up new champs.
- **Objective timers are approximate.** Drake first spawn 5:00, respawn 5:00 after kill. Baron first 25:00, respawn 6:00 after kill. Patch timing changes (voidgrubs, atakhan) aren't tracked — if Riot shifts spawn rules, the timer drifts until the code is updated.
- **Anti-cheat may block the Live Client API.** If `League of Legends.exe` is running with no listener on `127.0.0.1:2999` even mid-match, Vanguard or another anti-cheat is suppressing it. There's no clean workaround from a tooling perspective — running the script natively on Windows sometimes helps because it's seen as a local user-mode process.
