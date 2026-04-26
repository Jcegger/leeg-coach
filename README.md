# leeg

Personal League of Legends notes + a small CLI that surfaces them while a game is running.

## Quickstart

Before you queue, in any terminal:

```bash
leeg
```

(Or `python3 ~/projects/leeg/tools/live.py` directly. The `leeg` alias lives in `~/.bashrc`.)

Leave it running. It sits idle until League opens, then:

- **In game** → live tactical assistant: enemy lineup with KDA / CS / items / level (sorted by threat), objective timers (drake / baron), recent events feed, rule-based "DO" advice (push windows, drake spawns, fed enemies), and an optional LLM coach (Claude Haiku 4.5) that fires on significant events.
- **Champ select** → bans + enemy picks with matchup notes, lane opponent highlighted. *(Win11-only with mirrored networking; Win10 portproxy setup below covers in-game only.)*

The CLI auto-detects which champion you're playing and loads the matching profile (one of the folders below). Pass `--champ <name>` only if you want to force a specific profile.

Quit with Ctrl-C.

### Optional: LLM coach

For per-event tactical coaching synthesized from your matchup + build notes, set an Anthropic API key:

```bash
echo 'export ANTHROPIC_API_KEY=sk-ant-...' >> ~/.bashrc && source ~/.bashrc
pip install anthropic
```

Get a key at console.anthropic.com → API Keys, and add a few dollars of credit (Plans & Billing → Add credit). Note: Claude Pro and the API are separate billing systems; Pro doesn't include API credits. Cost is ~$0.05–$0.10/game thanks to prompt caching. Without a key, the rule-based DO panel still works.

Offline reference? Open the notes directly:

- [`mundo/matchups.md`](mundo/matchups.md) — every matchup, ctrl-F the enemy
- [`mundo/build.md`](mundo/build.md) — every build path + runes + spells
- [`mundo/playbook.md`](mundo/playbook.md) — laning, mid/late, teamfighting

## Layout

```
leeg/
├── README.md           ← you are here
├── mundo/              ← Mundo top notes
│   ├── README.md       ← TL;DR + index
│   ├── build.md        ← items, runes, spells, skill order
│   ├── playbook.md     ← lane/mid/late + teamfighting
│   └── matchups.md     ← all 164 matchups by tier
├── sivir/              ← Sivir bot notes (ADC + support matchups)
│   ├── README.md
│   ├── build.md
│   ├── playbook.md
│   └── matchups.md
└── tools/
    ├── README.md       ← detailed CLI docs
    └── live.py         ← in-game / champ select CLI
```

## First-time setup (WSL2)

By default, WSL2 can't reach Windows-side `127.0.0.1`, which is where League's services bind. Two paths depending on your Windows version.

### Windows 11 22H2+ (preferred — both modes work)

Enable WSL2 mirrored networking:

1. Edit `/mnt/c/Users/<windows-user>/.wslconfig` and add:
   ```
   [wsl2]
   networkingMode=mirrored
   ```
2. From **PowerShell on Windows**, run `wsl --shutdown`.
3. Reopen WSL.

Verify: `cat /etc/resolv.conf` shows `127.0.0.53` (or similar) instead of `172.x.x.x`.

Both in-game (port 2999) and champ select (dynamic LCU port) work after this.

### Windows 10 (in-game only)

Mirrored networking is Win11-only. Workaround: forward port 2999 from the WSL gateway to Windows loopback. From **Admin PowerShell**:

```powershell
netsh interface portproxy add v4tov4 listenport=2999 listenaddress=172.22.64.1 connectport=2999 connectaddress=127.0.0.1
New-NetFirewallRule -DisplayName "WSL LeagueLiveAPI" -Direction Inbound -Protocol TCP -LocalPort 2999 -Action Allow
```

Replace `172.22.64.1` with your WSL gateway IP if different (`ip route | grep default` in WSL prints it). The `listenaddress=172.22.64.1` form (vs `0.0.0.0`) is important — it avoids a self-loop where the proxy intercepts Windows-side `127.0.0.1:2999` traffic instead of forwarding it.

This covers the Live Client Data API (port 2999, used during a match). The LCU API (champ select) uses a dynamic port that changes each client launch, so champ select notes won't work without mirrored networking. To remove later:

```powershell
netsh interface portproxy delete v4tov4 listenport=2999 listenaddress=172.22.64.1
Remove-NetFirewallRule -DisplayName "WSL LeagueLiveAPI"
```

**Status on this machine:** Win10 portproxy in place; mirrored networking unavailable.

## Adding a new champion

1. Create `leeg/<champ>/matchups.md` using the same structure as `mundo/matchups.md`:
   ```markdown
   # <Champ> — Matchups

   ## Extreme threats
   ### Aatrox
   <body>

   ### Bel Veth
   <body>

   ## Major threats
   ...
   ```
   Section names the parser recognizes: `## Extreme threats`, `## Major threats`, `## Even`, `## Minor`, `## Tiny` (case-insensitive prefix match).
2. (Optional) Add `build.md`, `playbook.md`, `README.md` mirroring the Mundo pattern.
3. The CLI will pick it up automatically the next time you queue that champ — no flags required. If the champion's API name doesn't match your folder name (e.g., `DrMundo` → `mundo`), add an entry to `CHAMP_ALIASES` in `tools/live.py`.

## Updating notes from a Mobafire guide

Mobafire blocks the default WebFetch User-Agent (HTTP 403). Use curl with a browser UA:

```bash
curl -sL -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" \
  -o /tmp/guide.html "<URL>"
```

Then strip HTML/scripts and parse the threats section. Ask Claude to refresh the matchup files when the source guide updates — the Mundo notes were extracted that way.

## Sources

- **Mundo (top):** [Belle19's "Too Big to Fail"](https://www.mobafire.com/league-of-legends/build/too-big-to-fail-na-challenger-mundo-main-guide-check-notes-matchup-update-revamp-632678) — NA Challenger Mundo main, Mobafire
- **Sivir (bot):** [heeiseenbeerg's "The Ultimate Sivir Build"](https://www.mobafire.com/league-of-legends/build/16-08-the-ultimate-sivir-build-648305) — Mobafire (build-only; playbook + most matchups in this repo are general Sivir/ADC knowledge — see `sivir/README.md` for per-file provenance)

## Status & roadmap

### Working today (Win10 + WSL2 + portproxy)
- ✅ In-game tactical assistant: live enemy stats, threat-tier sort, KDA/CS/items/level
- ✅ Damage-profile build picker (rule-based) + AP/AD enemy classification with item-aware overrides
- ✅ Rule-based "DO" advice panel (push windows, drake/baron timers, fed-enemy alerts, CS/item gap)
- ✅ Objective timers (drake / baron) and rolling recent-events feed
- ✅ LLM coach (Claude Haiku 4.5) with prompt caching, structured JSON output, per-game build commitment, team-score awareness, history of last 5 calls
- ✅ Game-transition detection (resets coach state on new game)
- ✅ API watchdog (per-call 20s timeout + 30s backstop)

### Not working / blocked
- ❌ Champ select notes — needs WSL2 mirrored networking (Win11 22H2+) or running the script natively on Windows; the Win10 portproxy doesn't cover the LCU's dynamic port
- ❌ ARAM / non-SR mode awareness — drake timers and matchup notes still render even when irrelevant

### Planned
- 📌 **Text-to-speech** for coach output. Win10 SAPI from WSL is the easiest path:
  ```bash
  powershell.exe -NoProfile -Command "(New-Object -ComObject SAPI.SpVoice).Speak('text')"
  ```
  Approach: add a `speak` field to the coach JSON schema (TTS-optimized 1-sentence summary, ≤80 chars), fire it via a background thread on each successful call, interrupt previous speech when new event triggers. Higher-quality alternatives later: ElevenLabs (~$1–2/game) or OpenAI TTS (~$0.10/game). See `tools/README.md` for details when implemented.
- 📌 **Add more champion notes.** Currently `mundo/` and `sivir/`. Pattern is documented above.
- 📌 **ARAM-aware coach.** Detect `gameData.gameMode == 'ARAM'`, skip drake/baron timers, swap to ARAM-specific prompt focused on poke/all-in/cooldown windows.
- 📌 **Mute hotkey** for the coach (relevant once TTS lands — silent mute mid-game).

## Troubleshooting

**Script shows "WAITING" forever even though League is open**
You're not actually loaded into a match (the Live Client API only runs during gameplay, not in lobby/champ select/loading/post-game). Wait until you're on the map. If still WAITING in-match: from Admin PowerShell, `netstat -an | findstr "LISTENING" | findstr ":2999"` — should show `127.0.0.1:2999 LISTENING` plus your portproxy listener. If the Windows-side listener is missing, Vanguard or another anti-cheat may be suppressing it; verify by hitting `https://127.0.0.1:2999/liveclientdata/allgamedata` in a Windows browser.

**Live API connects but champ select doesn't (or vice versa)**
On Win10, champ select isn't supported with the basic portproxy setup — the LCU port is dynamic. Either upgrade to Win11 + mirrored networking, or run the script natively on Windows (install Python on the Windows side and pass `--lockfile "C:\Riot Games\League of Legends\lockfile"`).

**Coach disabled at startup**
Either `pip install anthropic` is missing, or `ANTHROPIC_API_KEY` isn't set in the shell that launched `leeg`. Re-run `source ~/.bashrc` after setting the key, then relaunch.

**"Cannot find …/matchups.md"**
The `--champ` value doesn't match a folder under `leeg/`. Check spelling.

**Champion index says 0 entries on startup**
CommunityDragon is unreachable (network down, or its CDN refused the request). The script still runs — IDs just show as `#<id>` instead of names.
