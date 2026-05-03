# leeg-coach

An in-game League of Legends tactical assistant that runs in a terminal alongside your client. Polls Riot's [Live Client Data API](https://hextechdocs.dev/getting-started-with-the-live-client-data-api/) every 3 seconds and surfaces live enemy stats, threat-sorted lineup, objective timers, rule-based coaching, and an optional AI coach powered by [Claude](https://www.anthropic.com).

The champion notes in this repo (`mundo/`, `sivir/`) are my own, but the tool works with any champion — drop in a `matchups.md` and it auto-discovers it.

## Features

- **Live enemy panel** — all 5 enemies sorted by threat tier with KDA, CS, gold, items, level
- **Objective timers** — drake and baron spawn countdowns, with lane-open callouts when towers fall
- **Rule-based DO panel** — deterministic advice: push windows, objective windows, fed-enemy alerts, next buy hint, dead-laner callouts
- **Event feed** — last 8 events (kills, towers, objectives) with timestamps
- **AI coach** (optional) — fires on significant events with 1–2 tactical bullets tuned to your champion's matchup notes and current game state
- **Voice output** (optional) — ElevenLabs neural voice, with a free edge-tts fallback via PowerShell

## Quickstart

```bash
git clone https://github.com/Jcegger/leeg-coach.git
cd leeg-coach
python tools/live.py
```

Leave it running. It waits at "WAITING FOR GAME" until you load into a match, then renders. Auto-detects your champion from the live API — pass `--champ <name>` to force a specific profile.

### AI coach setup (optional)

Get a key at [console.anthropic.com](https://console.anthropic.com) → API Keys. Note: Claude Pro and the API are separate billing systems.

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
```

Cost: ~$0.05–$0.10/game with Claude Sonnet, thanks to prompt caching. The rule-based DO panel still works without a key.

### Voice output setup (optional)

**ElevenLabs** (best quality):
```bash
pip install elevenlabs
export ELEVENLABS_API_KEY=...
```

**edge-tts** (free fallback — Windows only, no install needed): works automatically if ElevenLabs isn't configured. Uses PowerShell's `edge-tts` neural voices via a temp `.ps1` script in `C:\Windows\Temp`.

## How the AI coach works

On game-shifting events (first blood, ace, baron, inhibitor, your kills and deaths, multikills, tower takes), the coach fires a short 1–2 bullet response using:

- Your champion's `matchups.md` + `build.md` as the system prompt (prompt-cached, 1h TTL — full cost paid once, cheap reads after)
- Current game state: enemy items/scores, gold, threat tier, last 8 events, objective timers, tower state
- A persistent build commitment — the coach picks a 6-item path at game start and stays on it unless the enemy comp demands a pivot
- Rolling 5-call history for tactical consistency

Server-side validators run after every LLM response:
- Strip hallucinated or component items before they reach the screen
- Strip starters/consumables from the build display
- Rewrite unaffordable BACK bullets to FARM with the gold deficit shown

A 90-second periodic fallback fires if the game has been quiet. A 25-second cooldown between calls prevents spam.

## Setup — WSL2 / Windows

The Riot Live Client Data API binds to Windows `127.0.0.1:2999`. WSL2 can't reach that by default.

### Windows 11 22H2+ (preferred — both in-game and champ select work)

Add to `/mnt/c/Users/<you>/.wslconfig`:
```ini
[wsl2]
networkingMode=mirrored
```
Then from PowerShell: `wsl --shutdown`, then reopen WSL.

### Windows 10 (in-game only)

Forward port 2999 from the WSL gateway to Windows loopback. Run from **Admin PowerShell**:

```powershell
# Replace 172.22.64.1 with your WSL gateway IP (run: ip route | grep default  in WSL)
netsh interface portproxy add v4tov4 listenport=2999 listenaddress=172.22.64.1 connectport=2999 connectaddress=127.0.0.1
New-NetFirewallRule -DisplayName "WSL LeagueLiveAPI" -Direction Inbound -Protocol TCP -LocalPort 2999 -Action Allow
```

Use your WSL gateway IP as `listenaddress`, **not** `0.0.0.0`. Using `0.0.0.0` causes the proxy to intercept Windows-side `127.0.0.1:2999` and loop, silently breaking both WSL access and the Windows browser.

Champ select notes require Win11 mirrored networking or running the script natively on Windows (pass `--lockfile "C:\Riot Games\League of Legends\lockfile"`).

To remove later:
```powershell
netsh interface portproxy delete v4tov4 listenport=2999 listenaddress=172.22.64.1
Remove-NetFirewallRule -DisplayName "WSL LeagueLiveAPI"
```

## Adding a champion

Fast path — scaffolding command:

```bash
python tools/live.py --add-champ "Aurora" --source "https://www.mobafire.com/..."
```

Creates `aurora/` with template `README.md`, `matchups.md`, `build.md`, `playbook.md`, and `meta.json`. Fill in the bodies. The CLI auto-discovers it the next time you queue that champion.

Manual path — create `<champ>/matchups.md` with this structure:

```markdown
# Champ — Matchups

## Extreme threats
### Aatrox
<notes>

## Major threats
### Fiora
<notes>

## Even
...
```

Section names the parser recognizes: `Extreme threats`, `Major threats`, `Even`, `Minor`, `Tiny` (case-insensitive prefix match).

If the champion's Riot API alias differs from your folder name (e.g., `DrMundo` → `mundo`), add an entry to `CHAMP_ALIASES` in `tools/live.py`.

## Updating notes from a Mobafire guide

Mobafire blocks the default user agent (HTTP 403). Use curl with a browser UA:

```bash
curl -sL -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
  -o /tmp/guide.html "<mobafire url>"
```

Then strip HTML/scripts and parse the threats section manually or with Claude.

## Project structure

```
leeg-coach/
├── mundo/              ← Mundo top (Belle19's NA Challenger guide)
│   ├── build.md
│   ├── matchups.md
│   ├── playbook.md
│   ├── README.md
│   └── meta.json
├── sivir/              ← Sivir bot (heeiseenbeerg's guide + general ADC knowledge)
│   └── ...
└── tools/
    ├── live.py         ← entire CLI, ~2700 lines, stdlib + anthropic
    └── README.md       ← detailed technical docs (coach internals, cost model, triggers)
```

## Notes sources

- **Mundo (top):** [Belle19's "Too Big to Fail"](https://www.mobafire.com/league-of-legends/build/too-big-to-fail-na-challenger-mundo-main-guide-check-notes-matchup-update-revamp-632678) — NA Challenger Mundo main
- **Sivir (bot):** [heeiseenbeerg's "The Ultimate Sivir Build"](https://www.mobafire.com/league-of-legends/build/16-08-the-ultimate-sivir-build-648305) — build only; playbook + matchups are general ADC knowledge (see `sivir/README.md`)

## Status

### Working
- Live enemy panel: threat sort, KDA, CS, gold, items, level
- Rule-based DO panel: push windows, drake/baron timers, buy hint, fed-enemy alerts, dead-laner callouts
- AI coach: Claude Sonnet 4.6, prompt caching, build commitment, 5-call history, 10+ event triggers
- Voice output: ElevenLabs neural voice, edge-tts free fallback
- Tower state tracking with lane-open callouts
- Game-transition detection (resets coach state on new game), API watchdog

### Not working / blocked
- Champ select notes on Windows 10 (needs Win11 mirrored networking or native Windows Python)
- ARAM mode awareness (drake/baron timers still render in ARAM)

### Planned
- More champion folders
- ARAM-aware coach (skip drake/baron timers, use ARAM-specific prompt)
- Mute hotkey

## Troubleshooting

**Script shows "WAITING" forever in-game**
From Admin PowerShell: `netstat -an | findstr "LISTENING" | findstr ":2999"` — should show `127.0.0.1:2999 LISTENING`. If missing, Vanguard may be suppressing the API listener. Verify by hitting `https://127.0.0.1:2999/liveclientdata/allgamedata` in a Windows browser mid-match.

**Coach disabled at startup**
Either `pip install anthropic` is missing or `ANTHROPIC_API_KEY` isn't exported in the current shell. Run `source ~/.bashrc` and relaunch.

**"Cannot find .../matchups.md"**
The `--champ` value doesn't match a folder under the repo root. Check spelling.

**Champion index shows 0 entries**
CommunityDragon is unreachable. The script still runs — item IDs just show as `#<id>` instead of names.

**Vanguard blocks the API**
If `League of Legends.exe` is running but nothing listens on `127.0.0.1:2999` mid-match, Vanguard is suppressing the Live API listener. No clean tooling workaround — try restarting the client.
