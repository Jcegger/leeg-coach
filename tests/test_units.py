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
    assert live.compute_phase(700, pl, {'towers': [(655.0, 'Jinx', 'Turret_T2_R_01_A')]}) == 'mid'
    assert live.first_turret({'towers': [(690, 'x', 'Turret_T1_L_01_A'), (655, 'y', 'Turret_T2_R_01_A')]}) == (655, 'bot outer')


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
