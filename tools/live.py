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
import string
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
    "wukong xayah xinzhao yasuo yone yorick yunara zed zeri"
).split()
_AP = (
    "ahri akali alistar amumu anivia annie aurelionsol aurora azir bard "
    "blitzcrank brand braum cassiopeia chogath diana drmundo ekko elise "
    "evelynn fiddlesticks fizz galio gragas gwen heimerdinger hwei ivern "
    "janna karma karthus kassadin katarina kayle kennen leblanc leona lillia "
    "lissandra lulu lux malphite malzahar maokai mel milio mordekaiser morgana "
    "nami nautilus neeko nunuwillump orianna rakan rell renataglasc rumble "
    "ryze sejuani seraphine singed sona soraka swain sylas syndra tahmkench "
    "taliyah taric teemo twistedfate veigar velkoz vex viktor vladimir xerath "
    "yuumi zac ziggs zilean zoe zyra"
).split()
_MIXED = "corki kogmaw ornn poppy shaco shen thresh".split()
CHAMP_DAMAGE = {**{c: 'AD' for c in _AD}, **{c: 'AP' for c in _AP}, **{c: 'Mixed' for c in _MIXED}}
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


# ─── champ folder discovery & lazy loading ──────────────────────────────────

def available_champs():
    """Folder names under LEEG_ROOT that contain a matchups.md."""
    return sorted(
        d.name for d in LEEG_ROOT.iterdir()
        if d.is_dir() and (d / 'matchups.md').exists()
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
    cache[champ_folder] = {
        'matchups': parse_matchups(matchups_path) if matchups_path.exists() else {},
        'build_variants': parse_build_variants(build_path) if build_path.exists() else [],
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
        kind = item_index.get(item_id)
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


def parse_build_variants(md_path):
    """Find ### sections under any ## that mentions 'build' or 'example'.
    Returns list of (heading, body)."""
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


def pick_build_variant(variants, profile_kind):
    """Match comp profile to a build.md variant. Falls back to Standard."""
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


def _cdragon_get(url):
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (compatible; leeg/1.0)'},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def fetch_champion_index():
    try:
        data = _cdragon_get(CHAMPION_INDEX_URL)
        return {c['id']: c.get('name', f"#{c['id']}") for c in data if c.get('id', -1) != -1}
    except Exception:
        return {}


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


def fetch_item_index():
    """Returns {itemID: 'AP'|'AD'|'Tank'|'Other'}. Empty dict on failure."""
    try:
        data = _cdragon_get(ITEMS_INDEX_URL)
        return {it['id']: classify_item(it.get('categories') or []) for it in data if it.get('id')}
    except Exception:
        return {}


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
    enemies = [p for p in players if p.get('team') and p.get('team') != your_team]
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


def tactical_advice(data, me, enemies, ev, timers):
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

    advice.sort(key=lambda x: x[0])
    return advice[:5]


# ─── LLM coach (Claude) ─────────────────────────────────────────────────────

COACH_SCHEMA = {
    "type": "object",
    "properties": {
        "bullets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "1-3 short imperative tactical bullet lines, each <=90 chars. Lead with a verb (PUSH, RECALL, WARD, KITE, PEEL, TRADE, HOLD, ROTATE, FREEZE, ENGAGE, DISENGAGE, BACK).",
        },
        "live_build": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Your recommended 4-6 item build path for this game, in build order. Items already in the player's inventory should appear first in their built positions, then planned items. This is meant to be displayed persistently on screen — keep it coherent across calls and only change it when game state materially shifts (e.g., enemy hard pivots damage profile, fed enemy carry, new objective threat).",
        },
        "build_diverged": {
            "type": "boolean",
            "description": "True if live_build differs materially from the RULE-BASED BUILD DEFAULT shown in the game state. False if you're endorsing the rule-based default as-is.",
        },
        "build_change_reason": {
            "type": "string",
            "description": "If build_diverged is true, a short reason (<=100 chars) for the deviation. Empty string if false.",
        },
    },
    "required": ["bullets", "live_build", "build_diverged", "build_change_reason"],
    "additionalProperties": False,
}


class Coach:
    """Calls Claude Haiku 4.5 on significant events to give live coaching.
    System prompt is the champ's matchups.md + build.md, prompt-cached so
    only the first call of a session pays the full input cost.
    """

    DEFAULT_MODEL = 'claude-haiku-4-5'
    DEFAULT_COOLDOWN = 25
    MAX_TOKENS = 250

    def __init__(self, model=None, cooldown_seconds=None):
        self.model = model or os.environ.get('LEEG_COACH_MODEL', self.DEFAULT_MODEL)
        self.cooldown_seconds = cooldown_seconds or self.DEFAULT_COOLDOWN
        self.client = None
        self.error_msg = None
        if not _ANTHROPIC_AVAILABLE:
            self.error_msg = 'anthropic SDK not installed (pip install anthropic)'
        elif not os.environ.get('ANTHROPIC_API_KEY'):
            self.error_msg = 'ANTHROPIC_API_KEY not set'
        else:
            try:
                self.client = anthropic.Anthropic()
            except Exception as e:
                self.error_msg = f'init failed: {e}'
        self.lock = threading.Lock()
        self.in_flight = False
        self.last_call = 0.0
        self.last_response = None
        self.last_response_at = 0.0
        self.last_trigger = ''
        self.last_event_count = 0
        self.last_laner_dead = False
        self.drake_warned = False
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
        # Watchdog timeout for a single in-flight API call. If a call is still
        # marked in_flight after this many seconds, we force-clear so the coach
        # can fire again. Acts as a backstop on top of the per-request 20s
        # timeout we set on the SDK client.
        self.WATCHDOG_SECONDS = 30

    def _reset_for_new_game(self):
        """Clear all per-game state. Called when the game time goes backward
        (new game / restart) or when the script transitions idle -> game."""
        self.last_response = None
        self.last_response_at = 0.0
        self.last_trigger = ''
        self.last_event_count = 0
        self.last_laner_dead = False
        self.drake_warned = False
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
        matchups_path = LEEG_ROOT / champ_folder / 'matchups.md'
        build_path = LEEG_ROOT / champ_folder / 'build.md'
        matchups_text = matchups_path.read_text() if matchups_path.exists() else '(no matchup notes)'
        build_text = build_path.read_text() if build_path.exists() else '(no build notes)'
        prompt = (
            f"You are an in-game League of Legends coach for someone playing {champ_folder}. "
            f"Watch the live game state and tell them what to do RIGHT NOW.\n\n"
            f"Style rules:\n"
            f"- Output 1-3 short bullet lines, each <= 90 chars\n"
            f"- Lead with an imperative verb (PUSH, RECALL, WARD, KITE, PEEL, TRADE, HOLD, ROTATE, FREEZE, ENGAGE, DISENGAGE, BACK)\n"
            f"- Reference specific champions, items, or timers when it helps\n"
            f"- No preamble, no moralizing, no emojis, no general advice\n"
            f"- If nothing urgent, give one tactical reminder relevant to the current state\n\n"
            f"Output format:\n"
            f"- You will produce a JSON object matching the provided schema.\n"
            f"- `bullets`: 1-3 short imperative tactical lines for RIGHT NOW.\n"
            f"- `live_build`: your recommended 4-6 item path for this game. This is rendered persistently on screen, so it must be COHERENT and STABLE across calls. Items the player already owns appear first in their built positions; planned items follow. Once you commit to a path, keep recommending it until the game state materially changes.\n"
            f"- `build_diverged`: true only if your live_build differs materially from the RULE-BASED BUILD DEFAULT shown in the game state. If you're endorsing the default, set this false and copy its items into live_build.\n"
            f"- `build_change_reason`: short reason for the deviation (only if diverged).\n\n"
            f"Memory you have access to each call:\n"
            f"- YOUR CURRENT BUILD COMMITMENT — the latest live_build you locked in, with timestamp + reason. This is your game-long anchor for the build path. Stay on it unless something material has changed since the commitment time.\n"
            f"- YOUR RECENT TACTICAL ADVICE — bullets from your last 5 calls. Use to avoid contradicting recent tactical guidance.\n"
            f"- TEAM SCORE — kills/drakes/barons/towers per side. Use for macro reads (we're ahead vs behind, contest objectives vs play safe, etc.).\n\n"
            f"Consistency rules (IMPORTANT):\n"
            f"- BUILD COMMITMENT: once you commit to a path, KEEP IT across calls. Only change it when game state has materially changed (enemy team pivots damage profile, a key carry gets fed/falls off, an objective threat changes the game plan). When you do change it, set build_diverged=true and explain in build_change_reason.\n"
            f"- TACTICAL BULLETS: build on prior advice. If you previously said to skip an item or path, don't later recommend it without a reason that ties to a recent event.\n"
            f"- The 'rule-based build path' is the deterministic default from build.md. REFERENCE only — deviate when warranted, then stick with the deviation.\n"
            f"- Do not yo-yo. If you wouldn't justify the change to a teammate, don't make it.\n\n"
            f"=== CHAMPION MATCHUP NOTES (for enemies you face as {champ_folder}) ===\n"
            f"{matchups_text}\n\n"
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
        if self.last_response is None and game_time > 60:
            return 'opening'

        # New significant events
        new_events = ev['raw'][self.last_event_count:]
        self.last_event_count = len(ev['raw'])
        you_name = me.get('riotIdGameName') or (me.get('summonerName') or '').split('#', 1)[0]
        for e in new_events:
            en = e.get('EventName')
            if en in ('FirstBlood', 'Ace', 'BaronKill', 'InhibKilled'):
                return en.lower()
            if en == 'DragonKill':
                return 'drake_taken'
            if en == 'ChampionKill':
                if e.get('VictimName') == you_name:
                    return 'you_died'
                if e.get('KillerName') == you_name:
                    return 'you_killed'

        # Laner state transition
        my_pos = (me.get('position') or '').upper()
        if my_pos:
            for e in enemies:
                if (e.get('position') or '').upper() == my_pos:
                    dead_now = bool(e.get('isDead'))
                    if dead_now and not self.last_laner_dead:
                        self.last_laner_dead = True
                        return 'laner_died'
                    if not dead_now:
                        self.last_laner_dead = False
                    break

        # Drake about to spawn
        drake = timers.get('drake')
        if drake is not None and 20 <= drake <= 40 and not self.drake_warned:
            self.drake_warned = True
            return 'drake_soon'
        if drake is not None and drake > 60:
            self.drake_warned = False

        # Periodic
        if time.time() - self.last_call > 90:
            return 'periodic'
        return None

    def request_async(self, trigger, champ_folder, user_message, game_time):
        if not self.client or self.in_flight:
            return
        self.in_flight = True
        self.last_call = time.time()
        self.last_trigger = trigger
        self.last_call_game_time = game_time
        threading.Thread(
            target=self._call,
            args=(self.build_system(champ_folder), user_message),
            daemon=True,
        ).start()

    def _call(self, system, user):
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
                output_config={'format': {'type': 'json_schema', 'schema': COACH_SCHEMA}},
            )
            text = ''.join(b.text for b in resp.content if b.type == 'text').strip()
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {}
            bullets = [b for b in (parsed.get('bullets') or []) if isinstance(b, str) and b.strip()]
            live_build = [s for s in (parsed.get('live_build') or []) if isinstance(s, str) and s.strip()]
            diverged = bool(parsed.get('build_diverged'))
            reason = (parsed.get('build_change_reason') or '').strip()
            with self.lock:
                self.last_response = text
                self.last_bullets = bullets
                self.last_live_build = live_build
                self.last_diverged = diverged
                self.last_change_reason = reason
                self.last_response_at = time.time()
                self.errors = 0
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
            with self.lock:
                self.errors += 1
                self.last_response = f'(coach error: {type(e).__name__}: {e})'
                if self.errors >= 5:
                    self.client = None
                    self.error_msg = f'disabled after {self.errors} errors'
        finally:
            with self.lock:
                self.in_flight = False

    def display_block(self):
        with self.lock:
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


def build_coach_message(data, me, enemies, ev, timers, profile, build_pick, trigger,
                        recent_responses=None, committed_build=None):
    game_time = int((data.get('gameData') or {}).get('gameTime', 0))
    mins, secs = divmod(game_time, 60)
    lines = [f'TRIGGER: {trigger}', f'TIME: {mins}:{secs:02d}']

    your_team = me.get('team') if me else None
    score = _team_score_summary(data, ev, your_team)
    if score:
        lines.append(f'TEAM SCORE (you-enemy): {score}')

    if me:
        scores = me.get('scores') or {}
        items = [(i or {}).get('displayName', '?') for i in (me.get('items') or []) if (i or {}).get('itemID')]
        pos = (me.get('position') or '').lower() or '?'
        lines.append(
            f'YOU: {me.get("championName")} ({pos}) lvl {me.get("level")} '
            f'{scores.get("kills",0)}/{scores.get("deaths",0)}/{scores.get("assists",0)} '
            f'{scores.get("creepScore",0)}cs items=[{", ".join(items)}]'
        )
        if me.get('isDead'):
            lines.append(f'YOU DEAD ({int(me.get("respawnTimer") or 0)}s)')

    lines.append('ENEMIES:')
    for e in enemies:
        sc = e.get('scores') or {}
        items = [(i or {}).get('displayName', '?') for i in (e.get('items') or []) if (i or {}).get('itemID')]
        pos = (e.get('position') or '').lower() or '?'
        line = (
            f'  {e.get("championName")} ({pos}) lvl {e.get("level")} '
            f'{sc.get("kills",0)}/{sc.get("deaths",0)}/{sc.get("assists",0)} '
            f'{sc.get("creepScore",0)}cs items=[{", ".join(items)}]'
        )
        if e.get('isDead'):
            line += f' DEAD({int(e.get("respawnTimer") or 0)}s)'
        lines.append(line)

    if profile and profile[3] > 0:
        label, ap, ad, _, _ = profile
        lines.append(f'COMP: {label} ({ap:g} AP / {ad:g} AD)')
    if build_pick:
        heading, body = build_pick
        lines.append(f'RULE-BASED BUILD DEFAULT (reference only — feel free to override): {heading} — {build_path_summary(body)}')

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
        lines.append(f'Path: {" · ".join(committed_build["items"])}')
        lines.append('KEEP THIS PATH unless game state has materially changed since the commitment time.')
        lines.append('If you keep it, return the SAME items in live_build and set build_diverged accordingly.')
        lines.append('If you change it, set build_diverged=true and explain why in build_change_reason.')

    if recent_responses:
        lines.append('')
        lines.append('YOUR RECENT TACTICAL ADVICE (last 5 calls — stay consistent unless game state materially changed):')
        for r in recent_responses:
            lines.append(f'  [{r["time"]} | {r["trigger"]}]')
            if r.get('bullets'):
                for b in r['bullets']:
                    lines.append(f'    - {b}')

    lines.append('')
    lines.append('Now: emit JSON with bullets + live_build + build_diverged + build_change_reason. Anchor live_build to your committed path above. Tactical bullets should react to current state.')
    return '\n'.join(lines)



# ─── LCU API (champ select / lobby) ─────────────────────────────────────────

def find_lockfile(explicit=None):
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    candidates = []
    for letter in string.ascii_lowercase:
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
        out.append(truncate(body, max_chars) + '\n')
    out.append('\n')
    return ''.join(out)


def render_in_game(data, matchups, host, max_chars, champ_folder, profile=None, build_pick=None, coach=None):
    your_champ, enemies, me = find_active_team(data)
    ev = parse_events(data)
    game_time = int((data.get('gameData') or {}).get('gameTime', 0))
    mins, secs = divmod(game_time, 60)
    timers = objective_timers(game_time, ev)
    advice = tactical_advice(data, me, enemies, ev, timers)

    if coach is not None:
        trigger = coach.maybe_trigger(ev, me, enemies, timers, game_time, champ_folder)
        if trigger:
            user_msg = build_coach_message(
                data, me, enemies, ev, timers, profile, build_pick, trigger,
                recent_responses=list(coach.recent_responses),
                committed_build=dict(coach.committed_build) if coach.committed_build else None,
            )
            coach.request_async(trigger, champ_folder, user_msg, game_time)

    notes_label = f'notes: {champ_folder}' if champ_folder else 'no notes'
    out = [CLEAR]
    out.append(header_line(f'leeg live · IN GAME · {your_champ or "?"} · {mins}:{secs:02d} · {host} · {notes_label}'))

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
        if entry:
            disp, body, tier = entry
            out.append(render_matchup(disp, pos, tier, body, max_chars, extras=extras))
        else:
            out.append(render_matchup(champ, pos, None, '', max_chars, extras=extras + '  — no notes'))
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
    out.append(f'{DIM}On WSL2: if the client is running but nothing connects, you likely need\n')
    out.append(f'         WSL2 mirrored networking. Add to ~/.wslconfig:\n')
    out.append(f'           [wsl2]\n           networkingMode=mirrored\n')
    out.append(f'         Then `wsl --shutdown` from PowerShell.{RESET}\n')
    return ''.join(out)


# ─── main loop ──────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--champ', default=None, help='force a specific champion folder (default: auto-detect)')
    ap.add_argument('--host', default=None, help='override Live Client / LCU host (default: auto-detect)')
    ap.add_argument('--lockfile', default=None, help='path to League lockfile (default: auto-detect under /mnt)')
    ap.add_argument('--max-chars', type=int, default=600, help='max chars per matchup body')
    ap.add_argument('--poll', type=float, default=POLL_SECONDS, help='poll interval in seconds')
    args = ap.parse_args()

    available = available_champs()
    if not available:
        print(f'No champ folders with matchups.md found under {LEEG_ROOT}', file=sys.stderr)
        sys.exit(1)

    override = args.champ
    if override:
        if not (LEEG_ROOT / override / 'matchups.md').exists():
            print(f'Cannot find {LEEG_ROOT / override / "matchups.md"}', file=sys.stderr)
            sys.exit(1)

    champ_cache = {}
    if override:
        load_champ_data(override, champ_cache)

    hosts = candidate_hosts(args.host)
    print(f'leeg live · loading champion + item indices...', flush=True)
    champ_index = fetch_champion_index()
    if not champ_index:
        print('  warning: could not fetch champion index from CommunityDragon', file=sys.stderr)
    item_index = fetch_item_index()
    if not item_index:
        print('  warning: could not fetch item index — falling back to archetype-only', file=sys.stderr)

    lockfile = find_lockfile(args.lockfile)
    mode_label = f'override --champ {override}' if override else f'auto-detect ({len(available)} profiles)'
    print(f'leeg live · live API hosts={hosts}  lockfile={lockfile}  notes={mode_label}', flush=True)

    coach = Coach()
    if coach.enabled:
        print(f'leeg live · coach: enabled (model={coach.model}, cooldown={coach.cooldown_seconds}s)', flush=True)
    else:
        print(f'leeg live · coach: disabled — {coach.error_msg}', flush=True)

    last_sig = None
    last_state = None
    last_champ = None  # last successfully resolved champ folder, used as fallback
    while True:
        # Mode 1: in-game
        data, host = fetch_game(hosts)
        if data:
            your_champ_api, enemies, _ = find_active_team(data)
            if override:
                champ_folder = override
            else:
                champ_folder = champ_to_folder(your_champ_api, available) or last_champ
            cdata = load_champ_data(champ_folder, champ_cache) if champ_folder else {'matchups': {}, 'build_variants': []}
            if champ_folder:
                last_champ = champ_folder

            profile = compute_damage_profile(enemies, item_index)
            build_pick = pick_build_variant(cdata['build_variants'], profile[0]) if cdata['build_variants'] else None

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
                sys.stdout.write(render_in_game(data, cdata['matchups'], host, args.max_chars, champ_folder, profile, build_pick, coach))
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
