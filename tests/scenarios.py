"""Scenario harness: mirrors live.py's main-loop build pick + render_in_game trigger
block + Coach._call validator chain, with a hand-written coach live_build."""
import _setup  # noqa: F401  (path + quiet logging)
import live
from live import normalize

IDX = live.fetch_item_index()
N2I = live._build_name_to_id(IDX)
CACHE = {}


def P(name, k, d, a, items, pos, team='CHAOS', lvl=14):
    return {'championName': name, 'position': pos, 'team': team, 'level': lvl,
            'scores': {'kills': k, 'deaths': d, 'assists': a, 'creepScore': 150},
            'items': [{'displayName': i, 'itemID': live.resolve_item_id(i, N2I)} for i in items]}


def run(title, folder, me, enemies, game_time, coach_build=None):
    print(f'\n=== {title}')
    cd = live.load_champ_data(folder, CACHE)
    profile = live.compute_damage_profile(enemies, IDX)
    my_pos = (me.get('position') or '').upper()
    laner = next((e for e in enemies if my_pos and (e.get('position') or '').upper() == my_pos), None)
    laner_entry = cd['matchups'].get(normalize(laner.get('championName', ''))) if laner else None
    th = live.compute_team_threats(enemies, IDX)
    no_w = any(normalize(e['championName']) in live._ANTI_WARMOGS_CHAMPS for e in enemies) or \
        any(normalize(e['championName']) in live._LOCKDOWN_CC_CHAMPS for e in enemies)
    pick = live.pick_build_variant(cd['build_variants'], profile[0], preferred_tag=live.laner_build_tag(laner_entry),
                                   ap_count=profile[1], ad_count=profile[2],
                                   has_healer=bool(th['tags'].get('HEALING')), no_warmogs=no_w,
                                   ap_assassin=live.has_ap_assassin(enemies, IDX),
                                   tank_count=live.count_tanks(enemies, IDX))
    print('pick:', pick[0], '|', live.build_path_summary(pick[1]))
    # --- render_in_game trigger block (copied order) ---
    active = live.filter_swaps(cd.get('swaps') or [], enemies, profile[1], profile[2], my_pos, game_time, IDX)
    my_boots = {normalize((i or {}).get('displayName', '')) for i in me['items']} & live._BOOTS_NORMS
    if my_boots:
        active = [s for s in active if normalize(s[0]) not in live._BOOTS_NORMS or normalize(s[0]) in my_boots]
    an = {normalize(s[0]) for s in active}
    gated = [(n, o) for n, o, _c in (cd.get('swaps') or []) if normalize(n) not in an]
    print('active swaps:', sorted({s[0] for s in active}))
    data = {'gameData': {'gameTime': game_time}, 'activePlayer': {'currentGold': 1500},
            'allPlayers': [me] + enemies, 'events': {'Events': []}}
    msg = live.build_coach_message(data, me, enemies, live.parse_events(data), {}, profile, pick, 'periodic',
                                   item_index=IDX, team_threats=th, swaps=active,
                                   gated_items={normalize(n) for n, _ in gated}, matchups=cd['matchups'],
                                   item_pool=cd.get('item_pool'), your_champ=me['championName'])
    for l in msg.splitlines():
        if l.lstrip().startswith(('[SNOWBALLING', '[AHEAD', '[SPIKE', 'LOW THREAT', 'counters:', 'TEAM THREATS')) \
                or 'BUILD ADAPTATION' in l:
            s = l.strip()
            if 'BUILD ADAPTATION' in s:
                s = s[s.index('BUILD ADAPTATION'):][:140]
            print('  msg>', s[:170])
    prio = live.compute_priority_enemies(me, enemies, IDX)
    council = live.compute_build_council(prio, IDX, active, live._player_class(me['championName']),
                                         cd.get('item_pool'), {normalize(n) for n, _ in gated})
    states = {normalize(e['championName']): live._enemy_threat_state(e, game_time) for _r, e in prio}
    offered = [c['item'] for en in council if states.get(normalize(en['enemy'])) in ('SNOWBALLING', 'AHEAD')
               for c in en['counters']]
    print('offered counters:', offered)
    playstyles = live.allowed_playstyles(cd['build_variants'], enemies, game_time, IDX)
    print('playstyles:', [p[0] for p in playstyles])
    if coach_build is None:
        return
    # --- Coach._call validator chain ---
    lb = live.strip_starters(coach_build)
    lb = live.strip_duplicate_boots(lb)
    lb = live.strip_components(lb, IDX)
    lb = live.validate_item_names(lb, IDX)
    lb = live.validate_on_build_guide(lb, cd.get('item_pool'), IDX)
    lb = live._drop_conflicting_items(lb)
    ref = live.reference_path(lb, pick, playstyles, IDX)
    swap_pairs = [(n, o) for n, o, _c in (cd.get('swaps') or [])]
    lb = live.enforce_swap_gates(lb, gated, ref, me['items'], IDX)
    lb = live.enforce_swap_targets(lb, swap_pairs, ref, me['items'], IDX)
    lb = live.enforce_path_lock(lb, swap_pairs, ref, me['items'], IDX, offered)
    lb = live.keep_owned_boots(lb, me['items'])
    print('coach proposed:', coach_build)
    print('final build   :', lb)


MUNDO_STD = ['Heartsteel', 'Boots of Swiftness', 'Unending Despair', 'Spirit Visage', "Jak'Sho, The Protean", 'Thornmail']

# 1. The real Garen game, owns Swiftness. Coach tries Randuin's; another call tries Steelcaps.
garen = P('Garen', 12, 9, 0, ['Infinity Edge', "Berserker's Greaves", 'Phantom Dancer', 'Mortal Reminder', 'The Collector'], 'TOP', lvl=17)
g_en = [garen, P('Lee Sin', 3, 7, 1, ['Bastionbreaker', 'Voltaic Cyclosword', "Lord Dominik's Regards"], 'JUNGLE'),
        P('Zoe', 1, 5, 4, ["Luden's Echo", 'Stormsurge'], 'MIDDLE'),
        P("Kog'Maw", 0, 7, 1, ["Guinsoo's Rageblade", 'Navori Flickerblade'], 'BOTTOM'),
        P('Teemo', 0, 6, 1, ["Zaz'Zak's Realmspike", 'Malignance'], 'UTILITY')]
mundo = P('Dr. Mundo', 1, 5, 4, ['Heartsteel', 'Spirit Visage', 'Unending Despair', 'Boots of Swiftness'], 'TOP', 'ORDER')
run('1a Garen game: coach adds Randuin', 'mundo', mundo, g_en, 1740,
    ['Heartsteel', 'Boots of Swiftness', 'Unending Despair', 'Spirit Visage', "Randuin's Omen", 'Thornmail'])
run('1b Garen game: coach swaps owned Swiftness for Steelcaps', 'mundo', mundo, g_en, 1740,
    ['Heartsteel', 'Plated Steelcaps', 'Unending Despair', 'Spirit Visage', "Randuin's Omen", 'Thornmail'])

# 2. Same game but Mundo has no boots yet: Steelcaps should be allowed.
mundo_nb = P('Dr. Mundo', 1, 5, 4, ['Heartsteel', 'Unending Despair'], 'TOP', 'ORDER')
run('2 Garen game, no boots yet: Steelcaps allowed', 'mundo', mundo_nb, g_en, 1740,
    ['Heartsteel', 'Plated Steelcaps', 'Unending Despair', 'Spirit Visage', "Randuin's Omen", 'Thornmail'])

# 3. Fed AP on-hit Teemo top: no Steelcaps (magic damage).
t_en = [P('Teemo', 9, 1, 2, ["Nashor's Tooth", "Guinsoo's Rageblade", "Sorcerer's Shoes"], 'TOP'),
        P('Elise', 2, 2, 3, [], 'JUNGLE'), P('Ahri', 2, 2, 2, ["Luden's Echo"], 'MIDDLE'),
        P('Caitlyn', 2, 2, 1, ['Infinity Edge'], 'BOTTOM'), P('Senna', 1, 2, 5, [], 'UTILITY')]
run('3 fed AP Teemo: no Steelcaps', 'mundo', mundo_nb, t_en, 1200)

# 4. Fed crit Tryndamere (not in _CRIT list) in a 1-ADC comp: Randuin's.
tr_en = [P('Tryndamere', 8, 1, 1, ['Navori Flickerblade', 'Infinity Edge', "Berserker's Greaves"], 'TOP'),
         P('Amumu', 1, 2, 5, [], 'JUNGLE'), P('Lux', 2, 2, 2, ["Luden's Echo"], 'MIDDLE'),
         P('Ezreal', 2, 2, 1, ["Essence Reaver"], 'BOTTOM'), P('Janna', 0, 1, 6, [], 'UTILITY')]
run('4 fed crit Tryndamere', 'mundo', mundo_nb, tr_en, 1200)

# 5. Sivir vs fed crit Yasuo: Steelcaps must stay off for ADC.
siv = P('Sivir', 2, 3, 2, ['Infinity Edge', "Berserker's Greaves"], 'BOTTOM', 'ORDER')
y_en = [P('Yasuo', 9, 1, 1, ['Infinity Edge', 'Phantom Dancer', "Berserker's Greaves"], 'MIDDLE'),
        P('Sejuani', 1, 2, 5, [], 'JUNGLE'), P('Ornn', 0, 2, 3, [], 'TOP'),
        P('Jhin', 2, 2, 1, ['The Collector'], 'BOTTOM'), P('Lulu', 0, 1, 6, [], 'UTILITY')]
run('5 Sivir vs fed Yasuo', 'sivir', siv, y_en, 1300)

# 6. Shen vs fed Master Yi (AD on-hit): Steelcaps unlocks.
shen = P('Shen', 1, 2, 3, ['Sunfire Aegis'], 'TOP', 'ORDER')
yi_en = [P('Master Yi', 10, 1, 1, ['Blade of The Ruined King', "Guinsoo's Rageblade", "Wit's End"], 'JUNGLE'),
         P('Garen', 1, 2, 1, [], 'TOP'), P('Lux', 1, 2, 2, [], 'MIDDLE'),
         P('Jinx', 2, 2, 1, ['Infinity Edge'], 'BOTTOM'), P('Leona', 0, 2, 5, [], 'UTILITY')]
run('6 Shen vs fed Yi', 'shen', shen, yi_en, 1300)

# 7. LOW THREAT edge cases: playmaking support, rich-but-feeding ADC, real feeder.
lt_en = [P('Darius', 3, 3, 1, ['Stridebreaker'], 'TOP'), P('Thresh', 1, 4, 20, [], 'UTILITY'),
         P("Kog'Maw", 2, 5, 1, ["Guinsoo's Rageblade", 'Navori Flickerblade', "Wit's End", 'Infinity Edge'], 'BOTTOM'),
         P('Zoe', 0, 6, 1, ["Luden's Echo"], 'MIDDLE'), P('Lee Sin', 2, 2, 2, ['Eclipse'], 'JUNGLE')]
run('7 LOW THREAT edges (expect Zoe only)', 'mundo', mundo_nb, lt_en, 1500)

# 8. Early game nobody fed: nothing new should unlock.
e_en = [P('Garen', 1, 0, 0, ["Doran's Shield", 'Zeal'], 'TOP'), P('Lee Sin', 1, 1, 1, [], 'JUNGLE'),
        P('Zoe', 0, 1, 0, [], 'MIDDLE'), P('Caitlyn', 1, 0, 0, ["Doran's Blade", 'Cloak of Agility'], 'BOTTOM'),
        P('Lux', 0, 0, 1, [], 'UTILITY')]
run('8 early game quiet', 'mundo', mundo_nb, e_en, 420)

# 9. Champ select style call: no items, no item_index.
print('\n=== 9 champ select threats:', live.compute_team_threats([{'championName': n} for n in ('Garen', 'Yasuo', 'Zed')])['tags'])
print('filter_swaps no items/no index ok:', len(live.filter_swaps(live.load_champ_data('mundo', CACHE)['swaps'],
      [{'championName': 'Garen'}], 0, 1)))

# 10. Tier-3 boots owned (Armored Advance), coach lists Steelcaps + Swiftness variants.
m3 = P('Dr. Mundo', 3, 3, 4, ['Heartsteel', "Warmog's Armor", 'Armored Advance'], 'TOP', 'ORDER')
run('10 tier-3 boots owned', 'mundo', m3, g_en, 1900,
    ['Heartsteel', 'Plated Steelcaps', 'Unending Despair', 'Spirit Visage', "Randuin's Omen", 'Thornmail'])
# 11. Kog'Maw player vs fed crit Yasuo: Randuin's per kogmaw/build.md
kog = P("Kog'Maw", 2, 2, 2, ["Guinsoo's Rageblade"], 'BOTTOM', 'ORDER')
run('11 Kog player vs fed Yasuo', 'kogmaw', kog, y_en, 1300)

# 12. Kayn game, 32m: Kayn 13/4 (1.48x) should be SNOWBALLING, Viego 8/6 (1.21x) only AHEAD,
#     so Randuin's stays gated and the coach's Randuin's-over-Thornmail can't happen.
k_en = [P('Kayn', 13, 4, 9, ['Spear of Shojin', 'Eclipse', "Mercury's Treads", 'Black Cleaver', "Death's Dance", 'The Brutalizer'], 'TOP', lvl=19),
        P('Viego', 8, 6, 5, ['Immortal Shieldbow', 'Hubris', 'Plated Steelcaps', 'The Collector', 'B. F. Sword', 'Pickaxe'], 'JUNGLE', lvl=17),
        P('Syndra', 2, 10, 6, ["Doran's Ring", "Luden's Echo", 'Cull', "Spellslinger's Shoes", 'Stormsurge', 'Hextech Alternator'], 'MIDDLE'),
        P('Ezreal', 1, 6, 4, ['Kraken Slayer', 'Essence Reaver', 'Long Sword', 'Fiendish Codex', 'Dagger'], 'BOTTOM'),
        P('Soraka', 2, 8, 7, ['Dream Maker', 'Moonstone Renewer', 'Ionian Boots of Lucidity', 'Redemption', 'Forbidden Idol', 'Forbidden Idol'], 'UTILITY')]
mk = P('Dr. Mundo', 5, 8, 3, ['Heartsteel', "Warmog's Armor", 'Boots of Swiftness', "Dead Man's Plate", 'Long Sword'], 'TOP', 'ORDER', lvl=16)
print('\n--- states 12:', {e['championName']: live._enemy_threat_state(e, 1950, k_en, IDX) for e in k_en})
run('12 Kayn game: coach tries Randuin over Thornmail', 'mundo', mk, k_en, 1950,
    ['Heartsteel', "Warmog's Armor", 'Boots of Swiftness', "Dead Man's Plate", 'Titanic Hydra', "Randuin's Omen"])

# 13. Warwick game, 20m: ASol 7/4 (1.15x) -> AHEAD, not SNOWBALLING.
w_en = [P('Jax', 3, 7, 1, ["Doran's Blade", 'Trinity Force', 'Plated Steelcaps', 'Haunting Guise', 'Amplifying Tome', 'Glowing Mote'], 'TOP'),
        P('Warwick', 3, 8, 7, ['Gustwalker Hatchling', 'Stridebreaker', "Berserker's Greaves", 'Recurve Bow', 'Vampiric Scepter'], 'JUNGLE'),
        P('Sylas', 2, 6, 2, ['Hextech Rocketbelt', "Doran's Ring", "Spellslinger's Shoes", 'Control Ward', 'Hextech Alternator'], 'MIDDLE'),
        P('Aurelion Sol', 7, 4, 2, ["Liandry's Torment", "Rylai's Crystal Scepter", 'Dark Seal', 'Fiendish Codex'], 'BOTTOM'),
        P('Braum', 0, 6, 8, ['Celestial Opposition', 'Locket of the Iron Solari', 'Bramble Vest', 'Boots', 'Cloth Armor'], 'UTILITY')]
print('\n--- states 13:', {e['championName']: live._enemy_threat_state(e, 1210, w_en, IDX) for e in w_en})

# 14. Early stomp, 11m: Zed 6/0 who already bought -> SNOWBALLING; same Zed sitting on unspent gold -> AHEAD.
z_items = ["Youmuu's Ghostblade", 'Serrated Dirk', 'Boots']
base = [P('Ornn', 0, 2, 0, ["Doran's Shield", 'Bramble Vest'], 'TOP'), P('Sejuani', 0, 2, 1, ['Gustwalker Hatchling', 'Boots'], 'JUNGLE'),
        P('Jinx', 1, 1, 0, ["Doran's Blade", 'Noonquiver'], 'BOTTOM'), P('Lulu', 0, 1, 2, ['World Atlas', 'Boots'], 'UTILITY')]
z1 = P('Zed', 6, 0, 1, z_items, 'MIDDLE'); z2 = P('Zed', 6, 0, 1, ["Doran's Blade"], 'MIDDLE')
print('\n--- 14 early Zed bought:', live._enemy_threat_state(z1, 660, base + [z1], IDX),
      '| unspent:', live._enemy_threat_state(z2, 660, base + [z2], IDX))
# 15. Garen still SNOWBALLING with the gold check (1.61x).
print('--- 15 Garen:', live._enemy_threat_state(garen, 1740, g_en, IDX), '| no index (old rule):', live._enemy_threat_state(garen, 1740))
