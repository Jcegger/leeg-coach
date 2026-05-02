"""Bulk-generate ChampDB notes for all champions. Run once to pre-populate the cache."""
import json, os, sys, time, threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))
from live import fetch_champion_index, ChampDB, DATA_DIR

api_key = os.environ.get('LEEG_ANTHROPIC_API_KEY') or os.environ.get('ANTHROPIC_API_KEY')
if not api_key:
    sys.exit('No API key found')

import anthropic
client = anthropic.Anthropic(api_key=api_key)

champ_index, _ = fetch_champion_index()
if not champ_index:
    sys.exit('Failed to fetch champion index')

names = sorted(set(champ_index.values()))
print(f'{len(names)} champions to generate')

import urllib.request, ssl
def get_patch():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen('https://ddragon.leagueoflegends.com/api/versions.json', context=ctx) as r:
        return json.loads(r.read())[0].rsplit('.', 1)[0]

patch = get_patch()
print(f'Patch: {patch}')

db = ChampDB(client=client, patch=patch)
db.client = client

lock = threading.Lock()
done = [0]

def generate_one(name):
    from live import normalize
    key = normalize(name)
    with db._lock:
        entry = db._notes.get(key)
        if entry:
            return name, 'skip'
    try:
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=200,
            messages=[{
                'role': 'user',
                'content': (
                    f'League of Legends: 3-4 short imperative sentences (max 80 words total) on how to play against {name}. '
                    f'Cover: their core threat, when they spike, a key exploit window, and one item or build tip. '
                    f'No headers, no preamble, no markdown. '
                    f'No emojis, imperative tone.'
                ),
            }],
        )
        notes = resp.content[0].text.strip()
        with db._lock:
            db._notes[key] = {
                'display_name': name,
                'patch': patch,
                'generated_at': time.strftime('%Y-%m-%d'),
                'notes': notes,
            }
            db._save()
        return name, 'ok'
    except Exception as e:
        return name, f'error: {e}'

with ThreadPoolExecutor(max_workers=5) as ex:
    futures = {ex.submit(generate_one, n): n for n in names}
    for f in as_completed(futures):
        name, status = f.result()
        done[0] += 1
        print(f'[{done[0]}/{len(names)}] {name}: {status}')

print('Done.')
