"""Unit tests for leeg's rule logic. Plain asserts; run with `python tests/run.py`
(or pytest if you have it). Needs network once for CommunityDragon item/champion data."""
import _setup  # noqa: F401
import live

IX = live.fetch_item_index()
N2I = live._build_name_to_id(IX)
CI, _ = live.fetch_champion_index()
live.set_web_champ_index(CI)
live.set_removed_items(IX)


def item(n):
    return {'itemID': live.resolve_item_id(n, N2I), 'displayName': n}


def me_with(*names):
    return {'items': [item(n) for n in names]}


def player(name, pos, team, cs=0, lvl=5, **kw):
    return dict({'championName': name, 'riotIdGameName': name, 'position': pos, 'team': team, 'level': lvl,
                 'scores': {'creepScore': cs}}, **kw)


# ---- buying: owned parts count ----

BUILD = ['Heartsteel', 'Plated Steelcaps', "Randuin's Omen", "Warmog's Armor"]


def test_remaining_cost_credits_nested_parts():
    hs = live.resolve_item_id('Heartsteel', N2I)
    inv = live.owned_item_counter(me_with("Giant's Belt", 'Ruby Crystal')['items'])
    rem, have = live.item_remaining_cost(hs, inv.copy(), IX)
    assert rem == IX[hs]['cost'] - 900 - 400, rem   # Belt direct, Ruby inside the Bracer
    assert "Giant's Belt" in have and 'Ruby Crystal' in have


def test_duplicate_parts_count_twice():
    w = live.resolve_item_id("Warmog's Armor", N2I)
    one = live.item_remaining_cost(w, live.owned_item_counter(me_with("Giant's Belt")['items']), IX)[0]
    two = live.item_remaining_cost(w, live.owned_item_counter(me_with("Giant's Belt", "Giant's Belt")['items']), IX)[0]
    assert one - two == 900, (one, two)


def test_hint_finishes_with_parts():
    h = live._next_buy_hint(me_with("Giant's Belt"), None, IX, 2100, committed_items=BUILD)
    assert h.startswith('BACK') and 'finishes Heartsteel' in h, h


def test_hint_no_recall_for_a_ruby():
    h = live._next_buy_hint(me_with(), None, IX, 400, committed_items=BUILD)
    assert h.startswith('next:'), h


def test_hint_names_the_part():
    h = live._next_buy_hint(me_with(), None, IX, 1650, committed_items=["Randuin's Omen"])
    assert h.startswith('BACK') and "Warden's Mail" in h, h


def test_affordability_credits_parts():
    owned = me_with("Warden's Mail")['items']
    assert live.affordability_postcheck(["BACK and finish Randuin's Omen now"], 1700, IX, owned)[0].startswith('BACK')
    assert live.affordability_postcheck(["BACK and finish Randuin's Omen now"], 1700, IX, [])[0].startswith('FARM')


def test_tiles_credit_parts_to_earliest_item():
    inv = live.owned_item_counter(me_with("Giant's Belt")['items'])
    tiles = live._web_items(BUILD, set(), IX, inv)
    assert tiles[0]['cost'] == tiles[0]['full'] - 900       # Heartsteel gets the Belt
    assert tiles[3]['cost'] == tiles[3]['full']             # Warmog's doesn't get it again


# ---- validators ----

def test_rejected_item_short_name_dropped():
    prop = ['Heartsteel', 'Boots of Swiftness', "Randuin's Omen", 'Unending Despair', 'Spirit Visage', 'Thornmail']
    fin = ['Heartsteel', 'Boots of Swiftness', "Jak'Sho, The Protean", 'Unending Despair', 'Spirit Visage', 'Thornmail']
    out = live.drop_rejected_item_bullets(["Rush Randuin's 3rd.", 'Farm toward Heartsteel.'], prop, fin, [], IX)
    assert out == ['Farm toward Heartsteel.'], out


def test_glance_length_and_affordability():
    assert live.clean_glance('one two three four five six seven eight nine ten eleven twelve thirteen', 0, IX, []) == ''
    assert live.clean_glance("BACK and finish Randuin's Omen", 500, IX, []).startswith('FARM')


def test_stale_note_detection():
    assert live.note_is_stale_text('- item: Adaptive Helm vs magic')
    assert live.note_is_stale_text('- item: Kaenic Lifegem')
    assert not live.note_is_stale_text('- item: Kaenic Rookern')
    assert not live.note_is_stale_text("Kayle's Divine Judgment makes her immune")
    assert not live.note_is_stale_text('Black Cleaver shreds armor')


# ---- game reads ----

def test_laning_ends_at_first_turret():
    pl = [{'level': 8}] * 10
    assert live.compute_phase(700, pl, {'towers': []}) == 'early'
    assert live.compute_phase(700, pl, {'towers': [(655.0, 'Jinx', 'Turret_TChaos_L0_P3_511845594')]}) == 'mid'
    assert live.first_turret({'towers': [(690, 'x', 'Turret_TOrder_L2_P3_1'), (655, 'y', 'Turret_TChaos_L0_P3_2')]}) == (655, 'bot outer')
    assert live.first_turret({'towers': [(655, 'y', 'Turret_T2_R_01_A')]}) == (655, 'bot outer')   # older format


ME = player('Dr. Mundo', 'TOP', 'ORDER', 60, 6)
PLAYERS = [ME, player('Garen', 'TOP', 'CHAOS', 80, 7), player('Lee Sin', 'JUNGLE', 'CHAOS'),
           player('Jinx', 'BOTTOM', 'ORDER'), player('Zoe', 'MIDDLE', 'CHAOS'), player('Lux', 'MIDDLE', 'ORDER')]


def test_lane_gap():
    assert live.lane_gap({'allPlayers': PLAYERS}, ME) == {'vs': 'Garen', 'cs': -20, 'lvl': -1}


def test_jungler_last_seen():
    data = {'allPlayers': PLAYERS}
    assert live.enemy_jungler_seen(data, {'kills': []}, ME)['at'] is None
    ev = {'kills': [(200, 'Lee Sin', 'Jinx', []), (300, 'Zoe', 'Lux', ['Lee Sin'])], 'drakes': [(250, 'Fire', 'Lee Sin', False)]}
    j = live.enemy_jungler_seen(data, ev, ME)
    assert (j['where'], j['at']) == ('mid', 300), j


def test_killed_by():
    me = {'team': 'ORDER', 'riotIdGameName': 'you'}
    data = {'allPlayers': [me, {'team': 'CHAOS', 'riotIdGameName': 'x', 'championName': 'Garen'}]}
    assert live.killed_by(data, {'kills': [(100, 'x', 'you', [])]}, me) == 'Garen'
    assert live.killed_by(data, {'kills': [(100, 'x', 'you', []), (500, 'Turret_T2_L_01_A', 'you', [])]}, me) is None


def test_tracker_death_kinds_and_time_dead():
    t = live.GameTracker()

    def upd(gt, evs, dead=False, d=0):
        m = dict(ME, isDead=dead, scores={'creepScore': 60, 'deaths': d})
        t.update({'allPlayers': [m] + PLAYERS[1:], 'events': {'Events': evs}}, m, 'Dr. Mundo', gt, {}, 'mundo')

    def kill(i, tm, k, a):
        return {'EventID': i, 'EventName': 'ChampionKill', 'EventTime': tm, 'KillerName': k,
                'VictimName': 'Dr. Mundo', 'Assisters': a}
    upd(100, [])
    upd(200, [kill(1, 199, 'Garen', ['Lee Sin'])], True, 1)
    upd(230, [], False, 1)
    upd(500, [kill(2, 499, 'Garen', [])], True, 2)
    upd(520, [], False, 2)
    upd(700, [kill(3, 699, 'Zoe', [])], True, 3)
    upd(720, [], False, 3)
    kinds = [d['kind'] for d in sorted(t.g['death_info'].values(), key=lambda d: d['t'])]
    assert kinds == ['gank', 'solo', 'roam'], kinds
    assert t.g['dead_s'] == 70, t.g['dead_s']


def test_career_stats_and_csv(tmp=None):
    import csv
    import tempfile
    from pathlib import Path
    g = {'date': '2026-11-01 20:00', 'champ': 'Dr. Mundo', 'vs': 'Garen', 'result': 'Win', 'duration': 1800,
         'k': 3, 'd': 2, 'a': 5, 'cs': 200, 'cs_min': 6.7, 'cs_marks': {'10': 60}, 'deaths': [200, 1300],
         'items': [{'name': "Warmog's Armor", 't': 840, 'boots': False}], 'item_bench': [13.5],
         'lane_marks': {'10': {'cs': -10, 'lvl': 0}}, 'dead_s': 50,
         'death_info': [{'t': 200, 'by': 'Lee Sin', 'help': [], 'kind': 'gank'}]}
    st = live.career_stats([g, dict(g, result='Loss')])
    assert (st['n'], st['wins'], st['losses']) == (2, 1, 1)
    assert st['nemesis'][0]['name'] == 'Lee Sin' and st['nemesis'][0]['kills'] == 2
    assert st['comebacks'] == 1 and st['death_buckets'][1] == 2
    live.GAMES_LOG.write_text('\n'.join(__import__('json').dumps(x) for x in (g, g)) + '\n')
    out = Path(tempfile.mkdtemp()) / 'g.csv'
    assert live.export_games_csv(out) == 2
    rows = list(csv.DictReader(out.open()))
    assert rows[0]['first_item'] == "Warmog's Armor" and rows[0]['ganked'] == '1'


def test_new_enemy_items_only_after_first_look():
    live._enemy_item_seen.clear()
    live._enemy_item_clock[0] = 0
    garen = player('Garen', 'TOP', 'CHAOS', items=[item('Infinity Edge')], scores={})
    lee = player('Lee Sin', 'JUNGLE', 'CHAOS', items=[item('Bastionbreaker')], scores={})
    first = live.web_enemy_cards('Dr. Mundo', ME, [garen, lee], 1500, IX)
    assert not any(i.get('new_at') for c in first for i in c['items'])      # owned when leeg looked: old
    garen['items'].append(item('Mortal Reminder'))
    later = {c['name']: c for c in live.web_enemy_cards('Dr. Mundo', ME, [garen, lee], 1510, IX)}
    assert [i['name'] for i in later['Garen']['items'] if 'new_at' in i] == ['Mortal Reminder']
    gone = {c['name']: c for c in live.web_enemy_cards('Dr. Mundo', ME, [garen, lee], 1700, IX)}
    assert not any('new_at' in i for i in gone['Garen']['items'])              # highlight lasts 90s


def test_next_drake_element():
    data = {'allPlayers': [{'team': 'ORDER', 'riotIdGameName': 'a'}, {'team': 'CHAOS', 'riotIdGameName': 'b'}]}
    two = {'drakes': [(400, 'Fire', 'a', False), (800, 'Water', 'b', False)]}
    assert live.next_drake(data, two, 'ORDER') is None                         # third type not on the feed yet
    three = {'drakes': two['drakes'] + [(1100, 'Air', 'a', False)]}
    assert live.next_drake(data, three, 'ORDER') == 'Cloud'
    soul = {'drakes': [(t, 'Fire', 'a', False) for t in (400, 800, 1100, 1400)]}
    assert live.next_drake(data, soul, 'ORDER') == 'Elder'


def test_champion_folders_parse():
    """Every champion folder's build.md has paths, its items exist in the game, and its
    playbook has the sections the coach reads."""
    for folder in live.available_champs():
        cd = live.load_champ_data(folder, {})
        assert cd['build_variants'], f'{folder}: no build paths'
        missing = [n for n in cd['item_pool'] if not live.resolve_item_id(n, N2I)]
        assert not missing, f'{folder}: unknown items {missing}'
        for new, old, _cond in cd['swaps']:
            assert live.resolve_item_id(new, N2I) and live.resolve_item_id(old, N2I), f'{folder}: swap {new} over {old}'
        assert live.pick_build_variant(cd['build_variants'], 'Standard'), f'{folder}: no default path'
        if (live.LEEG_ROOT / folder / 'playbook.md').exists():
            assert cd['win_conditions'], f'{folder}: playbook has no "## Win conditions"'


def _played_game(tracker, minutes=20, result=None):
    m = dict(ME, scores={'creepScore': 120, 'deaths': 0})
    evs = [{'EventName': 'GameEnd', 'Result': result}] if result else []
    for t in range(0, minutes * 60 + 1, 60):
        tracker.update({'allPlayers': [m] + PLAYERS[1:], 'events': {'Events': evs if t == minutes * 60 else []}},
                       m, 'Dr. Mundo', t, {}, 'mundo')


def test_game_saved_after_idle_grace():
    """Regression: the idle branch once lost its finish() call and no game was ever saved."""
    import time as _t
    live.GAMES_LOG.write_text('')
    tr = live.GameTracker()
    _played_game(tr)
    assert not tr.end_if_gone()                      # API just went away: within the grace
    tr.last_seen = _t.time() - live.IDLE_GRACE_S - 1
    assert tr.end_if_gone() and tr.g is None
    assert len(live.load_all_games()) == 1


def test_restart_saves_a_finished_game_only():
    live.GAMES_LOG.write_text('')
    tr = live.GameTracker()
    _played_game(tr)                                 # still running: the next leeg picks it up
    tr.save_on_exit()
    assert tr.g is not None and live.load_all_games() == []
    m = dict(ME, scores={'creepScore': 130, 'deaths': 0})   # same game ends: GameEnd on the feed
    tr.update({'allPlayers': [m] + PLAYERS[1:], 'events': {'Events': [{'EventName': 'GameEnd', 'Result': 'Win'}]}},
              m, 'Dr. Mundo', 21 * 60, {}, 'mundo')
    tr.save_on_exit()                                # --restart right after the game must not lose it
    assert [g['result'] for g in live.load_all_games()] == ['Win']


def test_result_inferred_from_nexus_turrets():
    """GameEnd missed (API closed first): both enemy nexus turrets just fell, so a Win; a
    surrender (no nexus kills) stays None."""
    live.GAMES_LOG.write_text('')
    for theirs, want in (('Chaos', 'Win'), ('Order', 'Loss'), (None, None)):
        tr = live.GameTracker()
        _played_game(tr)
        if theirs:
            m = dict(ME, scores={'creepScore': 130, 'deaths': 0})
            evs = [{'EventID': 90 + i, 'EventName': 'TurretKilled', 'EventTime': 20 * 60 + i * 3,
                    'TurretKilled': f'Turret_T{theirs}_L1_P{4 + i}_1', 'KillerName': 'x'} for i in range(2)]
            tr.update({'allPlayers': [m] + PLAYERS[1:], 'events': {'Events': evs}}, m, 'Dr. Mundo', 20 * 60 + 10, {}, 'mundo')
        tr.finish()
        assert live.load_all_games()[-1]['result'] == want, (theirs, want)
    tr = live.GameTracker()                          # nexus turrets fell long before the end: no guess
    _played_game(tr)
    tr.g['nexus_kills'] = {'a': ('CHAOS', 100), 'b': ('CHAOS', 103)}
    tr.finish()
    assert live.load_all_games()[-1]['result'] is None


GOOD = {'date': '2026-11-10 10:17', 'cs_min': 7.4, 'deaths': [1300], 'k': 14, 'd': 2, 'a': 9, 'duration': 2500,
        'items': [{'name': 'Yun Tal Wildarrows', 't': 780, 'boots': False}], 'item_bench': [13.5],
        'lane_marks': {'10': {'cs': 12}}, 'result': 'Win', 'vs': 'Garen', 'champ': 'Sivir'}


def test_grade():
    assert live.grade_game(GOOD)['letter'] == 'S'
    bad = dict(GOOD, cs_min=4.8, deaths=[200, 400, 700], d=6, k=1, lane_marks={})
    g = live.grade_game(bad)
    assert g['letter'] == 'D' and 'deaths before 15' in g['line']
    assert sum(g['parts'].values()) == g['score']


def test_milestones_and_rivals():
    loss = dict(GOOD, result='Loss', date='2026-11-09 20:00', death_info=[{'t': 1300, 'by': 'Garen', 'kind': 'solo'}])
    ms = {m['id']: m for m in live.milestones([loss, GOOD])}
    assert 'got' in ms['revenge'] and ms['revenge']['got']['date'] == GOOD['date']   # beat Garen after losing to him
    assert 'got' in ms['grade_s'] and 'got' not in ms['ten_games']
    rv = live.rivalries([loss, GOOD])
    assert rv[0]['name'] == 'Garen' and (rv[0]['w'], rv[0]['l'], rv[0]['kills']) == (1, 1, 1)


def test_game_moment():
    me = {'team': 'ORDER', 'riotIdGameName': 'you'}
    data = {'allPlayers': [me, {'team': 'ORDER', 'riotIdGameName': 'ally'}]}
    ev = {'multi': [(1506, 'you', 3)], 'aces': [], 'barons': [], 'drakes': []}
    assert live.game_moment(data, ev, me, 1510) == {'text': 'TRIPLE KILL', 'at': 1506}
    assert live.game_moment(data, ev, me, 1520) is None                      # 8s window
    ev = {'multi': [(1506, 'someone', 3)], 'aces': [(1600, 'ally', 'CHAOS')], 'barons': [(1700, 'ally', False)], 'drakes': []}
    assert live.game_moment(data, ev, me, 1601) is None                      # their ace, someone else's multi
    assert live.game_moment(data, ev, me, 1702)['text'] == 'BARON'


# ---- build stability: fed latch, undo guard, committed order ----

def _trynd(k, d):
    return {'championName': 'Tryndamere', 'position': 'TOP', 'scores': {'kills': k, 'deaths': d, 'assists': 0},
            'items': [item('Infinity Edge'), item("Berserker's Greaves")]}


def test_fed_latch_holds_until_not_ahead():
    latch = set()
    live.update_fed_latch(latch, [_trynd(4, 0)], 900)
    assert latch == {'tryndamere'}
    t = _trynd(4, 2)                                    # diff 2: raw AHEAD, latch keeps SNOWBALLING
    live.update_fed_latch(latch, [t], 1000)
    assert live._enemy_threat_state(t, 1000) == 'AHEAD'
    assert live._enemy_threat_state(t, 1000, latch=latch) == 'SNOWBALLING'
    sw = [('Plated Steelcaps', 'Boots of Swiftness', 'fed auto-attacker')]
    assert live.filter_swaps(sw, [t], 0, 1, 'TOP', 1000, None, latch) == sw
    assert live.filter_swaps(sw, [t], 0, 1, 'TOP', 1000, None) == []
    live.update_fed_latch(latch, [_trynd(4, 4)], 1200)  # even: released
    assert latch == set()


CB = ['Heartsteel', 'Plated Steelcaps', 'Unending Despair', 'Thornmail', 'Titanic Hydra', "Dead Man's Plate"]
FLIP = CB[:5] + ['Spirit Visage']


def test_undo_guard_reverts_recent_readd():
    dropped = {live.normalize('Spirit Visage'): 1400}
    assert live.guard_swap_undo(FLIP, CB, dropped, 1500, [], [], IX) == CB
    assert live.guard_swap_undo(FLIP, CB, dropped, 1400 + live.UNDO_HOLD, [], [], IX) == FLIP   # hold over
    assert live.guard_swap_undo(FLIP, CB, dropped, 1500, ["Dead Man's Plate"], [], IX) == FLIP  # DMP now gated
    assert live.guard_swap_undo(FLIP, CB, dropped, 1500, [], [item('Spirit Visage')], IX) == FLIP  # owned
    assert live.guard_swap_undo(FLIP, CB, {}, 1500, [], [], IX) == FLIP


def test_keep_committed_order():
    re = [CB[1], CB[0]] + CB[2:]
    assert live.keep_committed_order(re, CB, [], IX) == CB
    owned = [item('Heartsteel'), item('Plated Steelcaps')]
    assert live.keep_committed_order([CB[1], CB[0]] + FLIP[2:], CB, owned, IX) == FLIP


# ---- structures: turret and inhib IDs ----

def test_parse_structure_both_formats():
    P = live.parse_structure
    assert P('Turret_TChaos_L0_P3_511845594') == ('CHAOS', 'bot', 'outer')
    assert P('Turret_TOrder_L2_P2_1526764315') == ('ORDER', 'top', 'inner')
    assert P('Turret_TOrder_L1_P1_1209122387') == ('ORDER', 'mid', 'inhib')
    assert P('Turret_TOrder_L1_P5_3625540808') == ('ORDER', 'mid', 'nexus')
    assert P('Inhib_TChaos_L2_P1_2351107073') == ('CHAOS', 'top', 'inhibitor')
    assert P('Turret_T1_C_05_A') == ('ORDER', 'mid', 'nexus')
    assert P('Barracks_T2_L1') == ('CHAOS', 'top', 'inhibitor')
    assert P('Minion_T100L0S01N0004') is None and P('') is None
    assert live.fmt_turret('Inhib_TChaos_L2_P1_1') == 'top inhibitor'
    assert live._struct_side('Turret_TChaos_L0_P3_1', 'ORDER') == 'enemy '


def _coach_for_events():
    c = live.Coach()
    c.client, c.last_response, c.last_call, c.cooldown_seconds = object(), 'x', 0, 0
    return c


def _trigger(event):
    c = _coach_for_events()
    me = {'team': 'ORDER', 'riotIdGameName': 'you', 'championName': 'Dr. Mundo'}
    enemies = [{'riotIdGameName': 'foe', 'team': 'CHAOS', 'championName': 'Garen'}]
    return c.maybe_trigger({'raw': [event]}, me, enemies, {}, 1200, 'mundo')


def test_structure_triggers_pick_the_right_side():
    assert _trigger({'EventName': 'TurretKilled', 'TurretKilled': 'Turret_TChaos_L0_P3_1', 'KillerName': 'x'}) == 'tower_taken'
    assert _trigger({'EventName': 'TurretKilled', 'TurretKilled': 'Turret_TOrder_L0_P3_1', 'KillerName': 'foe'}) != 'tower_taken'
    # A minion kills our inhib: still ours lost, not "push their nexus"
    assert _trigger({'EventName': 'InhibKilled', 'InhibKilled': 'Inhib_TOrder_L1_P1_1', 'KillerName': 'Minion_T200L1S20N0001'}) == 'inhib_lost'
    assert _trigger({'EventName': 'InhibKilled', 'InhibKilled': 'Inhib_TChaos_L1_P1_1', 'KillerName': 'Minion_T100L1S20N0001'}) == 'inhibkilled'


def test_base_line_counts_per_side():
    ev = {'towers': [(1, 'x', 'Turret_TOrder_L1_P4_1'), (2, 'x', 'Turret_TChaos_L0_P3_2')],
          'inhibs': [(3, 'x', 'Inhib_TOrder_L1_P1_1'), (4, 'x', 'Inhib_TOrder_L2_P1_2'), (5, 'y', 'Inhib_TChaos_L0_P1_3')]}
    out = live._tower_state_summary(ev, 'ORDER')
    assert 'enemy fallen=[bot outer]' in out
    assert 'BASE: your inhibitors down 2/3 · your nexus turrets down 1/2 · enemy inhibitors down 1/3' in out
