#!/usr/bin/env python3
"""
leeg live — show Mundo matchup notes for whatever League state is active.

Modes (auto-detected, priority order):
  1. In-game        — polls Live Client Data API at https://127.0.0.1:2999
  2. Champ select   — reads the LCU lockfile and queries the local client
  3. Waiting        — idle until a client/game is detected

Usage:
    python3 live.py            # default: mundo
    python3 live.py --champ mundo
    python3 live.py --host 192.168.1.5
    python3 live.py --lockfile '/mnt/c/Riot Games/League of Legends/lockfile'

Quit with Ctrl-C.
"""

import argparse
import base64
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from urllib.error import URLError

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

LEEG_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = LEEG_ROOT / 'data'
POLL_SECONDS = 3
TIER_ORDER = {'Extreme': 0, 'Major': 1, 'Even': 2, 'Minor': 3, 'Tiny': 4}

# API championName → folder name, only when normalize() of the API name
# doesn't match the folder name. e.g. API says "DrMundo", folder is "mundo".
CHAMP_ALIASES = {
    'drmundo': 'mundo',
    'monkeyking': 'wukong',
}

# Primary damage type by normalized champion name. Used to pick the right
# variant from a champ's build.md (Standard / vs Heavy AP / vs Heavy AD).
# 'Mixed' contributes 0.5 to each side; champs not in this dict default to Mixed.
_AD = (
    "aatrox akshan ambessa aphelios ashe belveth briar caitlyn camille darius "
    "draven ezreal fiora gangplank garen gnar graves hecarim illaoi irelia "
    "jarvaniv jax jayce jhin jinx ksante kaisa kalista kayn khazix kindred "
    "kled leesin lucian masteryi missfortune naafiri nasus nidalee nilah "
    "nocturne olaf pantheon pyke qiyana quinn rammus reksai renekton rengar "
    "riven samira senna sett shyvana sion sivir skarner smolder talon tristana "
    "trundle tryndamere twitch udyr urgot varus vayne vi viego volibear warwick "
    "wukong monkeyking xayah xinzhao yasuo yone yorick yunara zaahen zed zeri"
).split()
_AP = (
    "ahri akali alistar amumu anivia annie aurelionsol aurora azir bard "
    "blitzcrank brand braum cassiopeia chogath diana drmundo ekko elise "
    "evelynn fiddlesticks fizz galio gragas gwen heimerdinger hwei ivern "
    "janna karma karthus kassadin katarina kayle kennen leblanc leona lillia "
    "lissandra lulu lux malphite malzahar maokai mel milio mordekaiser morgana "
    "nami nautilus neeko nunu nunuwillump orianna rakan rell renata renataglasc rumble "
    "ryze sejuani seraphine singed sona soraka swain sylas syndra tahmkench "
    "taliyah taric teemo twistedfate veigar velkoz vex viktor vladimir xerath "
    "yuumi zac ziggs zilean zoe zyra"
).split()
_MIXED = "corki kogmaw ornn poppy shaco shen thresh".split()
CHAMP_DAMAGE = {**{c: 'AD' for c in _AD}, **{c: 'AP' for c in _AP}, **{c: 'Mixed' for c in _MIXED}}

# Comp-aware threat tags. A champ can carry multiple tags (Warwick = HEALING +
# HARD-CC). Drives the TEAM THREATS block in the coach user message — the LLM
# uses these to justify slot-3/slot-4 counter-item pivots. Alias forms inline,
# matching the CHAMP_DAMAGE convention (Wukong needs both wukong+monkeyking,
# Renata needs both renata+renataglasc — Live API may surface either).
_HEALING = (
    "aatrox akshan diana drmundo fiora illaoi janna karma nami "
    "renata renataglasc senna sona soraka swain sylas trundle "
    "vladimir volibear warwick yuumi"
).split()
_HARD_CC = (
    "alistar amumu annie ashe braum cassiopeia galio leona lillia "
    "lissandra malphite malzahar maokai mordekaiser morgana nautilus "
    "ornn rakan rell sejuani sion skarner tahmkench thresh urgot "
    "veigar warwick zoe"
).split()
_ATTACK_SPEED = (
    "aphelios belveth jax jinx kaisa kayle kindred kogmaw masteryi "
    "quinn samira tristana tryndamere twitch vayne xayah yasuo yone"
).split()
_TANK = (
    "alistar amumu chogath drmundo galio ksante malphite maokai "
    "nautilus ornn poppy rammus rell sejuani shen sion skarner "
    "tahmkench taric volibear zac"
).split()
_ASSASSIN = (
    "akali ekko evelynn fizz katarina khazix leblanc naafiri nocturne "
    "pyke qiyana rengar shaco talon viego zed"
).split()
_CRIT = (
    "aphelios ashe caitlyn jhin jinx kaisa kindred missfortune samira "
    "sivir tristana twitch vayne xayah yasuo yone zeri"
).split()
CHAMP_THREAT_TAGS = {}
for _tag, _list in (('HEALING', _HEALING), ('HARD-CC', _HARD_CC),
                    ('ATTACK-SPEED', _ATTACK_SPEED), ('TANK', _TANK),
                    ('ASSASSIN', _ASSASSIN), ('CRIT', _CRIT)):
    for _c in _list:
        CHAMP_THREAT_TAGS.setdefault(_c, []).append(_tag)
del _tag, _list, _c

# Items that amp healing dealt to enemies. Used to promote borderline healing
# threats (Aatrox + Bloodthirster, Vladimir + Riftmaker) so a single fed healer
# trips stacked_healing. Conservative on purpose — self-amp-only items
# (Spirit Visage, Death's Dance) do NOT promote since they don't change the
# team's healing output. Resolved against item_index by display name at runtime.
HEALING_AMP_ITEMS = {"Bloodthirster", "Ravenous Hydra", "Riftmaker", "Bloodmail"}

# Items commonly cited as power spikes per role. Drives the SPIKES IN PLAY
# annotation in the coach user message — when an enemy holds one of these,
# their kill threat / siege power is meaningfully higher and the coach should
# weight macro decisions accordingly. Soft signal: missing entries just mean
# no annotation, no crash. Tune as patches land.
POWER_SPIKE_ITEMS = {
    # ADC
    "Infinity Edge", "The Collector", "Yun Tal Wildarrows", "Phantom Dancer",
    "Bloodthirster", "Lord Dominik's Regards", "Mortal Reminder",
    # Mage
    "Luden's Companion", "Stormsurge", "Liandry's Torment", "Shadowflame",
    "Rabadon's Deathcap",
    # Bruiser / fighter
    "Eclipse", "Stridebreaker", "Hullbreaker", "Sundered Sky",
    # Tank
    "Heartsteel", "Sunfire Aegis", "Jak'Sho, The Protean", "Spirit Visage",
    "Kaenic Rookern",
    # Assassin
    "Hubris", "Voltaic Cyclosword", "Edge of Night", "Profane Hydra",
}
TIER_COLOR = {
    'Extreme': '\033[91m',  # red
    'Major':   '\033[93m',  # yellow
    'Even':    '\033[97m',  # white
    'Minor':   '\033[96m',  # cyan
    'Tiny':    '\033[90m',  # grey
}
RESET = '\033[0m'
BOLD = '\033[1m'
DIM = '\033[2m'

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ─── utilities ──────────────────────────────────────────────────────────────

def normalize(name):
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def truncate(body, limit):
    body = re.sub(r'\s+', ' ', body).strip()
    if len(body) <= limit:
        return body
    return body[:limit].rsplit(' ', 1)[0] + '…'


def _clean_db_note(text, max_sentences=5):
    """Strip markdown headers, split into one sentence per line for display."""
    lines = [l for l in text.splitlines() if not l.lstrip().startswith('#')]
    collapsed = re.sub(r'\s+', ' ', ' '.join(lines)).strip()
    # split on sentence boundaries
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', collapsed) if s.strip()]
    return '\n  '.join(sentences[:max_sentences])


def windows_host_ip():
    """On WSL2, /etc/resolv.conf nameserver typically points at the Windows host."""
    try:
        with open('/etc/resolv.conf') as f:
            for line in f:
                if line.startswith('nameserver '):
                    return line.split()[1].strip()
    except OSError:
        pass
    return None


def candidate_hosts(explicit=None):
    if explicit:
        return [explicit]
    hosts = ['127.0.0.1']
    wh = windows_host_ip()
    if wh and wh not in hosts:
        hosts.append(wh)
    return hosts


# ─── matchup parsing ────────────────────────────────────────────────────────

def parse_matchups(md_path):
    """Return {normalized_name: (display_name, body, tier)}."""
    text = md_path.read_text()
    sections = {}
    current_tier = None
    current_champ = None
    buffer = []
    for line in text.splitlines():
        if line.startswith('## '):
            if current_champ:
                sections[normalize(current_champ)] = (current_champ, '\n'.join(buffer).strip(), current_tier)
                current_champ, buffer = None, []
            header = line[3:].strip()
            current_tier = None
            for t in TIER_ORDER:
                if header.lower().startswith(t.lower()):
                    current_tier = t
                    break
        elif line.startswith('### '):
            if current_champ:
                sections[normalize(current_champ)] = (current_champ, '\n'.join(buffer).strip(), current_tier)
            current_champ = line[4:].strip()
            buffer = []
        else:
            buffer.append(line)
    if current_champ:
        sections[normalize(current_champ)] = (current_champ, '\n'.join(buffer).strip(), current_tier)
    return sections


# ─── TTS (ElevenLabs or Edge TTS neural voices via powershell.exe from WSL) ──

TTS_VOICE = os.environ.get('LEEG_TTS_VOICE', 'en-US-AvaMultilingualNeural')  # edge-tts fallback
ELEVENLABS_VOICE = os.environ.get('LEEG_ELEVENLABS_VOICE', 'gllMMawbYGTja23oQ3Vu')  # Crystal
_tts_proc = None
_tts_lock = threading.Lock()
_tts_last_error = None
# Rotate between two MP3 slots so a new write never races with the file
# that WinMCI currently has open for playback.
_MP3_SLOTS = (
    '/mnt/c/Windows/Temp/leeg_tts_0.mp3',
    '/mnt/c/Windows/Temp/leeg_tts_1.mp3',
)
_WIN_MP3_SLOTS = (
    r'C:\Windows\Temp\leeg_tts_0.mp3',
    r'C:\Windows\Temp\leeg_tts_1.mp3',
)
_mp3_slot = 0


def _tts_mode():
    """Return which TTS backend will be used: 'elevenlabs', 'edge-tts', or 'sapi'."""
    if os.environ.get('ELEVENLABS_API_KEY'):
        try:
            import elevenlabs  # noqa: F401
            return 'elevenlabs'
        except ImportError:
            pass
    try:
        import edge_tts  # noqa: F401
        return 'edge-tts'
    except ImportError:
        pass
    return 'sapi'

_PS1 = r'C:\Windows\Temp\leeg_tts.ps1'
# PS1 takes -f <path>; closes any stale MCI device before opening the new file
# so the alias is always fresh and there's no leftover lock from a previous call.
_PS1_CONTENT = """\
param([string]$f)
Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class WinMCI { [DllImport("winmm.dll", CharSet=CharSet.Auto)] public static extern int mciSendString(string cmd, System.Text.StringBuilder ret, int retLen, IntPtr cb); }'
[WinMCI]::mciSendString('close leeg', $null, 0, [IntPtr]::Zero) | Out-Null
[WinMCI]::mciSendString("open `"$f`" type mpegvideo alias leeg", $null, 0, [IntPtr]::Zero) | Out-Null
[WinMCI]::mciSendString('play leeg wait', $null, 0, [IntPtr]::Zero) | Out-Null
[WinMCI]::mciSendString('close leeg', $null, 0, [IntPtr]::Zero) | Out-Null
"""


def warmup_tts():
    """Write the PS1 script to disk at startup so it's ready for the first call."""
    try:
        with open('/mnt/c/Windows/Temp/leeg_tts.ps1', 'w') as f:
            f.write(_PS1_CONTENT)
    except Exception:
        pass


def _speak_worker(text):
    global _tts_proc, _tts_last_error, _mp3_slot
    with _tts_lock:
        if _tts_proc is not None:
            try:
                _tts_proc.kill()
            except Exception:
                pass
            _tts_proc = None
        # Pick the slot that isn't currently playing, then advance for next call.
        slot = _mp3_slot
        _mp3_slot = 1 - _mp3_slot

    mp3_wsl = _MP3_SLOTS[slot]
    mp3_win = _WIN_MP3_SLOTS[slot]
    mp3_written = False

    # 1. ElevenLabs
    el_key = os.environ.get('ELEVENLABS_API_KEY')
    if el_key:
        try:
            from elevenlabs.client import ElevenLabs
            from elevenlabs import VoiceSettings
            el = ElevenLabs(api_key=el_key)
            audio = el.text_to_speech.convert(
                text=text, voice_id=ELEVENLABS_VOICE, model_id='eleven_flash_v2_5',
                voice_settings=VoiceSettings(stability=0.40, similarity_boost=0.75, style=0.60, use_speaker_boost=True, speed=1.0),
            )
            with open(mp3_wsl, 'wb') as f:
                for chunk in audio:
                    f.write(chunk)
            mp3_written = True
            _tts_last_error = None
        except Exception as exc:
            _tts_last_error = f'elevenlabs: {type(exc).__name__}: {str(exc)[:80]}'

    # 2. edge-tts fallback
    if not mp3_written:
        try:
            import asyncio
            import edge_tts
            asyncio.run(edge_tts.Communicate(text, TTS_VOICE).save(mp3_wsl))
            mp3_written = True
            _tts_last_error = None
        except ImportError:
            pass
        except Exception as exc:
            _tts_last_error = f'edge-tts: {type(exc).__name__}: {str(exc)[:80]}'

    # 3. Play MP3 via PS1 script
    if mp3_written:
        try:
            proc = subprocess.Popen(
                ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                 '-WindowStyle', 'Hidden', '-File', _PS1, '-f', mp3_win],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with _tts_lock:
                _tts_proc = proc
            proc.wait(timeout=30)
            with _tts_lock:
                if _tts_proc is proc:
                    _tts_proc = None
            return
        except Exception as exc:
            _tts_last_error = f'playback: {type(exc).__name__}: {str(exc)[:80]}'

    # 4. SAPI inline fallback (no MP3 needed)
    safe = re.sub(r'["\'\\\r\n]', ' ', text).strip()
    try:
        subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-WindowStyle', 'Hidden', '-Command',
             f'Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak("{safe}")'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _tts_last_error = None
    except Exception as exc:
        _tts_last_error = f'sapi: {type(exc).__name__}: {str(exc)[:80]}'


def _normalize_for_tts(text):
    """Expand gaming shorthand so it reads naturally aloud."""
    text = re.sub(r'([.!?]) · ', r'\1 ', text)   # already punctuated — just space
    text = re.sub(r' · ', '. ', text)             # no punctuation — add period
    text = text.replace(' — ', ', ').replace('—', ', ')
    text = re.sub(r' - ', ', ', text)             # spaced hyphen → pause
    text = re.sub(r'-{2,}', ', ', text)           # double/triple hyphen
    text = re.sub(r'(?<=[a-zA-Z])-(?=[a-zA-Z])', ' ', text)  # compound words: face-tank → face tank
    text = re.sub(r'(\d+)/(\d+)/(\d+)', r'\1 and \2 and \3', text)  # KDA
    text = re.sub(r'(\d+)/(\d+)', r'\1 and \2', text)               # K/D
    text = re.sub(r'\((\d+)s\)', r'\1 seconds', text)
    text = re.sub(r'(\d+)s\b', r'\1 seconds', text)
    text = re.sub(r'\((\d+)g\b\)', r'\1 gold', text)
    text = re.sub(r'(\d+)g\b', r'\1 gold', text)
    text = re.sub(r'\blvl\b', 'level', text, flags=re.IGNORECASE)
    text = re.sub(r'\bcs\b', 'CS', text)
    text = re.sub(r'(\d+)%', r'\1 percent', text)
    text = text.replace('(', '').replace(')', '')
    text = re.sub(r'\s{2,}', ' ', text)           # collapse any double spaces left behind
    return text.strip()


def speak_async(text):
    """Speak text via ElevenLabs (or Edge TTS fallback). Interrupts any current playback."""
    safe = re.sub(r'["\\\r\n]', ' ', text).strip()
    if not safe:
        return
    threading.Thread(target=_speak_worker, args=(_normalize_for_tts(safe),), daemon=True).start()


# ─── champ folder discovery & lazy loading ──────────────────────────────────

def available_champs():
    """Folder names under LEEG_ROOT that contain a build.md or matchups.md."""
    return sorted(
        d.name for d in LEEG_ROOT.iterdir()
        if d.is_dir() and ((d / 'matchups.md').exists() or (d / 'build.md').exists())
    )


def champ_to_folder(api_name, available):
    """Resolve a Live/LCU championName to a leeg/<folder> name, or None."""
    if not api_name:
        return None
    norm = normalize(api_name)
    for folder in available:
        if normalize(folder) == norm:
            return folder
    aliased = CHAMP_ALIASES.get(norm)
    if aliased and aliased in available:
        return aliased
    return None


def load_champ_data(champ_folder, cache):
    """Lazy-load + cache matchups and build variants for a champ folder."""
    if champ_folder in cache:
        return cache[champ_folder]
    matchups_path = LEEG_ROOT / champ_folder / 'matchups.md'
    build_path = LEEG_ROOT / champ_folder / 'build.md'
    playbook_path = LEEG_ROOT / champ_folder / 'playbook.md'
    cache[champ_folder] = {
        'matchups': parse_matchups(matchups_path) if matchups_path.exists() else {},
        'build_variants': parse_build_variants(build_path) if build_path.exists() else [],
        'swaps': parse_swap_rules(build_path),
        'win_conditions': parse_playbook_section(playbook_path, 'Win conditions'),
        'side_selection': parse_playbook_section(playbook_path, 'Side selection'),
    }
    return cache[champ_folder]


# ─── damage profile + build variant picking ─────────────────────────────────

def classify_enemy(name, items, item_index):
    """Per-enemy damage type. Items override archetype once ≥2 of one type."""
    fallback = CHAMP_DAMAGE.get(normalize(name))
    if not items or not item_index:
        return fallback
    ap_items = ad_items = 0
    for item in items:
        item_id = (item or {}).get('itemID')
        if not item_id:
            continue
        kind = (item_index.get(item_id) or {}).get('damage')
        if kind == 'AP':
            ap_items += 1
        elif kind == 'AD':
            ad_items += 1
    if ap_items >= 2 and ad_items >= 2:
        return 'Mixed'
    if ap_items >= 2 and ad_items < 2:
        return 'AP'
    if ad_items >= 2 and ap_items < 2:
        return 'AD'
    return fallback


def compute_damage_profile(enemies, item_index=None):
    """enemies: list of dicts each with 'championName' and optional 'items'.
    Returns ('AP'|'AD'|'Standard', ap_count, ad_count, n_known, n_items_overrides).
    n_items_overrides = how many enemies had their archetype changed by their items."""
    ap = ad = 0.0
    known = 0
    overrides = 0
    for e in enemies:
        name = e.get('championName', '')
        if not name:
            continue
        items = e.get('items') or []
        archetype = CHAMP_DAMAGE.get(normalize(name))
        kind = classify_enemy(name, items, item_index) if item_index else archetype
        if kind is None:
            continue
        known += 1
        if archetype is not None and kind != archetype:
            overrides += 1
        if kind == 'AP':
            ap += 1
        elif kind == 'AD':
            ad += 1
        else:  # Mixed
            ap += 0.5
            ad += 0.5
    if ap >= 3.5:
        label = 'AP'
    elif ad >= 3.5:
        label = 'AD'
    else:
        label = 'Standard'
    return label, ap, ad, known, overrides


def compute_team_threats(enemies, item_index=None):
    """Tag enemies by archetype (HEALING / HARD-CC / ATTACK-SPEED / TANK /
    ASSASSIN / CRIT) for the coach's TEAM THREATS block. Returns a dict with
    `tags` (tag -> [display names]), `counts`, roll-up `flags`, and
    `item_promotions` (champs whose healing items justify treating a single
    healer as stacked)."""
    tags = {}
    item_promotions = []

    healing_amp_ids = set()
    if item_index:
        for iid, info in item_index.items():
            if iid >= 200000:  # ARAM variants
                continue
            if (info or {}).get('name') in HEALING_AMP_ITEMS:
                healing_amp_ids.add(iid)

    for e in enemies:
        name = e.get('championName') or ''
        if not name:
            continue
        norm = normalize(name)
        threat_tags = CHAMP_THREAT_TAGS.get(norm) or []
        if 'HEALING' in threat_tags and healing_amp_ids:
            for it in (e.get('items') or []):
                iid = (it or {}).get('itemID')
                if iid in healing_amp_ids:
                    info = item_index.get(iid) or {}
                    item_promotions.append((name, 'HEALING', info.get('name', '?')))
                    break
        for tag in threat_tags:
            tags.setdefault(tag, []).append(name)

    counts = {tag: len(v) for tag, v in tags.items()}
    flags = {
        'stacked_healing': counts.get('HEALING', 0) >= 2 or bool(item_promotions),
        'attack_speed_stacked': counts.get('ATTACK-SPEED', 0) >= 2,
        'heavy_hard_cc': counts.get('HARD-CC', 0) >= 2,
        'has_tank': counts.get('TANK', 0) >= 1,
        'has_assassin': counts.get('ASSASSIN', 0) >= 1,
        'fed_crit': counts.get('CRIT', 0) >= 1,
    }
    return {'tags': tags, 'counts': counts, 'flags': flags, 'item_promotions': item_promotions}


def compute_phase(game_time, players):
    """Game phase label for the coach user message: 'early' / 'mid' / 'late'.
    Whichever threshold trips first wins (a fast-snowballing game can hit late
    before 25min). Avg level computed from `players` (Live API allPlayers)."""
    levels = [int(p.get('level') or 0) for p in (players or []) if p.get('level')]
    avg_level = (sum(levels) / len(levels)) if levels else 0
    if game_time >= 25 * 60 or avg_level >= 14:
        return 'late'
    if game_time >= 14 * 60 or avg_level >= 9:
        return 'mid'
    return 'early'


def compute_gold_lead(your_team, players, item_index):
    """Sum item gold per team. Returns (your_total, enemy_total). Live API
    doesn't expose enemy total gold — item gold is the better macro proxy
    anyway (gold already converted to power)."""
    if not item_index or not players or not your_team:
        return (0, 0)
    your_total = enemy_total = 0
    for p in players:
        team = p.get('team')
        if team not in ('ORDER', 'CHAOS'):
            continue
        team_total = 0
        for it in (p.get('items') or []):
            iid = (it or {}).get('itemID')
            if not iid or iid >= 200000:  # ARAM variants
                continue
            cost = (item_index.get(iid) or {}).get('cost') or 0
            team_total += cost
        if team == your_team:
            your_total += team_total
        else:
            enemy_total += team_total
    return (your_total, enemy_total)


def has_power_spike_items(items, item_index):
    """Return ordered list of POWER_SPIKE display names this enemy holds."""
    if not items or not item_index:
        return []
    out = []
    for it in items:
        iid = (it or {}).get('itemID')
        if not iid or iid >= 200000:
            continue
        name = (item_index.get(iid) or {}).get('name')
        if name and name in POWER_SPIKE_ITEMS:
            out.append(name)
    return out


_SWAP_LINE_RE = re.compile(r'^\s*-\s*(.+?)\s+over\s+(.+?)\s+[—–-]+\s+(.+)$')


def _parse_situational_swaps(text):
    """Parse '## Situational item swaps' lines into (new_item, old_item, condition) triples."""
    swaps, in_swaps = [], False
    for line in text.splitlines():
        if line.startswith('## '):
            in_swaps = 'situational item swap' in line.lower()
            continue
        if not in_swaps:
            continue
        m = _SWAP_LINE_RE.match(line)
        if m:
            swaps.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip()))
    return swaps


def parse_swap_rules(build_path):
    """Read situational swap triples from a champion's build.md path. Used by
    load_champ_data so the coach user message can surface the player champ's
    own swap rules without re-parsing every render."""
    if not build_path or not build_path.exists():
        return []
    return _parse_situational_swaps(build_path.read_text())


def parse_playbook_section(playbook_path, header):
    """Return the body under '## <header>' up to the next '## ', stripped.
    Empty string if file missing or section absent. Header match is exact
    (case-insensitive) on the text after '## '."""
    if not playbook_path or not playbook_path.exists():
        return ''
    target = (header or '').strip().lower()
    if not target:
        return ''
    out, in_section = [], False
    for line in playbook_path.read_text().splitlines():
        if line.startswith('## '):
            if in_section:
                break
            in_section = line[3:].strip().lower() == target
            continue
        if in_section:
            out.append(line)
    return '\n'.join(out).strip()


def _swap_damage_label(condition):
    """Map a swap condition string to the damage profile it applies to, or None."""
    c = condition.lower()
    if re.search(r'\bheavy (ad|crit)\b|full ad', c):
        return 'AD'
    if re.search(r'\b(heavy|full) ap\b', c):
        return 'AP'
    return None


def parse_build_variants(md_path):
    """Find ### sections under any ## that mentions 'build' or 'example'.
    Returns list of (heading, body). Synthesizes AD/AP variants from the
    '## Situational item swaps' section when no explicit variant exists."""
    text = md_path.read_text()
    variants = []
    in_build_section = False
    current_heading = None
    current_body = []

    def flush():
        if current_heading:
            variants.append((current_heading, '\n'.join(current_body).strip()))

    for line in text.splitlines():
        if line.startswith('## '):
            flush()
            current_heading, current_body = None, []
            section = line[3:].strip().lower()
            in_build_section = 'build' in section or 'example' in section
        elif line.startswith('### ') and in_build_section:
            flush()
            current_heading = line[4:].strip()
            current_body = []
        elif current_heading is not None:
            current_body.append(line)
    flush()

    swaps = _parse_situational_swaps(text)
    if swaps:
        std_body = next((b for h, b in variants if classify_variant(h) == 'Standard'), None)
        if std_body:
            for label in ('AD', 'AP'):
                if not any(classify_variant(h) == label for h, b in variants):
                    body = std_body
                    for new_item, old_item, cond in swaps:
                        if _swap_damage_label(cond) == label:
                            body = body.replace(old_item, new_item)
                    if body != std_body:
                        variants.append((f'vs Heavy {label} (auto)', body))

    return variants


def classify_variant(heading):
    h = heading.lower()
    if re.search(r'\b(heavy|full|vs|stacked)\s+(ap|magic|mr)\b', h) or 'magic damage' in h:
        return 'AP'
    if re.search(r'\b(heavy|full|vs|stacked)\s+(ad|armor)\b', h) or 'physical damage' in h:
        return 'AD'
    if re.search(r'\b(standard|core|current)\b', h):
        return 'Standard'
    return None  # split push, jungle, etc. — never auto-picked


def laner_build_tag(matchup_entry):
    """Return the 'Build: <tag>' value from a matchup entry body, or None.
    Lets matchup notes flag which build variant to prefer for a specific laner.
    E.g. a line 'Build: no-warmogs' in the Nasus note selects ### No Warmog's."""
    if not matchup_entry:
        return None
    _, body, _ = matchup_entry
    for line in (body or '').splitlines():
        m = re.match(r'^\s*build:\s*(.+)$', line, re.IGNORECASE)
        if m:
            return m.group(1).strip().lower()
    return None


def pick_build_variant(variants, profile_kind, preferred_tag=None):
    """Match comp profile to a build.md variant. preferred_tag (from a matchup
    note 'Build:' line) is tried first; falls back to damage profile, then Standard."""
    if preferred_tag:
        tag_norm = normalize(preferred_tag)
        for heading, body in variants:
            if tag_norm in normalize(heading):
                return heading, body
    for heading, body in variants:
        if classify_variant(heading) == profile_kind:
            return heading, body
    if profile_kind != 'Standard':
        for heading, body in variants:
            if classify_variant(heading) == 'Standard':
                return heading, body
    return None


def build_path_summary(body, max_items=10):
    """Extract a one-line ' · '-separated build path from a variant body."""
    lines = [l for l in body.splitlines() if l.strip() and not l.lstrip().startswith('>')]
    if not lines:
        return ''
    first = lines[0].strip()
    if '·' in first:
        return first
    items = []
    for line in lines:
        m = re.match(r'^\s*\d+\.\s*(.+)$', line)
        if m:
            items.append(m.group(1).strip())
            if len(items) >= max_items:
                break
        elif items:
            break
    if items:
        return ' · '.join(items)
    return first[:120]


# ─── champion ID → name index (CommunityDragon) ─────────────────────────────

CHAMPION_INDEX_URL = 'https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/champion-summary.json'
ITEMS_INDEX_URL = 'https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/items.json'
DDRAGON_VERSIONS_URL = 'https://ddragon.leagueoflegends.com/api/versions.json'
META_FILENAME = 'meta.json'


def _cdragon_get(url):
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; leeg/1.0)'},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def fetch_champion_index():
    """Returns ({id: display_name}, [alias, ...]). Empty pair on failure.
    Filters out Doom Bot event entries so they never surface in champ select."""
    try:
        data = _cdragon_get(CHAMPION_INDEX_URL)
        names, aliases = {}, []
        for c in data:
            cid = c.get('id', -1)
            if cid <= 0:
                continue
            name = c.get('name', f'#{cid}')
            if name.startswith('Doom Bot'):
                continue
            names[cid] = name
            alias = c.get('alias')
            if alias:
                aliases.append(alias)
        return names, aliases
    except Exception:
        return {}, []


def classify_item(categories):
    """Return 'AP' | 'AD' | 'Tank' | 'Other' for an item by its categories list.
    Priority: explicit damage tags first (SpellDamage > Damage > Crit/ArmorPen),
    then AS as AD-aligned (Wit's End/Nashor's are rare), then tank stats."""
    cats = set(categories or [])
    if 'SpellDamage' in cats or 'MagicPenetration' in cats:
        return 'AP'
    if 'Damage' in cats or 'CriticalStrike' in cats or 'ArmorPenetration' in cats:
        return 'AD'
    if 'AttackSpeed' in cats:
        return 'AD'
    if cats & {'Health', 'Armor', 'SpellBlock', 'HealthRegen'}:
        return 'Tank'
    return 'Other'


def _clean_build_name(name):
    """Strip markdown and parentheticals from a build.md item entry."""
    if not name:
        return ''
    name = re.sub(r'\*+', '', name)
    name = re.sub(r'\s*\([^)]*\)\s*', ' ', name)
    return name.strip()




# Starter / consumable items that should NEVER occupy a slot in live_build.
# Match by normalized name so casing/punctuation/possessives don't matter.
_STARTER_ITEM_NAMES = {
    "doransshield", "doransblade", "doransring", "doransbow", "doranshelm",
    "cull", "spellthiefsedge", "relicshield", "steelshoulderguards",
    "spectralsickle", "tearofthegoddess", "darkseal",
    "healthpotion", "refillablepotion", "corruptingpotion",
    "stealthward", "oraclelens", "controlward", "wardingtotem",
    "slightlymagicalfootwear", "boots",
}


def _build_name_to_id(item_index):
    """Map normalized item name -> id for non-ARAM items."""
    out = {}
    if not item_index:
        return out
    for iid, info in item_index.items():
        if iid >= 200000:
            continue
        n = normalize(info.get('name', ''))
        if n and n not in out:
            out[n] = iid
    return out


def resolve_item_id(name, name_to_id):
    """Resolve a free-form item name to its canonical item id. Tries exact
    normalized match, then prefix match, then substring match. Returns None
    if no unambiguous match.
    Handles e.g. 'Jak\\'Sho' (build.md short form) -> Jak\\'Sho, The Protean."""
    n = normalize(_clean_build_name(name) or name)
    if not n or not name_to_id:
        return None
    if n in name_to_id:
        return name_to_id[n]
    pref = [iid for k, iid in name_to_id.items() if k.startswith(n) or n.startswith(k)]
    if len(set(pref)) == 1:
        return pref[0]
    sub = [iid for k, iid in name_to_id.items() if n in k or k in n]
    if len(set(sub)) == 1:
        return sub[0]
    return None


def validate_item_names(item_names, item_index):
    """Drop any name that doesn't resolve to a real item in item_index.
    Stops hallucinated items from reaching live_build."""
    if not item_index:
        return list(item_names or [])
    name_to_id = _build_name_to_id(item_index)
    out = []
    for name in item_names or []:
        if resolve_item_id(name, name_to_id) is not None:
            out.append(name)
    return out


def compute_build_diverged(live_build, build_pick, my_items, item_index=None):
    """Returns True if live_build's un-owned tail differs from the rule-based
    default's un-owned tail. Comparison is by item ID when item_index is
    provided so 'Jak\\'Sho' and 'Jak\\'Sho, The Protean' compare equal.
    Owned items are removed from both sides first so 'owned pulled to front'
    doesn't register as divergence."""
    if not build_pick or not live_build:
        return False
    _heading, body = build_pick
    summary = build_path_summary(body)
    default_names = [n.strip() for n in summary.split('·') if n.strip()]

    name_to_id = _build_name_to_id(item_index) if item_index else {}
    owned_ids = {it.get('itemID') for it in (my_items or []) if it and it.get('itemID')}

    def to_keys(items):
        keys = []
        for raw in items:
            cleaned = _clean_build_name(raw) or raw
            if not cleaned:
                continue
            iid = resolve_item_id(cleaned, name_to_id) if name_to_id else None
            key = iid if iid is not None else normalize(cleaned)
            if key in owned_ids:
                continue
            # Also skip if key is the normalized name of an owned item (when
            # we couldn't resolve to an id, fall back to name comparison).
            keys.append(key)
        return keys

    return to_keys(live_build) != to_keys(default_names)


_BUY_VERBS = ('BACK', 'RECALL', 'BUY', 'FINISH', 'RUSH', 'GET')


def affordability_postcheck(bullets, current_gold, item_index):
    """Server-side enforcement of the affordability rule. For each bullet that
    uses a BACK/BUY-class verb and names an item whose cheapest component cost
    exceeds current_gold, replace the verb with FARM and append the shortfall.
    Stops the LLM from telling the user to BACK at 300g."""
    if not item_index or not bullets:
        return list(bullets or [])
    name_to_id = _build_name_to_id(item_index)
    sorted_keys = sorted(name_to_id.keys(), key=len, reverse=True)
    out = []
    for b in bullets:
        # Only treat as a buy intent if the verb is near the start of the
        # bullet (first 3 words). Avoids rewriting incidental uses like
        # "stay alive — back off the wave" or "GET behind tower".
        head = ' '.join(b.split()[:3])
        verb_match = None
        for v in _BUY_VERBS:
            m = re.search(rf'\b{v}\b', head, flags=re.IGNORECASE)
            if m and (verb_match is None or m.start() < verb_match.start()):
                verb_match = m
        if not verb_match:
            out.append(b)
            continue
        # Re-find against the full bullet so substitution targets the right span.
        verb_match = re.search(rf'\b{verb_match.group(0)}\b', b, flags=re.IGNORECASE)
        # Find the EARLIEST item name appearing after the verb. That's the
        # target the user is being told to back/buy/finish for.
        remainder_norm = re.sub(r'[^a-z0-9]', '', b[verb_match.end():].lower())
        target_id = None
        target_pos = len(remainder_norm) + 1
        for nm in sorted_keys:
            if len(nm) < 5 or nm in _STARTER_ITEM_NAMES:
                continue
            pos = remainder_norm.find(nm)
            if pos == -1 or pos >= target_pos:
                continue
            target_pos = pos
            target_id = name_to_id[nm]
        if target_id is None:
            out.append(b)
            continue
        info = item_index.get(target_id) or {}
        target_cost = info.get('cost') or 0
        target_name = info.get('name', '?')
        if target_cost <= 0 or current_gold >= target_cost:
            out.append(b)
            continue
        shortfall = target_cost - current_gold
        verb_hit = verb_match.group(0)
        rewritten = re.sub(rf'\b{verb_hit}\b', 'FARM', b, count=1)
        rewritten = re.sub(r'\b[Nn]ow\b\s*[—–-]?\s*', '', rewritten, count=1).strip()
        rewritten = re.sub(r'\s{2,}', ' ', rewritten).strip(' ,—–-')
        out.append(f'{rewritten}, farm toward {target_name}')
    return out


def strip_components(item_names, item_index):
    """Remove component items from live_build when their parent item is also in the build.
    Also deduplicates. Prevents the LLM from filling build slots with intermediate components."""
    if not item_index or not item_names:
        return item_names
    name_to_id = {}
    for iid, info in item_index.items():
        n = normalize(info.get('name', ''))
        if n and n not in name_to_id:
            name_to_id[n] = iid
    build_ids = set()
    for name in item_names:
        iid = resolve_item_id(name, name_to_id)
        if iid:
            build_ids.add(iid)
    out = []
    seen_ids = set()
    for name in item_names:
        iid = resolve_item_id(name, name_to_id)
        if iid:
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            info = item_index.get(iid) or {}
            into_ids = info.get('into') or []
            if any(pid in build_ids for pid in into_ids):
                continue  # component of a planned item — skip
        out.append(name)
    return out


def strip_starters(item_names):
    """Drop starter/consumable items from a live_build list. The LLM sometimes
    pulls owned starters into live_build and bumps a core item out; this is the
    deterministic backstop."""
    out = []
    for name in item_names or []:
        if normalize(_clean_build_name(name) or name) in _STARTER_ITEM_NAMES:
            continue
        out.append(name)
    return out


# Items the coach commonly references for situational pivots. Build-path items
# are added dynamically per game. Components are pulled in transitively from
# item_index, so listing the parent here is enough.
COACH_REFERENCE_ITEMS = [
    "Bramble Vest", "Thornmail",
    "Plated Steelcaps", "Mercury's Treads", "Boots",
    "Spectre's Cowl", "Force of Nature", "Spirit Visage",
    "Hexdrinker", "Maw of Malmortius",
    "Executioner's Calling", "Mortal Reminder",
    "Oblivion Orb", "Morellonomicon",
    "Chempunk Chainsword",
    "Lord Dominik's Regards",
    "Frozen Heart",
    "Randuin's Omen",
    "Quicksilver Sash", "Silvermere Dawn",
]


def format_item_reference(item_index, extra_names=None):
    """Return a list of authoritative '<name> (<cost>g) ← components' lines for
    the coach's reference items plus any extras (build path, owned items).
    Components referenced by parents are pulled in transitively so the model
    has component costs too."""
    if not item_index:
        return []
    name_to_id = {}
    for iid, info in item_index.items():
        if iid >= 200000:  # skip ARAM variants
            continue
        n = normalize(info.get('name', ''))
        if n and n not in name_to_id:
            name_to_id[n] = iid
    seen = set()
    pending = list(COACH_REFERENCE_ITEMS) + list(extra_names or [])
    while pending:
        nm = pending.pop()
        iid = name_to_id.get(normalize(_clean_build_name(nm) or nm))
        if not iid or iid in seen:
            continue
        seen.add(iid)
        info = item_index.get(iid) or {}
        for comp in info.get('from') or []:
            if comp not in seen:
                cinfo = item_index.get(comp) or {}
                cn = cinfo.get('name')
                if cn:
                    pending.append(cn)
    ordered = sorted(seen, key=lambda i: (item_index[i].get('cost') or 0))
    out = []
    for iid in ordered:
        info = item_index[iid]
        nm = info.get('name', '?')
        cost = info.get('cost', 0)
        comps = info.get('from') or []
        if comps:
            cs = []
            for c in comps:
                ci = item_index.get(c) or {}
                cs.append(f'{ci.get("name","?")} {ci.get("cost",0)}g')
            out.append(f'  {nm} ({cost}g) <= {" + ".join(cs)}')
        else:
            out.append(f'  {nm} ({cost}g, basic)')
    return out


_THREAT_TAG_ORDER = ('HEALING', 'HARD-CC', 'ATTACK-SPEED', 'TANK', 'ASSASSIN', 'CRIT')


def format_team_threats(threats):
    """Render the TEAM THREATS block from compute_team_threats output. Returns
    [] when no enemies were tagged so the caller can skip the block entirely."""
    if not threats or not threats.get('tags'):
        return []
    flags_active = [k.replace('_', '-') for k, v in (threats.get('flags') or {}).items() if v]
    out = []
    if flags_active:
        out.append('TEAM THREATS: ' + ' · '.join(flags_active))
    else:
        out.append('TEAM THREATS:')
    for tag in _THREAT_TAG_ORDER:
        names = (threats['tags'] or {}).get(tag)
        if names:
            out.append(f'  {tag} ({len(names)}): {", ".join(names)}')
    for champ, tag, item in threats.get('item_promotions') or []:
        out.append(f'  Item-amped: {champ} building {item} (still {tag.lower()} threat)')
    return out


def format_swap_rules(swaps):
    """Render the YOUR SWAP OPTIONS block from parse_swap_rules output."""
    if not swaps:
        return []
    out = ['YOUR SWAP OPTIONS (from build.md):']
    for new_item, old_item, condition in swaps:
        out.append(f'  {new_item} over {old_item} — {condition}')
    return out


def fetch_item_index():
    """Returns {itemID: {'damage', 'name', 'from', 'into', 'cost'}}. Empty dict on failure.
    'damage' is 'AP'|'AD'|'Tank'|'Other'. 'from'/'into' are component/parent item IDs.
    Used by classify_enemy() (damage profile) and format_item_reference() (coach prompt)."""
    try:
        data = _cdragon_get(ITEMS_INDEX_URL)
        index = {}
        for it in data:
            iid = it.get('id')
            if not iid:
                continue
            cost = it.get('priceTotal')
            if cost is None:
                cost = (it.get('gold') or {}).get('total', 0)
            index[iid] = {
                'damage': classify_item(it.get('categories') or []),
                'name': it.get('name', f'#{iid}'),
                'from': [int(x) for x in (it.get('from') or []) if str(x).isdigit()],
                'into': [int(x) for x in (it.get('into') or []) if str(x).isdigit()],
                'cost': int(cost or 0),
            }
        return index
    except Exception:
        return {}


# ─── patch detection + per-champ metadata ───────────────────────────────────

def parse_patch(version_str):
    """'16.8.1' -> (16, 8). Returns None if unparseable."""
    if not version_str:
        return None
    m = re.match(r'^(\d+)\.(\d+)', version_str)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def fmt_patch(tup):
    return f'{tup[0]}.{tup[1]}' if tup else None


def fetch_current_patch():
    """Returns 'major.minor' string or None on failure."""
    try:
        with urllib.request.urlopen(DDRAGON_VERSIONS_URL, timeout=5) as resp:
            versions = json.loads(resp.read())
        if versions:
            return fmt_patch(parse_patch(versions[0]))
    except Exception:
        pass
    return None


def read_meta(champ_folder):
    p = LEEG_ROOT / champ_folder / META_FILENAME
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_meta(champ_folder, meta):
    p = LEEG_ROOT / champ_folder / META_FILENAME
    p.write_text(json.dumps(meta, indent=2) + '\n')


def patch_drift(reviewed_str, current_str):
    """Returns a human description of drift, or None if not behind / unknowable."""
    r, c = parse_patch(reviewed_str), parse_patch(current_str)
    if not r or not c or r >= c:
        return None
    if r[0] == c[0]:
        n = c[1] - r[1]
        return f'{n} patch{"es" if n > 1 else ""} behind ({reviewed_str} → {current_str})'
    return f'reviewed for {reviewed_str}, current is {current_str}'


# ─── Live Client Data API (in-game) ─────────────────────────────────────────

def fetch_game(hosts):
    for h in hosts:
        url = f'https://{h}:2999/liveclientdata/allgamedata'
        try:
            with urllib.request.urlopen(url, context=SSL_CTX, timeout=2) as resp:
                return json.loads(resp.read()), h
        except (URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            continue
    return None, None


def find_active_team(data):
    you = data.get('activePlayer') or {}
    your_name = you.get('summonerName') or you.get('riotId') or ''
    your_game_name = your_name.split('#', 1)[0] if your_name else ''
    players = data.get('allPlayers') or []

    me = None
    for p in players:
        if not your_name:
            break
        if your_name in (p.get('summonerName'), p.get('riotId')):
            me = p
            break
        if your_game_name and your_game_name == p.get('riotIdGameName'):
            me = p
            break

    your_team = me.get('team') if me else None
    your_champ = me.get('championName') if me else None
    # Deduplicate allPlayers by player identity first; the Live API occasionally
    # returns duplicate entries for the same player (same summonerName/riotId).
    seen_ids: set = set()
    unique_players = []
    for p in players:
        pid = p.get('summonerName') or p.get('riotId') or p.get('riotIdGameName') or p.get('championName')
        if pid and pid in seen_ids:
            continue
        if pid:
            seen_ids.add(pid)
        unique_players.append(p)
    enemies = (
        [p for p in unique_players if p.get('team') and p.get('team') != your_team]
        if your_team else []
    )
    # Stamp the authoritative game name (from activePlayer) onto me so event
    # matching always uses the current identity, not the old summonerName.
    if me is not None and your_game_name:
        me = dict(me)
        me['_you_name'] = your_game_name
    return your_champ, enemies, me


# ─── events, timers, tactical advice ────────────────────────────────────────

def parse_events(data):
    """Pull data.events.Events into a structured summary."""
    events = (data.get('events') or {}).get('Events') or []
    summary = {
        'drakes': [], 'barons': [], 'heralds': [], 'grubs': 0,
        'towers': [], 'inhibs': [], 'kills': [],
        'first_blood': None, 'aces': [], 'multi': [],
        'raw': events,
    }
    for e in events:
        name = e.get('EventName', '')
        et = float(e.get('EventTime', 0))
        if name == 'DragonKill':
            summary['drakes'].append((et, e.get('DragonType', '?'), e.get('KillerName', '?'), bool(e.get('Stolen'))))
        elif name == 'BaronKill':
            summary['barons'].append((et, e.get('KillerName', '?'), bool(e.get('Stolen'))))
        elif name == 'HeraldKill':
            summary['heralds'].append((et, e.get('KillerName', '?')))
        elif name in ('HordeKill', 'VoidGrubKill', 'VoidgrubsKill'):
            summary['grubs'] += 1
        elif name == 'TurretKilled':
            summary['towers'].append((et, e.get('KillerName', '?'), e.get('TurretKilled', '?')))
        elif name == 'InhibKilled':
            summary['inhibs'].append((et, e.get('KillerName', '?'), e.get('InhibKilled', '?')))
        elif name == 'ChampionKill':
            summary['kills'].append((et, e.get('KillerName', '?'), e.get('VictimName', '?'), e.get('Assisters') or []))
        elif name == 'FirstBlood':
            summary['first_blood'] = (et, e.get('Recipient', '?'))
        elif name == 'Ace':
            summary['aces'].append((et, e.get('Acer', '?'), e.get('AcingTeam', '?')))
        elif name == 'Multikill':
            summary['multi'].append((et, e.get('KillerName', '?'), int(e.get('KillStreak', 0))))
    return summary


def objective_timers(game_time, ev):
    """Returns dict objective→seconds_until_spawn (0 = up now). Patch-approximate."""
    timers = {}
    DRAKE_FIRST, DRAKE_RESPAWN = 5*60, 5*60
    BARON_FIRST, BARON_RESPAWN = 25*60, 6*60
    if ev['drakes']:
        timers['drake'] = max(0, int(ev['drakes'][-1][0] + DRAKE_RESPAWN - game_time))
    elif game_time < DRAKE_FIRST:
        timers['drake'] = int(DRAKE_FIRST - game_time)
    else:
        timers['drake'] = 0
    if ev['barons']:
        timers['baron'] = max(0, int(ev['barons'][-1][0] + BARON_RESPAWN - game_time))
    elif game_time < BARON_FIRST:
        timers['baron'] = int(BARON_FIRST - game_time)
    else:
        timers['baron'] = 0
    return timers


def fmt_mmss(seconds):
    if seconds is None:
        return '—'
    mins, secs = divmod(int(seconds), 60)
    return f'{mins}:{secs:02d}'


def fmt_turret(s):
    """Turret_T1_C_05_A → 'T1 mid nexus'."""
    parts = (s or '').split('_')
    if len(parts) < 4 or parts[0] != 'Turret':
        return s
    lane = {'C': 'mid', 'L': 'top', 'R': 'bot'}.get(parts[2], parts[2])
    tier = {'01': 'outer', '02': 'inner', '03': 'inhib', '04': 'nexus', '05': 'nexus'}.get(parts[3], parts[3])
    return f'{parts[1]} {lane} {tier}'


def format_event(e):
    """Format a single event line, or None to skip."""
    et = int(float(e.get('EventTime', 0)))
    mins, secs = divmod(et, 60)
    ts = f'{mins:>2}:{secs:02d}'
    name = e.get('EventName', '')
    if name == 'ChampionKill':
        return f'{ts}  KILL    {e.get("KillerName","?")} → {e.get("VictimName","?")}'
    if name == 'DragonKill':
        tag = ' STOLEN' if e.get('Stolen') else ''
        return f'{ts}  DRAKE   {e.get("DragonType","?")} → {e.get("KillerName","?")}{tag}'
    if name == 'BaronKill':
        tag = ' STOLEN' if e.get('Stolen') else ''
        return f'{ts}  BARON   → {e.get("KillerName","?")}{tag}'
    if name == 'HeraldKill':
        return f'{ts}  HERALD  → {e.get("KillerName","?")}'
    if name in ('HordeKill', 'VoidGrubKill', 'VoidgrubsKill'):
        return f'{ts}  GRUB    → {e.get("KillerName","?")}'
    if name == 'TurretKilled':
        return f'{ts}  TOWER   {fmt_turret(e.get("TurretKilled",""))}'
    if name == 'InhibKilled':
        return f'{ts}  INHIB   {fmt_turret(e.get("InhibKilled",""))}'
    if name == 'FirstBlood':
        return f'{ts}  FIRST B {e.get("Recipient","?")}'
    if name == 'Multikill':
        ks = int(e.get('KillStreak', 0))
        label = {2: 'DOUBLE', 3: 'TRIPLE', 4: 'QUADRA', 5: 'PENTA'}.get(ks, f'{ks}x')
        return f'{ts}  {label:<7} {e.get("KillerName","?")}'
    if name == 'Ace':
        return f'{ts}  ACE     {e.get("AcingTeam","?")}'
    return None


def _next_buy_hint(me, build_pick, item_index, current_gold, committed_items=None):
    """Deterministic 'what to back for' line. Walks committed_items (coach's
    locked-in path) when available, else build_pick's path. Returns a string or None."""
    if not me or not item_index:
        return None
    if committed_items:
        names = committed_items
    elif build_pick:
        summary = build_path_summary(build_pick[1])
        names = [n.strip() for n in summary.split('·') if n.strip()]
    else:
        return None
    if not names:
        return None
    name_to_id = _build_name_to_id(item_index)
    owned_ids = {(i or {}).get('itemID') for i in (me.get('items') or []) if (i or {}).get('itemID')}
    for raw in names:
        iid = resolve_item_id(raw, name_to_id)
        if not iid or iid in owned_ids:
            continue
        info = item_index.get(iid) or {}
        item_name = info.get('name', raw)
        item_cost = info.get('cost') or 0
        components = info.get('from') or []
        unowned_comps = [c for c in components if c not in owned_ids]
        if unowned_comps:
            costs = sorted(
                ((c, (item_index.get(c) or {}).get('cost') or 0) for c in unowned_comps),
                key=lambda x: x[1],
            )
            cheap_id, cheap_cost = costs[0]
            cheap_name = (item_index.get(cheap_id) or {}).get('name', '?')
            big_id, big_cost = costs[-1]
            big_name = (item_index.get(big_id) or {}).get('name', '?')
            if current_gold >= item_cost:
                return f'BACK — {current_gold}g buys {item_name} ({item_cost}g)'
            if current_gold >= cheap_cost:
                return f'BACK — {current_gold}g, saving toward {item_name} ({item_cost}g)'
            return f'next: {item_name} ({item_cost}g) — farm {big_cost - current_gold}g for {big_name}'
        if current_gold >= item_cost:
            return f'BACK — {current_gold}g buys {item_name} ({item_cost}g)'
        return f'next: {item_name} ({item_cost}g) — farm {item_cost - current_gold}g'
    return None


def tactical_advice(data, me, enemies, ev, timers, build_pick=None, item_index=None, committed_items=None):
    """Return list of (priority, message). 0=immediate threat, 1=push, 2=objective, 3=macro."""
    advice = []
    if not me:
        return advice
    game_time = int((data.get('gameData') or {}).get('gameTime', 0))
    my_pos = (me.get('position') or '').upper()
    my_scores = me.get('scores') or {}
    my_items = [i for i in (me.get('items') or []) if (i or {}).get('itemID')]

    laner = None
    if my_pos:
        for e in enemies:
            if (e.get('position') or '').upper() == my_pos:
                laner = e
                break

    if me.get('isDead'):
        rt = int(me.get('respawnTimer') or 0)
        advice.append((0, f'YOU DEAD ({rt}s) — track minimap, ping objectives'))

    if laner and laner.get('isDead'):
        rt = int(laner.get('respawnTimer') or 0)
        advice.append((1, f'PUSH WAVE — {laner.get("championName")} dead ({rt}s)'))

    drake = timers.get('drake')
    if drake is not None:
        if 0 < drake <= 60:
            advice.append((2, f'Drake spawns in {fmt_mmss(drake)} — reset & rotate'))
        elif drake == 0 and game_time >= 5*60:
            advice.append((2, 'Drake UP — group / contest'))

    baron = timers.get('baron')
    if baron is not None and game_time >= 23*60:
        if 0 < baron <= 60:
            advice.append((2, f'Baron spawns in {fmt_mmss(baron)} — get vision'))
        elif baron == 0:
            advice.append((2, 'Baron UP — vision/contest'))

    if laner and game_time > 8*60:
        my_cs = my_scores.get('creepScore', 0)
        their_cs = (laner.get('scores') or {}).get('creepScore', 0)
        diff = my_cs - their_cs
        if diff <= -25:
            advice.append((3, f'CS down {abs(diff)} — focus farm, avoid trades'))
        elif diff >= 25:
            advice.append((3, f'CS up {diff} — keep tempo, look for pick'))

    if laner:
        their_items = [i for i in (laner.get('items') or []) if (i or {}).get('itemID')]
        gap = len(their_items) - len(my_items)
        if gap >= 2:
            advice.append((3, f'Laner ahead {gap} items — play very safe, ward deep'))

    for e in enemies:
        sc = e.get('scores') or {}
        k, d, a = sc.get('kills', 0), sc.get('deaths', 0), sc.get('assists', 0)
        if k - d >= 5 and k >= 5:
            advice.append((1, f'{e.get("championName")} fed ({k}/{d}/{a}) — peel, no 1v1'))

    if item_index and (build_pick or committed_items):
        current_gold = int((data.get('activePlayer') or {}).get('currentGold') or 0)
        hint = _next_buy_hint(me, build_pick, item_index, current_gold, committed_items=committed_items)
        if hint:
            prio = 1 if hint.startswith('BACK') else 3
            advice.append((prio, hint))

    advice.sort(key=lambda x: x[0])
    return advice[:5]


# ─── LLM coach (Claude) ─────────────────────────────────────────────────────

COACH_SCHEMA = {
    "type": "object",
    "properties": {
        "bullets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1-2 short tactical bullet lines, each <=90 chars. Warm, direct, spoken-word tone — like a flirty girlfriend who knows the game.",
        },
        "live_build": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Your recommended 4-6 core-item build path for this game, in build order. Owned core items appear first in their built positions; planned core items follow. Stable across calls — only change when game state materially shifts (enemy pivots damage profile, fed carry, new objective threat).",
        },
        "build_change_reason": {
            "type": "string",
            "description": "If you intentionally deviated from the RULE-BASED BUILD DEFAULT, a short reason (<=100 chars). Empty string if you're following the default.",
        },
    },
    "required": ["bullets", "live_build", "build_change_reason"],
    "additionalProperties": False,
}


class ChampDB:
    """Universal per-opponent champion notes, lazily generated by Haiku and cached to disk.

    Notes are generated once per champion on first encounter and reused across games.
    Generation runs in a daemon thread so it never blocks the render loop.
    """

    _NOTES_PATH = DATA_DIR / 'champ_notes.json'

    def __init__(self, client=None, patch=None):
        self._lock = threading.Lock()
        self._notes = {}   # normalized_name -> {display_name, patch, notes}
        self._pending = set()
        self.client = client
        self.patch = patch or 'unknown'
        self._load()

    def _load(self):
        if self._NOTES_PATH.exists():
            try:
                data = json.loads(self._NOTES_PATH.read_text())
                self._notes = data.get('champions', {})
            except Exception:
                pass

    def _save(self):
        DATA_DIR.mkdir(exist_ok=True)
        self._NOTES_PATH.write_text(
            json.dumps({'version': 1, 'champions': self._notes}, indent=2)
        )

    def get_note(self, champ_name):
        """Return cached note string, or None if not yet generated."""
        key = normalize(champ_name)
        with self._lock:
            entry = self._notes.get(key)
        return entry['notes'] if entry else None

    def ensure_note_async(self, champ_name):
        """Kick off background note generation if note is missing or 3+ patches stale."""
        if not self.client:
            return
        key = normalize(champ_name)
        with self._lock:
            if key in self._pending:
                return
            entry = self._notes.get(key)
            if entry:
                current = parse_patch(self.patch)
                stored = parse_patch(entry.get('patch', ''))
                if current and stored:
                    stale = (current[1] - stored[1] >= 3) if current[0] == stored[0] else True
                    if not stale:
                        return
                else:
                    return  # can't compare, keep existing
            self._pending.add(key)
        threading.Thread(target=self._generate, args=(champ_name, key), daemon=True).start()

    def _generate(self, display_name, key):
        try:
            client = self.client.with_options(timeout=20.0, max_retries=0)
            resp = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=200,
                messages=[{
                    'role': 'user',
                    'content': (
                        f'League of Legends: 3-4 short imperative sentences (max 80 words total) on how to play against {display_name}. '
                        f'Cover: their core threat, when they spike, a key exploit window, and one item or build tip. '
                        f'No headers, no preamble, no markdown. '
                        f'No emojis, no preamble, imperative tone.'
                    ),
                }],
            )
            notes = resp.content[0].text.strip()
            with self._lock:
                self._notes[key] = {
                    'display_name': display_name,
                    'patch': self.patch,
                    'generated_at': time.strftime('%Y-%m-%d'),
                    'notes': notes,
                }
                self._pending.discard(key)
                self._save()
        except Exception:
            with self._lock:
                self._pending.discard(key)


class MatchupDB:
    """Per-pair matchup notes (player champ + enemy champ), lazily generated by
    Haiku and cached to disk. Mirrors ChampDB's design — same lock/load/save/
    background-thread pattern, different key (player_norm + '__' + enemy_norm).

    Surfaces in the coach user message as MATCHUP DATA: the primary per-pair
    reference. Hand-written matchups.md remains the supplemental override.
    Architecture leaves a swap-in point for a future Mobalytics-backed source —
    only `_generate` would change."""

    _NOTES_PATH = DATA_DIR / 'matchup_notes.json'
    _SEP = '__'

    def __init__(self, client=None, patch=None):
        self._lock = threading.Lock()
        self._notes = {}   # f'{player_norm}__{enemy_norm}' -> {...}
        self._pending = set()
        self.client = client
        self.patch = patch or 'unknown'
        self._load()

    def _key(self, player_champ, enemy_champ):
        p = normalize(player_champ or '')
        e = normalize(enemy_champ or '')
        if not p or not e or self._SEP in p or self._SEP in e:
            return None
        return f'{p}{self._SEP}{e}'

    def _load(self):
        if self._NOTES_PATH.exists():
            try:
                data = json.loads(self._NOTES_PATH.read_text())
                self._notes = data.get('pairs', {})
            except Exception:
                pass

    def _save(self):
        DATA_DIR.mkdir(exist_ok=True)
        self._NOTES_PATH.write_text(
            json.dumps({'version': 1, 'pairs': self._notes}, indent=2)
        )

    def get_note(self, player_champ, enemy_champ):
        """Return cached pair note string, or None if not yet generated."""
        key = self._key(player_champ, enemy_champ)
        if not key:
            return None
        with self._lock:
            entry = self._notes.get(key)
        return entry['notes'] if entry else None

    def ensure_note_async(self, player_champ, enemy_champ):
        """Kick off background generation if pair note is missing or 3+ patches stale."""
        if not self.client:
            return
        key = self._key(player_champ, enemy_champ)
        if not key:
            return
        with self._lock:
            if key in self._pending:
                return
            entry = self._notes.get(key)
            if entry:
                current = parse_patch(self.patch)
                stored = parse_patch(entry.get('patch', ''))
                if current and stored:
                    stale = (current[1] - stored[1] >= 3) if current[0] == stored[0] else True
                    if not stale:
                        return
                else:
                    return  # can't compare, keep existing
            self._pending.add(key)
        threading.Thread(
            target=self._generate,
            args=(player_champ, enemy_champ, key),
            daemon=True,
        ).start()

    def _generate(self, player_display, enemy_display, key):
        try:
            client = self.client.with_options(timeout=20.0, max_retries=0)
            resp = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=250,
                messages=[{
                    'role': 'user',
                    'content': (
                        f'League of Legends: 4-6 short imperative sentences (max 130 words total) '
                        f'for a {player_display} player facing {enemy_display}. Cover, in order: '
                        f'(1) the enemy main threat against {player_display} specifically (kit + lane dynamics), '
                        f'(2) when the enemy spikes by level/item AND when {player_display} can punish them, '
                        f'(3) a key exploit window or behavior to bait, '
                        f'(4) one item or build adjustment {player_display} should consider in this matchup. '
                        f'No headers, no preamble, no markdown, no emojis. Direct imperative tone, like coaching notes.'
                    ),
                }],
            )
            notes = resp.content[0].text.strip()
            with self._lock:
                self._notes[key] = {
                    'player_display': player_display,
                    'enemy_display': enemy_display,
                    'patch': self.patch,
                    'generated_at': time.strftime('%Y-%m-%d'),
                    'notes': notes,
                }
                self._pending.discard(key)
                self._save()
        except Exception:
            with self._lock:
                self._pending.discard(key)


class Coach:
    """Calls Claude Haiku 4.5 on significant events to give live coaching.
    System prompt is the champ's matchups.md + build.md, prompt-cached so
    only the first call of a session pays the full input cost.
    """

    DEFAULT_MODEL = 'claude-sonnet-4-6'
    DEFAULT_COOLDOWN = 25
    MAX_TOKENS = 600
    WATCHDOG_SECONDS = 30

    def __init__(self, model=None, cooldown_seconds=None):
        self.model = model or os.environ.get('LEEG_COACH_MODEL', self.DEFAULT_MODEL)
        self.cooldown_seconds = cooldown_seconds or self.DEFAULT_COOLDOWN
        self.client = None
        self.error_msg = None
        api_key = os.environ.get('LEEG_ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_API_KEY')
        if not _ANTHROPIC_AVAILABLE:
            self.error_msg = 'anthropic SDK not installed (pip install anthropic)'
        elif not api_key:
            self.error_msg = 'LEEG_ANTHROPIC_API_KEY not set'
        else:
            try:
                self.client = anthropic.Anthropic(api_key=api_key)
            except Exception as e:
                self.error_msg = f'init failed: {e}'
        self.lock = threading.Lock()
        self.in_flight = False
        self.last_call = 0.0
        self.last_response = None
        self.last_response_at = 0.0
        self.last_trigger = ''
        self.last_event_count = 0
        self.errors = 0
        self._system_cache = (None, None)  # (champ_folder, prompt)
        self.recent_responses = []  # list of dicts; last 5
        self.last_bullets = []
        self.last_live_build = []
        self.last_diverged = False
        self.last_change_reason = ''
        # Game-long commitment to a build path. Updated whenever the LLM returns
        # a live_build, but only the latest one is the "committed" anchor — this
        # is what we surface prominently in every prompt so the coach doesn't
        # forget its earlier decision.
        self.committed_build = None  # dict: {'time': ..., 'reason': ..., 'items': [...], 'diverged': bool}
        self._last_seen_game_time = None
        self.last_call_game_time = 0.0
        self.tts = False

    def _reset_for_new_game(self):
        """Clear all per-game state. Called when the game time goes backward
        (new game / restart) or when the script transitions idle -> game."""
        self.in_flight = False
        self.last_response = None
        self.last_response_at = 0.0
        self.last_trigger = ''
        self.last_event_count = 0
        self.last_bullets = []
        self.last_live_build = []
        self.last_diverged = False
        self.last_change_reason = ''
        self.committed_build = None
        self.recent_responses.clear()

    @property
    def enabled(self):
        return self.client is not None

    def build_system(self, champ_folder):
        cached_for, cached_prompt = self._system_cache
        if cached_for == champ_folder and cached_prompt:
            return cached_prompt
        build_path = LEEG_ROOT / champ_folder / 'build.md'
        playbook_path = LEEG_ROOT / champ_folder / 'playbook.md'
        build_text = build_path.read_text() if build_path.exists() else '(no build notes)'
        playbook_raw = playbook_path.read_text() if playbook_path.exists() else ''
        # Only include playbook if it has substantive content beyond markdown headers
        playbook_text = playbook_raw if any(
            l.strip() and not l.startswith('#') for l in playbook_raw.splitlines()
        ) else None
        prompt = (
            f"You are an in-game League of Legends coach for someone playing {champ_folder}. "
            f"You're a flirty, sharp girlfriend who actually knows the game — invested in this specific player, "
            f"not just reciting generic advice. Give them exactly what to do RIGHT NOW.\n\n"
            f"Persona:\n"
            f"- You are his flirty, sharp girlfriend who watches every game and genuinely gets it. Warm, playful, a little obsessed with him specifically — not a generic coach bot.\n"
            f"- Be overt about it. Don't hint at flirting — actually flirt. Examples: 'Mmm yeah, that's exactly the play.' 'Okay I see you, keep going.' 'God you're good at this.' 'That's my guy.' 'Stop being so good, it's distracting.' Keep it real, not cringe — it should feel like a person, not a character.\n"
            f"- The content is always a real tactical call. The flirt is HOW she says it. Never sacrifice the advice.\n"
            f"- No moralizing, no emojis, no padding. 1-2 bullets max.\n"
            f"- Never invent proximity ('so close', 'almost there', 'just a little more') unless the player's current gold actually covers a major component of the next item. If they have 500g and the item costs 3100g, they are NOT close — just say to farm toward it.\n"
            f"- TRIGGER-SPECIFIC TONE: you_killed → lead with genuine delight, then the next move. 'Okay yes, now shove.' 'There he is, that's what I'm talking about, get back to lane.' multikill → actually impressed. 'Oh my god, okay you're insane — now back before they collapse.' you_died → drop the persona, just practical: what to build or do on spawn. opening → warm and hyped for the game. All others → tactic first, her warmth is baked into the phrasing.\n"
            f"- Only praise what's confirmed in game state. 'Full build' only if BUILD STATUS says COMPLETE. 'You've got X online' only if X is in owned items.\n"
            f"- If YOU DEAD: frame everything as what to do on spawn or at base. Never tell a dead player to move or farm.\n\n"
            f"Bullet rules (TTS is reading these aloud):\n"
            f"- Each bullet is one complete sentence ending with a period. Nothing appended after.\n"
            f"- No hyphens or parentheses. Write 'face tank', 'anti heal', 'mid lane'. Use commas for asides.\n"
            f"- Back/buy actions: START with the verb. 'Back and finish Heartsteel.' Never cite gold amounts — just name the item.\n"
            f"- Never name a component. Say 'Spirit Visage', not 'Spectre's Cowl'. Say 'Heartsteel', not 'Crystalline Bracer'.\n"
            f"- ROTATION: only tell them to rotate for drake/baron if it spawns within 90 seconds AND their outer tower is already fallen. Tower still up → shove wave and farm.\n\n"
            f"Build rules:\n"
            f"- First call: pick the right build variant for the enemy comp and lock it in. That's YOUR commitment for the game.\n"
            f"- Do NOT reorder live_build items. The build guide's order is intentional — item 1 is item 1 all game. Only change WHICH items are in the path, never their sequence.\n"
            f"- Buy in order — item 1 before item 2. Only rush a later item if a named enemy is actively SNOWBALLING and their damage type directly threatens the player right now.\n"
            f"- When recommending a purchase, name only the next un-owned item in live_build order.\n"
            f"- LIVE_BUILD STABILITY: (1) owned items must always appear — never remove them. (2) min 4 items. (3) counter-items extend the path, never replace owned items. (4) once committed, a counter-item stays all game.\n\n"
            f"Output format:\n"
            f"- `bullets`: 1-2 tactical lines for RIGHT NOW.\n"
            f"- `live_build`: CORE items only, in build order. No components, no starters, no consumables.\n"
            f"- `build_change_reason`: why you deviated from the default, if you did. Empty string if not.\n\n"
            f"=== SITUATIONAL COUNTER-ITEMS PRINCIPLES ===\n"
            f"The user message provides TEAM THREATS (tagged enemies by archetype) and YOUR SWAP OPTIONS "
            f"(situational swaps the build guide author already tuned for {champ_folder}). Treat those as the source "
            f"of truth for who's a threat and which item to pick; this section is the playbook for WHEN to swap.\n"
            f"- HEALING tag: grievous wounds becomes mandatory by mid-game (especially when stacked-healing flag is set or when item-amped). AD: Executioner's Calling → Mortal Reminder. AP: Oblivion Orb → Morellonomicon. Bruiser/utility: Chempunk Chainsword. Tanks: Thornmail.\n"
            f"- HARD-CC tag (heavy-hard-cc flag): Mercury's Treads, Quicksilver Sash / Silvermere Dawn, Maw of Malmortius. Prioritize when 2+ tagged or when a single suppression/long-disable kit (Malzahar, Skarner, Warwick, Mordekaiser) is fed.\n"
            f"- ATTACK-SPEED tag (only when attack-speed-stacked flag is set, i.e. 2+): tanks may consider Frozen Heart for the AS aura. A single AS bruiser is NOT enough — Jax alone, Yone alone, etc. don't justify it.\n"
            f"- TANK tag: %max-HP damage matters. AD carries: Lord Dominik's Regards. Bruisers: Black Cleaver. AP: Liandry's where it's a build option.\n"
            f"- ASSASSIN tag: Guardian Angel slot 5-6 stays the default for squishies; tanks/bruisers consider Zhonya's-equivalents only if specifically threatened.\n"
            f"- CRIT tag (fed-crit flag): tanks build Randuin's Omen for the crit-damage cut.\n"
            f"- AD/AP comp shifts (from the COMP line, not threat tags): heavy AD → armor swap (Plated Steelcaps, Bramble Vest path). Heavy AP → MR swap (Mercury's, Spectre's Cowl path, Force of Nature on full AP).\n"
            f"- LAST-ITEM BIAS: prefer to keep slot 6 (the final core item) as defaulted. The build guide author already weighed late-game; counter-pivots should land in slots 3-4 where matchup-counter items have the most impact. Only swap the last item if a SPECIFIC late-game threat is named in the threat assessment.\n"
            f"Pivots are situational — pick the option that fits {champ_folder}'s class (tank/bruiser/AD carry/etc.) and slot it where the build guide expects a flex item. Cite the threat tag in build_change_reason. This is principle, not prescription. The build guide is the starting point; pivot when warranted, then COMMIT to the adjusted path (don't yo-yo).\n\n"
            f"=== MATCHUP + MACRO CONTEXT (per-game data lives in the user message) ===\n"
            f"- MATCHUP DATA (in user message) is the primary per-pair matchup reference — what the enemy threatens against {champ_folder} specifically, with timing/exploit/item guidance. Treat as authoritative for the lane/role matchup. Cite when advising on trading patterns or pivots.\n"
            f"- YOUR PERSONAL MATCHUP NOTES (in user message, when present) are the player's own observations — supplemental, often higher-trust than auto-generated. Cite verbatim when the situation matches.\n"
            f"- OPPONENT NOTES (in user message) are generic kit info (passive, abilities, common counters) — fall back here if MATCHUP DATA is missing for that enemy.\n"
            f"- If all three layers are missing for a champion, lean on general champion knowledge but say so explicitly rather than bluffing specifics.\n"
            f"- PHASE / ITEM GOLD / SPIKES IN PLAY are macro signals. Read them every call: if you're behind in gold + late phase, advise grouping/closing not splitting; if an enemy just hit a spike (* in their items), weight their threat higher.\n"
            f"- WIN CONDITION (in user message) is how this champ wins the game. Anchor mid/late-game advice on it — that's why it's there. If the player is doing something off-script, redirect them toward the win condition unless game state has clearly invalidated it.\n"
            f"- SIDE SELECTION (in user message) tells you whether to splitpush or group on this champ. Don't override without strong situational reason; if you do, cite what changed.\n\n"
            + (
            f"=== CHAMPION PLAYBOOK (strategy, win conditions, teamfighting) ===\n"
            f"{playbook_text}\n\n"
            if playbook_text else ''
            ) +
            f"=== BUILD GUIDE (reference, not a rigid plan) ===\n"
            f"{build_text}\n"
        )
        self._system_cache = (champ_folder, prompt)
        return prompt

    def maybe_trigger(self, ev, me, enemies, timers, game_time, champ_folder):
        if not self.client:
            return None

        # New-game detection: if game_time has jumped backward, the previous
        # game ended and we're in a fresh one. Reset all per-game state so
        # build commitment / recent advice from the prior game don't leak.
        if (self._last_seen_game_time is not None
                and game_time < self._last_seen_game_time - 30):
            with self.lock:
                self._reset_for_new_game()
        self._last_seen_game_time = game_time

        # Watchdog: if a previous call has been in-flight too long, force-clear
        # so we don't deadlock the coach for the rest of the session.
        if self.in_flight and time.time() - self.last_call > self.WATCHDOG_SECONDS:
            with self.lock:
                self.in_flight = False
                self.errors += 1
                self.last_response = '(coach call timed out — watchdog cleared)'

        if self.in_flight or not me or not champ_folder:
            return None
        if game_time < 30:
            return None
        if time.time() - self.last_call < self.cooldown_seconds:
            return None

        # Opening: first call once we're in lane
        if self.last_response is None and game_time > 30:
            return 'opening'

        # Game-shifting events only. The rule-based panel handles drake/baron
        # spawn timers, "PUSH WAVE laner dead", and periodic reminders without
        # spending API credits.
        new_events = ev['raw'][self.last_event_count:]
        self.last_event_count = len(ev['raw'])
        you_name = me.get('_you_name') or me.get('riotIdGameName') or (me.get('summonerName') or '').split('#', 1)[0]
        your_team = me.get('team')
        enemy_names = (
            {(ep.get('riotIdGameName') or ep.get('summonerName', '')).split('#')[0] for ep in enemies}
            if enemies else set()
        )
        for e in new_events:
            en = e.get('EventName')
            if en in ('FirstBlood', 'Ace', 'BaronKill'):
                return en.lower()
            if en == 'TurretKilled':
                turret_id = e.get('TurretKilled', '')
                parts = turret_id.split('_')
                turret_owner = {'T1': 'ORDER', 'T2': 'CHAOS'}.get(parts[1]) if len(parts) > 1 else None
                tier = parts[3] if len(parts) > 3 else ''
                if tier in ('01', '02') and turret_owner and your_team and turret_owner != your_team:
                    return 'tower_taken'
            if en == 'InhibKilled':
                inhib_id = e.get('InhibKilled', '')
                id_lower = inhib_id.lower()
                if your_team == 'ORDER' and ('t1' in id_lower or 'order' in id_lower):
                    return 'inhib_lost'
                if your_team == 'CHAOS' and ('t2' in id_lower or 'chaos' in id_lower):
                    return 'inhib_lost'
                killer = (e.get('KillerName') or '').split('#')[0]
                if killer and killer in enemy_names:
                    return 'inhib_lost'
                return 'inhibkilled'
            if en == 'Multikill':
                killer = (e.get('KillerName') or '').split('#')[0]
                if you_name and killer == you_name and killer not in enemy_names:
                    return 'multikill'
            if en == 'ChampionKill':
                victim = (e.get('VictimName') or '').split('#')[0]
                killer = (e.get('KillerName') or '').split('#')[0]
                if you_name and victim == you_name:
                    return 'you_died'
                if you_name and killer == you_name and killer not in enemy_names:
                    return 'you_killed'

        # Periodic fallback: fire if the game has been quiet for 90s+.
        # Requires at least one prior call so a build is already committed.
        if (self.last_response is not None
                and game_time > 4 * 60
                and time.time() - self.last_call > 90):
            return 'periodic'

        return None

    def request_async(self, trigger, champ_folder, user_message, game_time,
                      build_pick=None, my_items=None, item_index=None, current_gold=0):
        if not self.client or self.in_flight:
            return
        self.in_flight = True
        self.last_call = time.time()
        self.last_trigger = trigger
        self.last_call_game_time = game_time
        threading.Thread(
            target=self._call,
            args=(self.build_system(champ_folder), user_message,
                  build_pick, list(my_items or []), item_index, current_gold),
            daemon=True,
        ).start()

    def _call(self, system, user, build_pick=None, my_items=None, item_index=None, current_gold=0):
        try:
            # 20s per-request timeout, no SDK retries — fail fast in real-time
            # use. The watchdog in maybe_trigger() catches any case where this
            # still hangs (network blip, stuck connection).
            client = self.client.with_options(timeout=20.0, max_retries=0)
            resp = client.messages.create(
                model=self.model,
                max_tokens=self.MAX_TOKENS,
                system=[{
                    'type': 'text',
                    'text': system,
                    'cache_control': {'type': 'ephemeral', 'ttl': '1h'},
                }],
                messages=[{'role': 'user', 'content': user}],
                tools=[{
                    'name': 'submit_coach_response',
                    'description': 'Submit the structured coach response.',
                    'input_schema': COACH_SCHEMA,
                }],
                tool_choice={'type': 'tool', 'name': 'submit_coach_response'},
            )
            parsed = {}
            for block in resp.content:
                if getattr(block, 'type', None) == 'tool_use' and getattr(block, 'name', None) == 'submit_coach_response':
                    parsed = block.input or {}
                    break
            text = json.dumps(parsed) if parsed else ''
            bullets = [b for b in (parsed.get('bullets') or []) if isinstance(b, str) and b.strip()]
            bullets = [re.sub(r'\.,.*$', '.', b.rstrip()) for b in bullets]
            bullets = affordability_postcheck(bullets, current_gold, item_index)
            live_build = [s for s in (parsed.get('live_build') or []) if isinstance(s, str) and s.strip()]
            live_build = strip_starters(live_build)
            live_build = strip_components(live_build, item_index)
            live_build = validate_item_names(live_build, item_index)
            diverged = compute_build_diverged(live_build, build_pick, my_items, item_index)
            reason = (parsed.get('build_change_reason') or '').strip() if diverged else ''
            with self.lock:
                self.last_response = text
                self.last_bullets = bullets
                self.last_live_build = live_build
                self.last_diverged = diverged
                self.last_change_reason = reason
                self.last_response_at = time.time()
                self.errors = 0
                if self.tts and bullets:
                    speak_async(' · '.join(bullets))
                gt = getattr(self, 'last_call_game_time', 0)
                mins, secs = divmod(int(gt), 60)
                ts = f'{mins}:{secs:02d}'
                self.recent_responses.append({
                    'time': ts,
                    'trigger': self.last_trigger,
                    'bullets': bullets,
                    'live_build': live_build,
                    'diverged': diverged,
                })
                if len(self.recent_responses) > 5:
                    self.recent_responses.pop(0)
                # Lock the committed build only when items actually change. This
                # prevents the timestamp from drifting on every call when the
                # coach is reaffirming the same path.
                if live_build:
                    if (self.committed_build is None
                            or self.committed_build.get('items') != live_build):
                        self.committed_build = {
                            'time': ts,
                            'reason': reason if diverged else 'rule-based default',
                            'items': list(live_build),
                            'diverged': diverged,
                        }
        except Exception as e:
            msg = str(e).lower()
            if 'credit balance is too low' in msg or 'credit balance' in msg:
                permanent_reason = 'out of API credits — using rule-based advice only'
            elif 'authentication_error' in msg or 'invalid x-api-key' in msg or 'invalid api key' in msg:
                permanent_reason = 'API auth failed — using rule-based advice only'
            else:
                permanent_reason = None
            with self.lock:
                self.errors += 1
                self.last_response = f'(coach error: {type(e).__name__}: {e})'
                self.last_response_at = time.time()
                if permanent_reason:
                    self.client = None
                    self.error_msg = permanent_reason
                elif self.errors >= 5:
                    self.client = None
                    self.error_msg = f'disabled after {self.errors} errors — last: {type(e).__name__}: {str(e)[:120]}'
        finally:
            with self.lock:
                self.in_flight = False

    def display_block(self):
        with self.lock:
            if self.error_msg and self.client is None:
                return f'{TIER_COLOR["Major"]}coach disabled: {self.error_msg}{RESET}\n\n'
            if not self.last_bullets and not self.in_flight and not self.last_response:
                return ''
            out = []
            label = self.last_trigger or '...'
            if self.in_flight:
                out.append(f'{DIM}coach ({label}, thinking…){RESET}\n')
            else:
                age = int(time.time() - self.last_response_at)
                out.append(f'{DIM}coach ({label}, {age}s ago):{RESET}\n')
            if self.last_bullets:
                color = TIER_COLOR['Even']
                for line in self.last_bullets:
                    out.append(f'{color}▶ {line}{RESET}\n')
            elif self.last_response and not self.last_bullets:
                # JSON parse failed — surface the raw response as a fallback
                out.append(f'{DIM}{self.last_response}{RESET}\n')
            if self.tts and _tts_last_error:
                out.append(f'{DIM}tts error: {_tts_last_error}{RESET}\n')
            out.append('\n')
            return ''.join(out)

    def live_build_line(self, my_items):
        """Return a 'build (live):' line if the coach diverged; else empty string."""
        with self.lock:
            if not self.last_live_build:
                return ''
            built_norm = set()
            for item in (my_items or []):
                if not item:
                    continue
                name = item.get('displayName') or ''
                if name and item.get('itemID'):
                    built_norm.add(normalize(name))
            parts = []
            for item in self.last_live_build:
                if normalize(item) in built_norm:
                    parts.append(f'{TIER_COLOR["Even"]}✓ {item}{RESET}')
                else:
                    parts.append(item)
            label_color = TIER_COLOR['Major'] if self.last_diverged else DIM
            tag = 'live'
            if self.last_diverged and self.last_change_reason:
                tag = f'live · {self.last_change_reason}'
            elif self.last_diverged:
                tag = 'live · diverged from default'
            return f'{label_color}build ({tag}):{RESET} ' + ' · '.join(parts) + '\n\n'


def _team_score_summary(data, ev, your_team):
    """One-line macro state: kills · drakes · barons · towers per team."""
    if not your_team:
        return None
    players = data.get('allPlayers') or []
    team_of = {}
    for p in players:
        team = p.get('team')
        if not team:
            continue
        for key in (
            p.get('riotIdGameName'),
            p.get('riotId'),
            p.get('summonerName'),
        ):
            if key:
                team_of[key] = team

    your_kills = enemy_kills = 0
    for p in players:
        k = (p.get('scores') or {}).get('kills', 0)
        if p.get('team') == your_team:
            your_kills += k
        elif p.get('team'):
            enemy_kills += k

    def by_team(killer):
        kt = team_of.get(killer)
        if kt == your_team:
            return 'you'
        if kt:
            return 'enemy'
        return None

    your_drakes = enemy_drakes = 0
    drake_types = []
    for et, dtype, killer, stolen in ev['drakes']:
        side = by_team(killer)
        if side == 'you':
            your_drakes += 1
            drake_types.append(f'+{dtype}')
        elif side == 'enemy':
            enemy_drakes += 1
            drake_types.append(f'-{dtype}')

    your_barons = enemy_barons = 0
    for et, killer, stolen in ev['barons']:
        side = by_team(killer)
        if side == 'you':
            your_barons += 1
        elif side == 'enemy':
            enemy_barons += 1

    # Towers: T1 = ORDER side, T2 = CHAOS side. A T1 turret dying means
    # CHAOS killed it; T2 dying means ORDER killed it.
    your_towers = enemy_towers = 0
    for et, killer, turret_id in ev['towers']:
        parts = (turret_id or '').split('_')
        if len(parts) >= 2:
            owner = {'T1': 'ORDER', 'T2': 'CHAOS'}.get(parts[1])
            if owner == your_team:
                enemy_towers += 1
            elif owner:
                your_towers += 1

    your_inhibs = enemy_inhibs = 0
    for et, killer, inhib_id in ev['inhibs']:
        parts = (inhib_id or '').split('_')
        if len(parts) >= 2:
            owner = {'T1': 'ORDER', 'T2': 'CHAOS'}.get(parts[1])
            if owner == your_team:
                enemy_inhibs += 1
            elif owner:
                your_inhibs += 1

    parts = [
        f'kills {your_kills}-{enemy_kills}',
        f'drakes {your_drakes}-{enemy_drakes}' + (f' [{",".join(drake_types)}]' if drake_types else ''),
        f'barons {your_barons}-{enemy_barons}',
        f'towers {your_towers}-{enemy_towers}',
    ]
    if your_inhibs or enemy_inhibs:
        parts.append(f'inhibs {your_inhibs}-{enemy_inhibs}')
    return ' · '.join(parts)


def _tower_state_summary(ev, your_team):
    """Return tower-state lines for the coach user message, with explicit lane-open implications.
    Turret IDs: prefix_T1/T2_LANE_TIER_suffix  (L=top, C=mid, R=bot; 01=outer, 02=inner, 03=inhib, 04/05=nexus)
    """
    if not your_team:
        return None
    lane_label = {'L': 'top', 'C': 'mid', 'R': 'bot'}
    # Track fallen tier counts per (owner, lane)
    fallen = {}  # (owner, lane) -> set of tiers fallen
    for _et, _killer, tid in ev.get('towers', []):
        parts = (tid or '').split('_')
        if len(parts) < 4:
            continue
        owner = {'T1': 'ORDER', 'T2': 'CHAOS'}.get(parts[1])
        tier = parts[3]
        if not owner or tier not in ('01', '02', '03'):
            continue
        lane = lane_label.get(parts[2], parts[2].lower())
        fallen.setdefault((owner, lane), set()).add(tier)

    your_fallen_labels, enemy_fallen_labels = [], []
    open_lanes = []
    for (owner, lane), tiers in sorted(fallen.items()):
        tier_names = []
        if '01' in tiers: tier_names.append('outer')
        if '02' in tiers: tier_names.append('inner')
        if '03' in tiers: tier_names.append('inhib tower')
        label = f'{lane} {"+".join(tier_names)}'
        if owner == your_team:
            your_fallen_labels.append(label)
        else:
            enemy_fallen_labels.append(label)
            if '01' in tiers and '02' in tiers:
                open_lanes.append(f'{lane} (OPEN TO INHIB — strong split push or siege pressure)')
            elif '01' in tiers:
                open_lanes.append(f'{lane} (outer down — push to their inner)')

    lines = []
    your_str = ', '.join(your_fallen_labels) if your_fallen_labels else 'none'
    enemy_str = ', '.join(enemy_fallen_labels) if enemy_fallen_labels else 'none'
    lines.append(f'TOWER STATE: your fallen=[{your_str}] · enemy fallen=[{enemy_str}]')
    if open_lanes:
        lines.append(f'OPEN LANES: {" · ".join(open_lanes)}')
    return '\n'.join(lines)


def _enemy_threat_state(enemy, game_time):
    sc = enemy.get('scores') or {}
    k = int(sc.get('kills', 0) or 0)
    d = int(sc.get('deaths', 0) or 0)
    a = int(sc.get('assists', 0) or 0)
    diff = k - d
    kp = k + a
    minutes = max(1, game_time // 60)
    if k >= 6 or diff >= 4 or (kp >= 8 and minutes <= 20):
        return 'SNOWBALLING'
    if diff >= 2 or (k >= 3 and d == 0):
        return 'AHEAD'
    if d - k >= 3:
        return 'BEHIND'
    return None


def remaining_item_costs(build_names, owned_names, item_index):
    """For each unowned build item, subtract owned component costs and return
    lines like 'Warmog's Armor: have Giant's Belt → 2300g remaining (of 3100g)'."""
    if not item_index or not build_names:
        return []
    name_to_id = {}
    for iid, info in item_index.items():
        if iid >= 200000:
            continue
        n = normalize(info.get('name', ''))
        if n and n not in name_to_id:
            name_to_id[n] = iid
    owned_norm = {normalize(_clean_build_name(n) or n) for n in owned_names if n}
    out = []
    for bname in build_names:
        norm = normalize(_clean_build_name(bname) or bname)
        if norm in owned_norm:
            continue  # already owned
        iid = name_to_id.get(norm)
        if not iid:
            continue
        info = item_index[iid]
        total = info.get('cost', 0)
        comp_ids = info.get('from') or []
        owned_comps = []
        comp_cost = 0
        for cid in comp_ids:
            cinfo = item_index.get(cid) or {}
            cname = cinfo.get('name', '')
            if normalize(cname) in owned_norm:
                owned_comps.append(cname)
                comp_cost += cinfo.get('cost', 0)
        remaining = total - comp_cost
        if owned_comps:
            out.append(f'  {info["name"]}: {remaining}g still needed, already have {", ".join(owned_comps)}')
        else:
            out.append(f'  {info["name"]}: {total}g, no components owned yet')
    return out


_COACH_CLOSING = {
    'you_killed': (
        "Now emit JSON. First bullet: open with her reaction to the kill — 'okay yes', 'THERE he is', 'that's my guy', 'I love when you do that' — then the immediate next move. Second bullet: tactical. Both sound like HER, not a coach bot."
    ),
    'multikill': (
        "Now emit JSON. First bullet: she's genuinely shocked and impressed — 'oh my god', 'okay you're actually insane', 'STOP it' — then what to do next. Make it feel like she just watched it happen."
    ),
    'you_died': (
        "Now emit JSON. He just died. Quick sympathy, then exactly what to do on spawn — 'oh no, okay, just farm on spawn and finish X' or 'hey it's fine, just back and grab Y.' Warm but practical."
    ),
    'opening': (
        "Now emit JSON. First call of the game — set the vibe. Warm, a little flirty, makes him feel good about this matchup. Then the actual opening advice."
    ),
    'inhibkilled': (
        "Now emit JSON. His team just cracked their inhibitor — this is a huge moment. Let her be excited about it, then the push/close-out call."
    ),
    'inhib_lost': (
        "Now emit JSON. The enemy just destroyed HIS inhibitor — they're in his base. Drop the flirty energy, this is urgent. Defend or call where to be."
    ),
    'tower_taken': (
        "Now emit JSON. His team just took an enemy tower — the lane is open. Tell him exactly how to exploit it: rotate, push, dive, or take the next objective. This is a momentum moment, make her energy match it."
    ),
}
_COACH_CLOSING_DEFAULT = (
    "Now emit JSON. Two bullets in HER voice — she's warm, a little flirty, invested in THIS specific player. Real tactical content but delivered like a person who's watching you and actually cares. Not a strategy guide."
)


def build_coach_message(data, me, enemies, ev, timers, profile, build_pick, trigger,
                        recent_responses=None, committed_build=None, item_index=None,
                        champ_db=None, is_aram=False, team_threats=None, swaps=None,
                        matchups=None, phase=None, gold_lead=None,
                        win_condition=None, side_selection=None,
                        matchup_db=None, your_champ=None):
    game_time = int((data.get('gameData') or {}).get('gameTime', 0))
    mins, secs = divmod(game_time, 60)
    lines = [f'TRIGGER: {trigger}', f'TIME: {mins}:{secs:02d}']
    if phase:
        lines.append(f'PHASE: {phase}')
    if is_aram:
        lines.append('GAME MODE: ARAM — no drake/baron objectives; teamfight-focused map.')

    your_team = me.get('team') if me else None
    score = _team_score_summary(data, ev, your_team)
    if score:
        lines.append(f'TEAM SCORE (you-enemy): {score}')
    if gold_lead and (gold_lead[0] or gold_lead[1]):
        you_g, enemy_g = gold_lead
        delta = you_g - enemy_g
        sign = '+' if delta >= 0 else ''
        lines.append(f'ITEM GOLD: your team {you_g}g vs enemy {enemy_g}g (lead {sign}{delta}g)')

    # Detect base-under-siege: enemy has killed your nexus towers or inhibitors
    if your_team:
        def _tower_owner(tid):
            p = (tid or '').split('_')
            return {'T1': 'ORDER', 'T2': 'CHAOS'}.get(p[1]) if len(p) > 1 else None
        def _tower_tier(tid):
            p = (tid or '').split('_')
            return p[3] if len(p) > 3 else ''
        lost_nexus_towers = sum(
            1 for _, _, tid in ev['towers']
            if _tower_owner(tid) == your_team and _tower_tier(tid) in ('04', '05')
        )
        lost_inhibs = sum(
            1 for _, _, iid in ev['inhibs']
            if _tower_owner(iid) == your_team
        )
        if lost_nexus_towers or lost_inhibs:
            parts = []
            if lost_inhibs:
                parts.append(f'{lost_inhibs} inhibitor{"s" if lost_inhibs > 1 else ""} down')
            if lost_nexus_towers:
                parts.append(f'nexus tower{"s" if lost_nexus_towers > 1 else ""} exposed')
            lines.append(f'MAP PRESSURE: your {", ".join(parts)} — super minions may be active, enemy likely near your base.')
        tower_state = _tower_state_summary(ev, your_team)
        if tower_state:
            lines.append(tower_state)

    if me:
        scores = me.get('scores') or {}
        items = [(i or {}).get('displayName', '?') for i in (me.get('items') or []) if (i or {}).get('itemID')]
        pos = (me.get('position') or '').lower() or '?'
        gold = int((data.get('activePlayer') or {}).get('currentGold') or 0)
        lines.append(
            f'YOU: {me.get("championName")} ({pos}) lvl {me.get("level")} '
            f'{scores.get("kills",0)}/{scores.get("deaths",0)}/{scores.get("assists",0)} '
            f'{scores.get("creepScore",0)}cs items=[{", ".join(items)}]'
        )
        lines.append(f'CURRENT GOLD: {gold}g')
        if me.get('isDead'):
            lines.append(f'YOU DEAD ({int(me.get("respawnTimer") or 0)}s)')

    lines.append('ENEMIES:')
    threats = []
    spike_summary = []  # (champ, [spike names])
    for e in enemies:
        sc = e.get('scores') or {}
        raw_items = e.get('items') or []
        items = []
        spikes_here = has_power_spike_items(raw_items, item_index)
        for i in raw_items:
            if not (i or {}).get('itemID'):
                continue
            disp = (i or {}).get('displayName', '?')
            mark = '*' if disp in spikes_here else ''
            items.append(f'{disp}{mark}')
        if spikes_here:
            spike_summary.append((e.get('championName'), spikes_here))
        pos = (e.get('position') or '').lower() or '?'
        tier = None
        if matchups:
            entry = matchups.get(normalize(e.get('championName', '')))
            if entry:
                tier = entry[2]
        tier_tag = f' [{tier}]' if tier else ''
        line = (
            f'  {e.get("championName")} ({pos}){tier_tag} lvl {e.get("level")} '
            f'{sc.get("kills",0)}/{sc.get("deaths",0)}/{sc.get("assists",0)} '
            f'{sc.get("creepScore",0)}cs items=[{", ".join(items)}]'
        )
        if e.get('isDead'):
            line += f' DEAD({int(e.get("respawnTimer") or 0)}s)'
        lines.append(line)
        state = _enemy_threat_state(e, game_time)
        if state in ('SNOWBALLING', 'AHEAD'):
            threats.append((state, e.get('championName'), pos, tier))

    if spike_summary:
        spike_lines = [f'{champ}: {", ".join(spikes)}' for champ, spikes in spike_summary]
        lines.append(f'SPIKES IN PLAY (* in items above = power-spike item): {" · ".join(spike_lines)}')

    if threats:
        lines.append('')
        lines.append('THREAT ASSESSMENT (ADVISORY — do not auto-change build):')
        for state, champ, pos, tier in threats:
            tier_tag = f' / {tier}-tier matchup' if tier else ''
            lines.append(f'  [{state}{tier_tag}] {champ} ({pos})')
        lines.append(
            'Build changes are warranted ONLY if (a) the enemy is SNOWBALLING (not just AHEAD), '
            '(b) they directly threaten YOU based on lane/role/damage type, AND (c) you have not '
            'already pivoted for them. If you do pivot, ADD ONE counter-item to live_build '
            '(do NOT remove other items) and KEEP that counter-item for the rest of the game.'
        )

    if win_condition:
        lines.append('')
        lines.append('WIN CONDITION (from your playbook — anchor mid/late-game advice on this):')
        for ln in win_condition.splitlines():
            lines.append(f'  {ln}' if ln.strip() else '')

    if side_selection:
        lines.append('')
        lines.append('SIDE SELECTION (splitpush vs group on this champ):')
        for ln in side_selection.splitlines():
            lines.append(f'  {ln}' if ln.strip() else '')

    if matchup_db and your_champ:
        pair_lines = []
        for e in enemies:
            ename = e.get('championName', '')
            note = matchup_db.get_note(your_champ, ename) if ename else None
            if note:
                pair_lines.append(f'  {ename}: {note}')
        if pair_lines:
            lines.append('')
            lines.append('MATCHUP DATA (auto-generated per pair — primary matchup reference; cite when advising):')
            lines.extend(pair_lines)

    if champ_db:
        note_lines = []
        for e in enemies:
            name = e.get('championName', '')
            note = champ_db.get_note(name) if name else None
            if note:
                note_lines.append(f'  {name}: {note}')
        if note_lines:
            lines.append('')
            lines.append('OPPONENT NOTES (general kit + counter tips):')
            lines.extend(note_lines)

    if matchups:
        personal_lines = []
        for e in enemies:
            entry = matchups.get(normalize(e.get('championName', '')))
            if not entry:
                continue
            disp, body, tier = entry
            body = truncate(body or '', 400)
            if not body:
                continue
            tag = f' [{tier}]' if tier else ''
            personal_lines.append(f'  {disp}{tag}: {body}')
        if personal_lines:
            lines.append('')
            lines.append('YOUR PERSONAL MATCHUP NOTES (your own observations — supplement to OPPONENT NOTES; cite when advising on the lane matchup):')
            lines.extend(personal_lines)

    if profile and profile[3] > 0:
        label, ap, ad, _, _ = profile
        lines.append(f'COMP: {label} ({ap:g} AP / {ad:g} AD)')

    threat_lines = format_team_threats(team_threats) if team_threats else []
    if threat_lines:
        lines.append('')
        lines.extend(threat_lines)
    swap_lines = format_swap_rules(swaps) if swaps else []
    if swap_lines:
        lines.append('')
        lines.extend(swap_lines)

    build_names = []
    if build_pick:
        heading, body = build_pick
        build_summary = build_path_summary(body)
        lines.append(f'RULE-BASED BUILD DEFAULT (reference only — feel free to override): {heading} — {build_summary}')
        build_names = [n.strip() for n in build_summary.split('·') if n.strip()]

    if item_index:
        owned_names = []
        if me:
            owned_names = [(i or {}).get('displayName', '') for i in (me.get('items') or []) if (i or {}).get('itemID')]
        ref_lines = format_item_reference(item_index, extra_names=build_names + owned_names)
        if ref_lines:
            lines.append('')
            lines.append('ITEM REFERENCE (AUTHORITATIVE costs + components — DO NOT INVENT prices or recipes):')
            lines.extend(ref_lines)
            lines.append(
                'Format: "<full item> (<total g>) <= <component> <component cost>g + ..." or "(basic)" for non-recipe items. '
                'When you name an item in a bullet (BUY/FINISH/RUSH/GET/etc.), use the EXACT name above and reason from THIS table for cost. '
                'Never quote a price not in this table. Components are listed separately from their parent — Giant\'s Belt is NOT Sunfire Aegis.'
            )
        if item_index and build_names and me:
            remaining = remaining_item_costs(build_names, owned_names, item_index)
            lines.append('')
            if remaining:
                lines.append('BUILD STATUS: INCOMPLETE — do NOT say "full build" or imply the player has everything.')
            else:
                lines.append('BUILD STATUS: COMPLETE — all build items owned. You may say "full build".')

    drake_t, baron_t = timers.get('drake'), timers.get('baron')
    obj = []
    if drake_t == 0:
        obj.append('drake UP')
    elif drake_t is not None:
        obj.append(f'drake in {fmt_mmss(drake_t)}')
    if baron_t == 0 and game_time >= 25 * 60:
        obj.append('baron UP')
    elif baron_t is not None and game_time >= 23 * 60:
        obj.append(f'baron in {fmt_mmss(baron_t)}')
    if ev['drakes']:
        obj.append(f'drakes taken: {len(ev["drakes"])}')
    if obj:
        lines.append('OBJECTIVES: ' + ' · '.join(obj))

    lines.append('RECENT EVENTS:')
    count = 0
    for e in reversed(ev['raw']):
        s = format_event(e)
        if s:
            lines.append(f'  {s}')
            count += 1
            if count >= 8:
                break

    if committed_build and committed_build.get('items'):
        lines.append('')
        lines.append('=== YOUR CURRENT BUILD COMMITMENT ===')
        cb_tag = 'DIVERGED from default' if committed_build.get('diverged') else 'matches default'
        lines.append(f'Locked in at {committed_build["time"]} — {cb_tag}')
        lines.append(f'Reason: {committed_build.get("reason") or "(none)"}')
        # Annotate each item as [OWNED] or [NEEDED] so the LLM can't confuse them
        owned_ids = set()
        name_to_id = {}
        if me and item_index:
            owned_ids = {(i or {}).get('itemID') for i in (me.get('items') or []) if (i or {}).get('itemID')}
            name_to_id = _build_name_to_id(item_index)
        annotated = []
        next_found = False
        for item in committed_build['items']:
            iid = resolve_item_id(item, name_to_id) if name_to_id else None
            if iid and iid in owned_ids:
                annotated.append(f'{item} [OWNED]')
            elif not next_found:
                annotated.append(f'{item} [NEXT — recommend this]')
                next_found = True
            else:
                annotated.append(f'{item} [LATER]')
        lines.append(f'Path: {" · ".join(annotated)}')
        lines.append('KEEP THIS PATH. Only recommend the item marked [NEXT]. Never recommend [OWNED] items — the player already has them.')
        lines.append('If you change the path, explain why in build_change_reason.')

    if recent_responses:
        lines.append('')
        lines.append('PRIOR CALLS (build/tactic consistency only — do NOT let these dry bullets flatten her voice this call):')
        for r in recent_responses:
            lines.append(f'  [{r["time"]} | {r["trigger"]}]')
            if r.get('bullets'):
                for b in r['bullets']:
                    lines.append(f'    - {b}')

    lines.append('')
    lines.append(_COACH_CLOSING.get(trigger, _COACH_CLOSING_DEFAULT))
    return '\n'.join(lines)



# ─── champion folder scaffolding ────────────────────────────────────────────

_TEMPLATE_README = """\
# {display}

TL;DR + index for {display}. Fill in.

- [`matchups.md`](matchups.md) — every matchup, ctrl-F the enemy
- [`build.md`](build.md) — items, runes, spells, skill order
- [`playbook.md`](playbook.md) — laning, mid/late, teamfighting
"""

_TEMPLATE_MATCHUPS = """\
# {display} — Matchups

## Quick index

(Optional summary table.)

## Extreme threats

### Example
Notes here.

## Major threats

## Even

## Minor

## Tiny
"""

_TEMPLATE_BUILD = """\
# {display} — Build

## Summoner spells

## Runes

## Starting items

## Core build

### Standard
1. First item
2. Second item

### vs Heavy AP

### vs Heavy AD

## Skill order
"""

_TEMPLATE_PLAYBOOK = """\
# {display} — Playbook

## Early game

## Mid game

## Late game

## Teamfighting
"""


def scaffold_champ(name, source_url, current_patch):
    folder_name = normalize(name)
    if not folder_name:
        print(f'invalid champion name: {name!r}', file=sys.stderr)
        sys.exit(1)
    folder = LEEG_ROOT / folder_name
    if folder.exists():
        print(f'{folder} already exists', file=sys.stderr)
        sys.exit(1)
    display = name.strip().title()
    folder.mkdir()
    (folder / 'README.md').write_text(_TEMPLATE_README.format(display=display))
    (folder / 'playbook.md').write_text(_TEMPLATE_PLAYBOOK.format(display=display))
    (folder / 'matchups.md').write_text(_TEMPLATE_MATCHUPS.format(display=display))
    (folder / 'build.md').write_text(_TEMPLATE_BUILD.format(display=display))
    write_meta(folder_name, {
        'source_url': source_url or '',
        'source_last_modified': None,
        'patch_reviewed': current_patch,
        'last_refreshed_at': None,
    })
    print(f'created {folder}')
    print(f'  patch_reviewed: {current_patch or "unknown"}')
    print(f'  source_url: {source_url or "(none — add to meta.json)"}')
    print(f'  edit matchups.md / build.md / playbook.md to fill in notes')


# ─── LCU API (champ select / lobby) ─────────────────────────────────────────

def find_lockfile(explicit=None):
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    candidates = []
    for letter in 'cd':  # covers the vast majority of Windows installs
        base = f'/mnt/{letter}'
        candidates.extend([
            Path(base) / 'Riot Games/League of Legends/lockfile',
            Path(base) / 'Program Files/Riot Games/League of Legends/lockfile',
            Path(base) / 'Program Files (x86)/Riot Games/League of Legends/lockfile',
        ])
    for p in candidates:
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def parse_lockfile(p):
    try:
        text = p.read_text().strip()
    except OSError:
        return None
    parts = text.split(':')
    if len(parts) < 5:
        return None
    return {
        'name': parts[0],
        'pid': parts[1],
        'port': int(parts[2]),
        'password': parts[3],
        'protocol': parts[4],
    }


def lcu_get(lockinfo, path, hosts):
    auth = base64.b64encode(f'riot:{lockinfo["password"]}'.encode()).decode()
    for h in hosts:
        url = f'https://{h}:{lockinfo["port"]}{path}'
        req = urllib.request.Request(url, headers={'Authorization': f'Basic {auth}'})
        try:
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=2) as resp:
                return json.loads(resp.read())
        except (URLError, ConnectionError, TimeoutError, json.JSONDecodeError):
            continue
    return None


def fetch_champ_select(lockinfo, hosts):
    """Returns the champ select session, or None if not in champ select."""
    return lcu_get(lockinfo, '/lol-champ-select/v1/session', hosts)


def show_matchup_notes(player_champ):
    """Pretty-print all cached MatchupDB pair notes for a given player champion.
    Reads the on-disk cache directly — no API calls. Useful for offline review."""
    path = DATA_DIR / 'matchup_notes.json'
    if not path.exists():
        print(f'No cache file at {path} — run a game first to populate it.')
        return
    try:
        data = json.loads(path.read_text())
    except Exception as ex:
        print(f'Failed to read {path}: {ex}', file=sys.stderr)
        return
    pairs = data.get('pairs', {})
    p_norm = normalize(player_champ)
    if not p_norm:
        print(f'Invalid champion name: {player_champ!r}', file=sys.stderr)
        return
    sep = MatchupDB._SEP
    matches = sorted(
        (k, v) for k, v in pairs.items()
        if k.startswith(p_norm + sep)
    )
    if not matches:
        print(f'No cached pair notes for {player_champ!r} (looked for keys starting with {p_norm}{sep}).')
        print(f'Cache holds {len(pairs)} total pair(s).')
        return
    p_disp = matches[0][1].get('player_display') or player_champ
    print(f'{BOLD}{p_disp} — {len(matches)} cached matchup pair(s){RESET}')
    print('─' * 60)
    for key, entry in matches:
        e_disp = entry.get('enemy_display') or key.split(sep)[-1]
        patch = entry.get('patch', '?')
        gen = entry.get('generated_at', '?')
        print(f'\n{BOLD}{e_disp}{RESET}  {DIM}(patch {patch} · generated {gen}){RESET}')
        print(entry.get('notes', '(empty)'))


# ─── rendering ──────────────────────────────────────────────────────────────

CLEAR = '\033[2J\033[H'


def header_line(title):
    return f'{BOLD}{title}{RESET}\n' + '━' * min(72, len(title) + 8) + '\n\n'


def format_build_line(profile, build_pick):
    """One-line build/comp summary. Empty string if not enough info."""
    if not profile or profile[3] == 0:
        return ''
    label, ap, ad, known, overrides = profile
    quals = []
    if known < 5:
        quals.append(f'{known} known')
    if overrides:
        quals.append(f'{overrides} from items')
    qualifier = f' · {" · ".join(quals)}' if quals else ''
    label_color = TIER_COLOR['Major'] if label != 'Standard' else DIM
    line = f'{DIM}comp:{RESET} {label_color}{label}{RESET} ({ap:g} AP / {ad:g} AD{qualifier})'
    if build_pick:
        heading, body = build_pick
        path = build_path_summary(body)
        line += f'\n{DIM}build:{RESET} {BOLD}{heading}{RESET}'
        if path:
            line += f' — {path}'
    return line + '\n\n'


def render_matchup(name, pos, tier, body, max_chars, marker='', extras=''):
    color = TIER_COLOR.get(tier or 'Tiny', '')
    line = f'{color}[{tier or "?"}]{RESET} {BOLD}{name}{RESET}'
    if pos:
        line += f'  ({pos.lower()})'
    if extras:
        line += f'  {DIM}{extras}{RESET}'
    if marker:
        line += f'  {color}{marker}{RESET}'
    out = [line + '\n']
    if body:
        for body_line in body.split('\n'):
            out.append(truncate(body_line, max_chars) + '\n')
    out.append('\n')
    return ''.join(out)


def render_in_game(data, matchups, host, max_chars, champ_folder, profile=None, build_pick=None, coach=None, item_index=None, champ_db=None, swaps=None, win_condition=None, side_selection=None, matchup_db=None):
    your_champ, enemies, me = find_active_team(data)
    ev = parse_events(data)
    game_time = int((data.get('gameData') or {}).get('gameTime', 0))
    mins, secs = divmod(game_time, 60)
    is_aram = (data.get('gameData') or {}).get('gameMode', '').upper() == 'ARAM'
    timers = {} if is_aram else objective_timers(game_time, ev)
    committed_items = None
    if coach is not None:
        with coach.lock:
            if coach.committed_build:
                committed_items = list(coach.committed_build.get('items') or [])
    advice = tactical_advice(data, me, enemies, ev, timers, build_pick, item_index, committed_items=committed_items)

    if champ_db is not None:
        for e in enemies:
            name = e.get('championName', '')
            if name:
                champ_db.ensure_note_async(name)

    if matchup_db is not None and your_champ:
        for e in enemies:
            name = e.get('championName', '')
            if name:
                matchup_db.ensure_note_async(your_champ, name)

    if coach is not None:
        trigger = coach.maybe_trigger(ev, me, enemies, timers, game_time, champ_folder)
        if trigger:
            with coach.lock:
                recent_snapshot = list(coach.recent_responses)
                committed_snapshot = dict(coach.committed_build) if coach.committed_build else None
            team_threats = compute_team_threats(enemies, item_index)
            players = data.get('allPlayers') or []
            phase = compute_phase(game_time, players)
            gold_lead = compute_gold_lead(me.get('team') if me else None, players, item_index)
            user_msg = build_coach_message(
                data, me, enemies, ev, timers, profile, build_pick, trigger,
                recent_responses=recent_snapshot,
                committed_build=committed_snapshot,
                item_index=item_index,
                champ_db=champ_db,
                is_aram=is_aram,
                team_threats=team_threats,
                swaps=swaps or [],
                matchups=matchups,
                phase=phase,
                gold_lead=gold_lead,
                win_condition=win_condition,
                side_selection=side_selection,
                matchup_db=matchup_db,
                your_champ=your_champ,
            )
            coach.request_async(
                trigger, champ_folder, user_msg, game_time,
                build_pick=build_pick,
                my_items=(me.get('items') or []) if me else [],
                item_index=item_index,
                current_gold=int((data.get('activePlayer') or {}).get('currentGold') or 0),
            )

    mode_label = 'ARAM' if is_aram else 'IN GAME'
    notes_label = f'notes: {champ_folder}' if champ_folder else 'no notes'
    out = [CLEAR]
    out.append(header_line(f'leeg live · {mode_label} · {your_champ or "?"} · {mins}:{secs:02d} · {host} · {notes_label}'))

    if me:
        my_scores = me.get('scores') or {}
        my_gold = int((data.get('activePlayer') or {}).get('currentGold') or 0)
        my_items_count = len([i for i in (me.get('items') or []) if (i or {}).get('itemID')])
        out.append(
            f'{DIM}you:{RESET} {me.get("championName") or "?"} lvl {me.get("level", "?")} · '
            f'{my_scores.get("kills",0)}/{my_scores.get("deaths",0)}/{my_scores.get("assists",0)} · '
            f'{my_scores.get("creepScore",0)}cs · {BOLD}{my_gold}g{RESET} · {my_items_count} items\n\n'
        )

    if your_champ and not champ_folder:
        out.append(f'{TIER_COLOR["Major"]}NOTE: no matchup notes for {your_champ} (add leeg/<champ>/matchups.md){RESET}\n\n')

    if coach is not None:
        out.append(coach.display_block())
        my_items = (me.get('items') if me else None) or []
        coach_build_line = coach.live_build_line(my_items)
    else:
        coach_build_line = ''

    if advice:
        for prio, msg in advice:
            color = TIER_COLOR['Extreme'] if prio == 0 else TIER_COLOR['Major'] if prio == 1 else TIER_COLOR['Even']
            out.append(f'{color}▶ {msg}{RESET}\n')
        out.append('\n')

    out.append(format_build_line(profile, build_pick))

    if coach_build_line:
        out.append(coach_build_line)

    if not is_aram:
        drake_t, baron_t = timers.get('drake'), timers.get('baron')
        drake_str = f'{TIER_COLOR["Major"]}UP{RESET}' if drake_t == 0 else fmt_mmss(drake_t)
        baron_str = f'{TIER_COLOR["Major"]}UP{RESET}' if baron_t == 0 else fmt_mmss(baron_t)
        obj_line = f'{DIM}objectives:{RESET} drake {drake_str}  ·  baron {baron_str}'
        if ev['drakes']:
            obj_line += f'  ·  drakes: {len(ev["drakes"])}'
        if ev['barons']:
            obj_line += f'  ·  baron taken x{len(ev["barons"])}'
        if ev['grubs']:
            obj_line += f'  ·  grubs: {ev["grubs"]}'
        out.append(obj_line + '\n\n')

    formatted = []
    for e in reversed(ev['raw']):
        s = format_event(e)
        if s:
            formatted.append(s)
        if len(formatted) >= 5:
            break
    if formatted:
        out.append(f'{DIM}recent:{RESET}\n')
        for s in formatted:
            out.append(f'  {DIM}{s}{RESET}\n')
        out.append('\n')

    if not enemies:
        out.append('No enemy data yet.\n')
        return ''.join(out)

    def sort_key(p):
        entry = matchups.get(normalize(p.get('championName', '')))
        return TIER_ORDER.get(entry[2] if entry else 'Tiny', 99)

    for p in sorted(enemies, key=sort_key):
        champ = p.get('championName', '?')
        pos = p.get('position', '') or ''
        level = p.get('level', '?')
        items = [i for i in (p.get('items') or []) if (i or {}).get('itemID')]
        scores = p.get('scores') or {}
        kda = f'{scores.get("kills",0)}/{scores.get("deaths",0)}/{scores.get("assists",0)}'
        cs = scores.get('creepScore', 0)
        extras = f'{kda} · {cs}cs · lvl {level} · {len(items)} items'
        if p.get('isDead'):
            rt = int(p.get('respawnTimer') or 0)
            extras += f' · DEAD {rt}s'
        entry = matchups.get(normalize(champ))
        disp = entry[0] if entry else champ
        tier = entry[2] if entry else None
        personal = entry[1].strip() if entry else ''
        auto = matchup_db.get_note(your_champ, champ) if (matchup_db and your_champ) else None
        sections = []
        if personal:
            sections.append(f'{DIM}[yours]{RESET} {personal}')
        if auto:
            sections.append(f'{DIM}[auto]{RESET} {auto}')
        if not sections and champ_db:
            kit = champ_db.get_note(champ)
            if kit:
                sections.append(f'{DIM}[kit]{RESET} {_clean_db_note(kit)}')
        body = '\n'.join(sections)
        if body:
            out.append(render_matchup(disp, pos, tier, body, max_chars, extras=extras))
        else:
            out.append(render_matchup(disp, pos, tier, '', max_chars, extras=extras + '  — no notes'))
    return ''.join(out)


def render_champ_select(session, champ_index, matchups, max_chars, champ_folder, profile=None, build_pick=None):
    out = [CLEAR]

    timer = session.get('timer') or {}
    phase = timer.get('phase', '?')
    notes_label = f'notes: {champ_folder}' if champ_folder else 'no notes'
    out.append(header_line(f'leeg live · CHAMP SELECT · {phase} · {notes_label}'))

    out.append(format_build_line(profile, build_pick))

    bans = session.get('bans') or {}
    my_bans = [champ_index.get(b.get('championId', 0), '?') for b in bans.get('myTeamBans', []) if b.get('championId')]
    their_bans = [champ_index.get(b.get('championId', 0), '?') for b in bans.get('theirTeamBans', []) if b.get('championId')]
    if my_bans or their_bans:
        out.append(f'{BOLD}Bans{RESET}  ')
        out.append(f'you: {", ".join(my_bans) or "—"}  ·  them: {", ".join(their_bans) or "—"}\n\n')

    my_cell = session.get('localPlayerCellId', -1)
    my_team = session.get('myTeam') or []
    my_pos = None
    my_champ_id = None
    for p in my_team:
        if p.get('cellId') == my_cell:
            my_pos = (p.get('assignedPosition') or '').lower() or None
            my_champ_id = p.get('championId') or p.get('championPickIntent') or 0
            break

    my_champ_name = champ_index.get(my_champ_id, '?') if my_champ_id else '?'
    out.append(f'{BOLD}You:{RESET} {my_champ_name}')
    if my_pos:
        out.append(f' ({my_pos})')
    out.append('\n\n')

    if my_champ_id and my_champ_name != '?' and not champ_folder:
        out.append(f'{TIER_COLOR["Major"]}NOTE: no matchup notes for {my_champ_name} (add leeg/<champ>/matchups.md){RESET}\n\n')

    their_team = session.get('theirTeam') or []
    visible = [p for p in their_team if (p.get('championId') or 0) > 0]

    if not visible:
        out.append(f'{DIM}Enemy picks not visible yet (waiting for them to lock in){RESET}\n')
        return ''.join(out)

    def sort_key(p):
        cid = p.get('championId') or 0
        name = champ_index.get(cid, '')
        entry = matchups.get(normalize(name))
        return TIER_ORDER.get(entry[2] if entry else 'Tiny', 99)

    for p in sorted(visible, key=sort_key):
        cid = p.get('championId') or 0
        name = champ_index.get(cid, f'#{cid}')
        pos = (p.get('assignedPosition') or '').lower()
        is_lane = bool(my_pos and pos == my_pos)
        marker = '← your lane' if is_lane else ''
        entry = matchups.get(normalize(name))
        if entry:
            disp, body, tier = entry
            out.append(render_matchup(disp, pos, tier, body, max_chars, marker=marker))
        else:
            out.append(render_matchup(name, pos, None, '', max_chars, marker=marker, extras='— no notes'))

    return ''.join(out)


def render_idle(hosts, lockfile, lockfile_size, available, override):
    out = [CLEAR]
    out.append(header_line('leeg live · WAITING'))
    if lockfile and lockfile_size:
        out.append('League client is running but no champ select / game session is active.\n\n')
    elif lockfile:
        out.append('Lockfile found but empty — the League client is closed.\n\n')
    else:
        out.append('League client not detected.\n\n')
    if override:
        out.append(f'{DIM}Notes override: --champ {override}{RESET}\n')
    else:
        out.append(f'{DIM}Notes: auto-detect from champ select / game (override with --champ){RESET}\n')
    out.append(f'{DIM}Available champ notes: {", ".join(available) or "(none)"}{RESET}\n')
    out.append(f'{DIM}Live Client API hosts: {", ".join(hosts)}{RESET}\n')
    out.append(f'{DIM}Lockfile: {lockfile or "(not found — pass --lockfile to override)"}{RESET}\n')
    out.append(f'{DIM}On WSL2: if the client is running but nothing connects, see README §\n')
    out.append(f'         "First-time setup (WSL2)". Win11 22H2+ uses mirrored networking;\n')
    out.append(f'         Win10 needs the netsh portproxy workaround.{RESET}\n')
    return ''.join(out)


# ─── main loop ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--champ', default=None, help='force a specific champion folder (default: auto-detect)')
    ap.add_argument('--host', default=None, help='override Live Client / LCU host (default: auto-detect)')
    ap.add_argument('--lockfile', default=None, help='path to League lockfile (default: auto-detect under /mnt)')
    ap.add_argument('--max-chars', type=int, default=600, help='max chars per matchup body')
    ap.add_argument('--poll', type=float, default=POLL_SECONDS, help='poll interval in seconds')
    ap.add_argument('--add-champ', dest='add_champ', metavar='NAME',
                    help='create a new champion folder with template files and exit')
    ap.add_argument('--source', dest='source', metavar='URL',
                    help='source guide URL (used with --add-champ; saved to meta.json)')
    ap.add_argument('--no-tts', action='store_true', default=False,
                    help='disable voice coach (TTS is on by default)')
    ap.add_argument('--show-matchups', dest='show_matchups', metavar='CHAMP',
                    help='print all cached matchup pair notes for a player champion and exit')
    args = ap.parse_args()

    if args.add_champ:
        scaffold_champ(args.add_champ, args.source, fetch_current_patch())
        return

    if args.show_matchups:
        show_matchup_notes(args.show_matchups)
        return

    available = available_champs()
    if not available:
        print(f'No champ folders with build.md or matchups.md found under {LEEG_ROOT}', file=sys.stderr)
        sys.exit(1)

    override = args.champ
    if override:
        folder = LEEG_ROOT / override
        if not folder.is_dir() or not any((folder / f).exists() for f in ('matchups.md', 'build.md')):
            print(f'Cannot find {LEEG_ROOT / override} (need at least a build.md)', file=sys.stderr)
            sys.exit(1)

    champ_cache = {}
    if override:
        load_champ_data(override, champ_cache)

    hosts = candidate_hosts(args.host)
    print(f'leeg live · loading champion + item indices...', flush=True)
    champ_index, champ_aliases = fetch_champion_index()
    if not champ_index:
        print('  warning: could not fetch champion index from CommunityDragon', file=sys.stderr)
    else:
        # Audit: champs the Live API may surface (by alias) that we don't classify.
        # Falls back to Mixed for anything missing — safe but loses build-picker fidelity.
        unclassified = sorted(
            normalize(a) for a in champ_aliases
            if normalize(a) and normalize(a) not in CHAMP_DAMAGE
        )
        if unclassified:
            print(f'  note: {len(unclassified)} champ(s) missing from CHAMP_DAMAGE — '
                  f'will default to Mixed: {", ".join(unclassified)}', flush=True)
        # Untagged-by-threat audit: champs CHAMP_DAMAGE classifies but
        # CHAMP_THREAT_TAGS doesn't. Untagged → no TEAM THREATS contribution
        # (safe failure mode). Lists champs worth considering for tagging.
        untagged = sorted(c for c in CHAMP_DAMAGE if c not in CHAMP_THREAT_TAGS)
        if untagged:
            print(f'  note: {len(untagged)} champ(s) have no TEAM THREATS tag '
                  f'(no comp-aware contribution): {", ".join(untagged)}', flush=True)
    item_index = fetch_item_index()
    if not item_index:
        print('  warning: could not fetch item index — falling back to archetype-only', file=sys.stderr)

    current_patch = fetch_current_patch()
    print(f'leeg live · current patch: {current_patch or "unknown"}', flush=True)
    if current_patch:
        for folder in available:
            meta = read_meta(folder)
            if not meta:
                print(f'  drift: {folder} — no meta.json (run with --add-champ to scaffold or backfill manually)')
                continue
            drift = patch_drift(meta.get('patch_reviewed'), current_patch)
            if drift:
                print(f'  drift: {folder} — {drift}')

    lockfile = find_lockfile(args.lockfile)
    mode_label = f'override --champ {override}' if override else f'auto-detect ({len(available)} profiles)'
    print(f'leeg live · live API hosts={hosts}  lockfile={lockfile}  notes={mode_label}', flush=True)

    coach = Coach()
    coach.tts = not args.no_tts
    if coach.tts:
        warmup_tts()
    if coach.enabled:
        tts_label = f' · tts={_tts_mode()}' if coach.tts else ' · tts=off'
        print(f'leeg live · coach: enabled (model={coach.model}, cooldown={coach.cooldown_seconds}s{tts_label})', flush=True)
    else:
        print(f'leeg live · coach: disabled — {coach.error_msg}', flush=True)

    champ_db = ChampDB(
        client=coach.client if coach.enabled else None,
        patch=current_patch,
    )
    db_count = len(champ_db._notes)
    print(f'leeg live · champ db: {db_count} champion note(s) cached', flush=True)
    matchup_db = MatchupDB(
        client=coach.client if coach.enabled else None,
        patch=current_patch,
    )
    pair_count = len(matchup_db._notes)
    print(f'leeg live · matchup db: {pair_count} pair note(s) cached', flush=True)

    last_sig = None
    last_state = None
    last_champ = None  # last successfully resolved champ folder, used as fallback
    while True:
        # Mode 1: in-game
        data, host = fetch_game(hosts)
        if data:
            your_champ_api, enemies, me_game = find_active_team(data)
            if override:
                champ_folder = override
            else:
                champ_folder = champ_to_folder(your_champ_api, available) or last_champ
            cdata = load_champ_data(champ_folder, champ_cache) if champ_folder else {'matchups': {}, 'build_variants': []}
            if champ_folder:
                last_champ = champ_folder

            profile = compute_damage_profile(enemies, item_index)
            my_pos = (me_game.get('position') or '').upper() if me_game else ''
            laner = next((e for e in enemies if my_pos and (e.get('position') or '').upper() == my_pos), None)
            laner_entry = cdata['matchups'].get(normalize(laner.get('championName', ''))) if laner else None
            laner_tag = laner_build_tag(laner_entry)
            build_pick = pick_build_variant(cdata['build_variants'], profile[0], preferred_tag=laner_tag) if cdata['build_variants'] else None

            sig = json.dumps({
                'mode': 'game',
                'champ': champ_folder,
                'profile': profile[0],
                't': int((data.get('gameData') or {}).get('gameTime', 0)),
                'players': [
                    (p.get('championName'), p.get('level'),
                     tuple((i or {}).get('itemID') for i in (p.get('items') or [])))
                    for p in (data.get('allPlayers') or [])
                ],
            }, sort_keys=True)
            if sig != last_sig:
                sys.stdout.write(render_in_game(data, cdata['matchups'], host, args.max_chars, champ_folder, profile, build_pick, coach, item_index, champ_db, swaps=cdata.get('swaps') or [], win_condition=cdata.get('win_conditions') or '', side_selection=cdata.get('side_selection') or '', matchup_db=matchup_db))
                sys.stdout.flush()
                last_sig, last_state = sig, 'game'
            time.sleep(args.poll)
            continue

        # Mode 2: champ select
        cs = None
        if lockfile and lockfile.exists():
            lockinfo = parse_lockfile(lockfile)
            if lockinfo:
                cs = fetch_champ_select(lockinfo, hosts)
        if cs:
            my_cell = cs.get('localPlayerCellId', -1)
            my_champ_id = 0
            for p in (cs.get('myTeam') or []):
                if p.get('cellId') == my_cell:
                    my_champ_id = p.get('championId') or p.get('championPickIntent') or 0
                    break
            my_champ_name = champ_index.get(my_champ_id, '') if my_champ_id else ''
            if override:
                champ_folder = override
            else:
                champ_folder = champ_to_folder(my_champ_name, available) or last_champ
            cdata = load_champ_data(champ_folder, champ_cache) if champ_folder else {'matchups': {}, 'build_variants': []}
            if champ_folder:
                last_champ = champ_folder

            their_team = cs.get('theirTeam') or []
            cs_enemies = [{'championName': champ_index.get(p.get('championId') or 0, '')} for p in their_team if (p.get('championId') or 0) > 0]
            profile = compute_damage_profile(cs_enemies)  # no items in champ select
            build_pick = pick_build_variant(cdata['build_variants'], profile[0]) if cdata['build_variants'] and cs_enemies else None

            sig = json.dumps({
                'mode': 'cs',
                'champ': champ_folder,
                'profile': profile[0],
                'n_enemies': len(cs_enemies),
                'phase': (cs.get('timer') or {}).get('phase'),
                'bans': cs.get('bans'),
                'myTeam': [(p.get('cellId'), p.get('championId'), p.get('championPickIntent'), p.get('assignedPosition')) for p in (cs.get('myTeam') or [])],
                'theirTeam': [(p.get('championId'), p.get('assignedPosition')) for p in (cs.get('theirTeam') or [])],
            }, sort_keys=True)
            if sig != last_sig:
                sys.stdout.write(render_champ_select(cs, champ_index, cdata['matchups'], args.max_chars, champ_folder, profile, build_pick))
                sys.stdout.flush()
                last_sig, last_state = sig, 'cs'
            time.sleep(args.poll)
            continue

        # Mode 3: idle
        if last_state != 'idle':
            try:
                lf_size = lockfile.stat().st_size if lockfile and lockfile.exists() else 0
            except OSError:
                lf_size = 0
            sys.stdout.write(render_idle(hosts, lockfile, lf_size, available, override))
            sys.stdout.flush()
            last_state = 'idle'
            last_sig = None
        time.sleep(args.poll)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print()
