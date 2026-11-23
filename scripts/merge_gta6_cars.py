#!/usr/bin/env python3
"""Merge shared new_cars.json files into car_library.json (and optionally rebuild index.html).

usage:  python3 merge_cars.py car_library.json new_cars1.json [new_cars2.json ...]
        python3 merge_cars.py car_library.json new_cars*.json --rebuild page_template.html gta6-cars.js index.html

A car is added only if its model (garage or parked-outside copy) isn't in the library yet and its data
parses. Fix a wrong name/class by editing that car's "name"/"class" in car_library.json afterwards.
"""
import json, sys

def ok(c):
    try:
        json.loads(c['entry']); [json.loads(v) for v in c['row'].values()]
        json.loads(c['source']['id2']); json.loads(c['source']['id3'])
        return isinstance(c['model'], int) and isinstance(c['street'], bool)
    except Exception:
        return False

args = sys.argv[1:]
rebuild = None
if '--rebuild' in args:
    i = args.index('--rebuild'); rebuild = args[i + 1:i + 4]; args = args[:i]
lib_path, files = args[0], args[1:]
lib = json.load(open(lib_path, encoding='utf-8'))
have = {f"{c['model'] & 0xffffffff}{'s' if c['street'] else 'g'}" for c in lib['cars']}
added = 0
for f in files:
    data = json.load(open(f, encoding='utf-8'))
    for c in data.get('cars', []):
        key = f"{c.get('model', 0) & 0xffffffff}{'s' if c.get('street') else 'g'}"
        if key in have or not ok(c):
            continue
        c['key'] = key; lib['cars'].append(c); have.add(key); added += 1
        print('  added', c.get('name'), '(outside)' if c['street'] else '(garage)')
lib['cars'].sort(key=lambda c: (c['name'].startswith('Unidentified'), c['name'], c['street']))
json.dump(lib, open(lib_path, 'w', encoding='utf-8'), separators=(',', ':'), ensure_ascii=False)
print(f'{added} new car(s); library now has {len(lib["cars"])}')
if rebuild:
    tpl, core, out = rebuild
    t = open(tpl, encoding='utf-8').read(); s = open(core, encoding='utf-8').read()
    L = json.dumps(lib, separators=(',', ':'), ensure_ascii=False).replace('</', '<\\/')
    open(out, 'w', encoding='utf-8').write(t.replace('/*CORE*/', s.replace('</', '<\\/')).replace('/*LIBRARY*/', L))
    print('rebuilt', out)
