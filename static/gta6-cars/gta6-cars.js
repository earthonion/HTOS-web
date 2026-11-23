/*!
 * GTA VI Car Giver — core library (browser + Node). v1.0.0
 *
 * Adds cars to a DECRYPTED GTA VI PS5 save (JSAV format,
 * the SARC000x file inside sdimg_SAVEDATASARC000x after GarlicSaves "decrypt"), into an
 * empty garage the player already owns, or replaces the car in one of their garages.
 *
 * It does NOT change cars the player already owns (paint, upgrades, plates, tuning),
 * does NOT give garages/properties, and only offers cars that exist in the car library.
 *
 * Usage:
 *   const save = GTA6Cars.open(uint8Array, library);   // throws on unsupported files
 *   save.garages()            -> [{hash, name, car|null}]
 *   save.streetCars()         -> [{...car}]
 *   save.availableCars(slot)  -> library cars allowed in that slot
 *   save.addCar(libKey, garageHash|null)   // null = street / "My Vehicles"
 *   save.replaceCar(garageHash, libKey)
 *   save.toBytes()            -> Uint8Array (validated)
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.GTA6Cars = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const VERSION = '1.4.0';
  const ITEM_LIMIT = 4095, RESERVED_ITEMS = 100;
  const PROPERTY_ID3 = 3919519363n;
  const TABLES = ['$0xfc8f2620', '$0x6ebbccb1', '$0xc500ddf4', '$0x9098d12e', '$0x1085013f'];
  const C = { id1: '$0xb4bfeab1', parent: '$0xc5730137', id2: '$0xbe8e255c', id3: '$0x12ae4287',
              value: '$0xe3fead7a', carId: '$0xd65b1f97', garage: '$0xb4ca9f4d' };
  const NO_GARAGE_CLASSES = new Set(['Boats', 'Planes', 'Helicopters', 'Military', 'Industrial', 'Trains', 'Trailers', 'Unknown']);
  const GARAGE_NAMES = { '2783060692': 'Caledonia Boulevard Garage', '3507684088': '7th Avenue Garage' };
  const GARAGE_GROUP_ID2 = 3134384386n;

  // ---------- lossless JSON (keeps every number/string token exactly as written) ----------
  class Raw { constructor(r) { this.r = r; } }
  const isRaw = v => v instanceof Raw;
  function parseJSON(text) {
    let i = 0;
    const ws = () => { while (i < text.length && ' \t\n\r'.includes(text[i])) i++; };
    function str() {
      const s = i; i++;
      while (text[i] !== '"') { if (text[i] === '\\') i++; i++; if (i >= text.length) throw new Error('bad string'); }
      i++; return text.slice(s, i);
    }
    function val() {
      ws(); const ch = text[i];
      if (ch === '{') {
        i++; const o = {}; ws();
        if (text[i] === '}') { i++; return o; }
        for (;;) {
          ws(); const k = JSON.parse(str()); ws();
          if (text[i++] !== ':') throw new Error('expected :');
          o[k] = val(); ws();
          const c = text[i++]; if (c === '}') return o; if (c !== ',') throw new Error('expected , or }');
        }
      }
      if (ch === '[') {
        i++; const a = []; ws();
        if (text[i] === ']') { i++; return a; }
        for (;;) {
          a.push(val()); ws();
          const c = text[i++]; if (c === ']') return a; if (c !== ',') throw new Error('expected , or ]');
        }
      }
      if (ch === '"') return new Raw(str());
      const s = i;
      while (i < text.length && !',]} \t\n\r'.includes(text[i])) i++;
      if (s === i) throw new Error('unexpected character at ' + i);
      return new Raw(text.slice(s, i));
    }
    const v = val(); ws();
    if (i !== text.length) throw new Error('trailing data');
    return v;
  }
  function dump(v) {
    if (isRaw(v)) return v.r;
    if (Array.isArray(v)) return '[' + v.map(dump).join(',') + ']';
    return '{' + Object.keys(v).map(k => JSON.stringify(k) + ':' + dump(v[k])).join(',') + '}';
  }
  const big = v => BigInt(v.r);
  const mk = n => new Raw(String(n));
  const arr = x => (x && !Array.isArray(x) && !isRaw(x) && '$data' in x) ? x.$data : x;
  const u32 = n => BigInt.asUintN(32, BigInt(n));
  const s32 = n => BigInt.asIntN(32, BigInt(n));
  const u64 = n => BigInt.asUintN(64, BigInt(n));


  // ---------- vehicle names (joaat of likely internal names from a public vehicle list) ----------
  function joaat(str) {
    let h = 0;
    for (const ch of str.toLowerCase()) { h = (h + ch.charCodeAt(0)) >>> 0; h = (h + (h << 10)) >>> 0; h = (h ^ (h >>> 6)) >>> 0; }
    h = (h + (h << 3)) >>> 0; h = (h ^ (h >>> 11)) >>> 0; h = (h + (h << 15)) >>> 0; return h;
  }
  let nameIndex = null;
  function buildNameIndex(list) {
    const idx = new Map(), SUF = ['', '2', '3', '4', '5', '6', '7', '8', '9', 'retro', 'classic', 'custom', 'xl', 'gt', '_hs'];
    for (const [name, make, cls, slug] of list || []) {
      const base = name.replace(/\(.*?\)/g, '').trim();
      const words = (base.toLowerCase().replace(/'/g, '').match(/[a-z0-9]+/g)) || [];
      const noyr = words.filter(w => !/^\d\d$/.test(w)), yr = words.filter(w => /^\d\d$/.test(w));
      const c = new Set();
      for (const ws of [words, noyr, (slug || '').toLowerCase().split('-'), [make.toLowerCase()].concat(noyr)]) { c.add(ws.join('')); c.add(ws.join('_')); }
      for (const y of yr) { c.add(noyr.join('') + y); c.add(noyr.join('') + '19' + y); c.add(noyr.join('') + '_' + y); }
      for (const w of noyr) c.add(w);
      for (const x of [...c]) for (const sfx of SUF) {
        const h = joaat(x + sfx);
        if (!idx.has(h)) idx.set(h, [(make ? make + ' ' : '') + name, cls]);
      }
    }
    return idx;
  }
  // SaveRift (Apache-2.0) vehicle categories -> the classes used here
  const SR_CLASS = { 'Bikes': 'Motorcycles', 'Watercraft': 'Boats', 'Aircraft': 'Planes', 'Trains': 'Trains', 'Trailers': 'Trailers',
    'Supercars': 'Super', 'Muscle Cars': 'Muscle', 'Off-Road Vehicles': 'Off-Road', 'Military': 'Military', 'Industrial': 'Industrial',
    'Unclassified': 'Unknown' };
  function nameOf(model, library) {
    if (library.names && library.names[model]) return library.names[model];
    if (!nameIndex) nameIndex = buildNameIndex(library.vehicleList);
    const srCat = library.srCategory && library.srCategory[model];
    const srClass = srCat ? (SR_CLASS[srCat] || srCat) : null;
    const hit = nameIndex.get(Number(model));
    if (hit) return srClass && ['Boats', 'Planes', 'Military', 'Trains', 'Trailers', 'Industrial'].includes(srClass) ? [hit[0], srClass] : hit;
    const internal = library.srInternal && library.srInternal[model];
    if (internal) return [internal.charAt(0).toUpperCase() + internal.slice(1), srClass || 'Unknown'];
    return ['Unidentified vehicle #' + model, 'Unknown'];
  }

  // ---------- JSAV container ----------
  function readJSAV(bytes) {
    if (bytes.length < 16) throw new Error('File too small to be a GTA VI save.');
    const magic = String.fromCharCode(...bytes.slice(0, 4));
    if (magic === 'ASAV') throw new Error('This save is in packed (ASAV) form. Decrypt it with GarlicSaves first and use the decrypted SARC file.');
    if (magic !== 'JSAV') throw new Error('Not a decrypted GTA VI save (expected JSAV). Use the SARC000x file from the decrypted sdimg_SAVEDATASARC000x folder.');
    const dv = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const nc = dv.getUint32(8, true), ns = dv.getUint32(12, true), n = nc + ns;
    const recs = [];
    for (let k = 0; k < n; k++) { const b = 16 + 20 * k; recs.push([0, 4, 8, 12, 16].map(o => dv.getUint32(b + o, true))); }
    return recs;
  }
  const dec = new TextDecoder('utf-8', { fatal: true }), enc = new TextEncoder();

  // ---------- Save ----------
  class Save {
    constructor(bytes, library) {
      this.bytes = bytes; this.lib = library; this.recs = readJSAV(bytes);
      this.blocks = {};
      for (const b of [3, 16]) {
        const [, , , off, size] = this.recs[b];
        const text = dec.decode(bytes.subarray(off, off + size));
        const obj = parseJSON(text);
        if (dump(obj) !== text) throw new Error('Save block ' + b + ' did not round-trip; refusing to edit.');
        this.blocks[b] = obj;
      }
      try {
        this.inv = this.blocks[3]['$0x7a47cb05']['$0xd3f59a5e']['$0x789bdd41'][0];
        this.cars = this.blocks[16]['$0x7a47cb05']['$0x7e56d644']['$0x74b31be3'];
        this.reg = arr(this.inv['$0xd558e4da']);
        for (const t of [TABLES[0], TABLES[1]]) if (!this.inv[t]) throw 0;
      } catch (e) { throw new Error('This save does not have the expected inventory/vehicle layout.'); }
      for (const c of library.cars) if (!c.key) c.key = (c.model >>> 0) + (c.street ? 's' : 'g');
      this.libByKey = Object.fromEntries(library.cars.map(c => [c.key, c]));
      this.libByModelId = {};
      for (const c of library.cars) this.libByModelId[String(c.modelId >>> 0)] = c;
      this.knownGarages = new Set(library.garages.map(String));
      this.log = [];
      this.baseline = new Set(this.validate());   // problems the game itself left in the save
    }
    col(t, c) { return arr(this.inv[t][c]); }
    tableLen(t) { return this.inv[t] ? this.col(t, C.value).length : 0; }
    itemsUsed() { return TABLES.reduce((s, t) => s + this.tableLen(t), 0); }

    ownedProperties() {
      const id2 = this.col(TABLES[0], C.id2), id3 = this.col(TABLES[0], C.id3), out = new Set();
      id3.forEach((v, k) => { if (big(v) === PROPERTY_ID3) out.add(String(u32(big(id2[k])))); });
      return out;
    }
    ownedGarages() {   // properties whose parent is the player's garage group
      const t = TABLES[0], id1 = this.col(t, C.id1), id2 = this.col(t, C.id2), id3 = this.col(t, C.id3), par = this.col(t, C.parent);
      const pos = new Map(id1.map((v, k) => [String(big(v)), k])), out = [];
      id3.forEach((v, k) => {
        if (big(v) !== PROPERTY_ID3) return;
        const p = pos.get(String(big(this.reg[Number(big(par[k]))] || mk(0))));
        if (p !== undefined && u32(big(id2[p])) === GARAGE_GROUP_ID2) out.push(String(u32(big(id2[k]))));
      });
      return out;
    }
    carRows() {
      const t = TABLES[1], cid = this.col(t, C.carId), out = [];
      cid.forEach((v, k) => { if (big(v) !== 0n) out.push(k); });
      return out;
    }
    entryFor(carId) { return this.cars.find(c => u32(big(c['$0x1b60404d'])) === u32(carId)); }
    modelOf(entry) {
      const d = arr(entry['$0x08ccb621']).slice(20, 24).map(v => Number(big(v)) & 255);
      return String(((d[3] << 24) >>> 0) + (d[2] << 16) + (d[1] << 8) + d[0]);
    }
    describeRow(k) {
      const t = TABLES[1];
      const mid = String(u32(big(this.col(t, C.id2)[k]))), carId = big(this.col(t, C.carId)[k]);
      const e = this.entryFor(carId), model = e ? this.modelOf(e) : null;
      const nm = model ? nameOf(model, this.lib)[0] : (this.libByModelId[mid] ? this.libByModelId[mid].name : 'Vehicle #' + mid);
      return { row: k, carId, modelId: mid, model, hasData: !!e, name: nm, garage: String(u32(big(this.col(t, C.garage)[k]))) };
    }
    garages() {
      const owned = this.ownedProperties(), occ = {};
      for (const k of this.carRows()) { const d = this.describeRow(k); if (d.garage !== '0') occ[d.garage] = d; }
      let list = this.ownedGarages();
      if (!list.length) list = [...owned].filter(h => this.knownGarages.has(h) || occ[h]);
      for (const h of Object.keys(occ)) if (!list.includes(h) && owned.has(h)) list.push(h);
      list.sort((a, b) => Number(a) - Number(b));
      return list.map((h, i) => ({ hash: h, name: GARAGE_NAMES[h] || ('Garage slot ' + (i + 1)), car: occ[h] || null }));
    }
    streetCars() { return this.carRows().map(k => this.describeRow(k)).filter(d => d.garage === '0' && d.hasData); }
    ownedModelIds() { return new Set(this.carRows().map(k => this.describeRow(k).modelId)); }
    availableCars(garageHash) {
      const owned = this.ownedModelIds(), street = garageHash == null;
      return this.lib.cars.filter(c => c.street === street && !NO_GARAGE_CLASSES.has(c.class) && !owned.has(String(c.modelId >>> 0)))
                          .sort((a, b) => (a.name > b.name ? 1 : -1));
    }


    // Cars in THIS save whose model isn't in the library yet, in library format (for sharing).
    harvest() {
      const t1 = TABLES[0], t2 = TABLES[1], id1 = this.col(t1, C.id1), pos = new Map(id1.map((v, k) => [String(big(v)), k]));
      const have = new Set(this.lib.cars.map(c => (c.model >>> 0) + (c.street ? 's' : 'g')));
      const out = [];
      for (const k of this.carRows()) {
        const d = this.describeRow(k); if (!d.hasData) continue;
        const street = d.garage === '0', key = d.model + (street ? 's' : 'g');
        if (have.has(key)) continue;
        const row = {}; for (const c of Object.keys(this.inv[t2])) if (c !== C.id1 && c !== C.parent) row[c] = dump(arr(this.inv[t2][c])[k]);
        const src = pos.get(String(big(this.reg[Number(big(arr(this.inv[t2][C.parent])[k]))] || mk(-1))));
        if (src === undefined) continue;
        const [name, cls] = nameOf(d.model, this.lib);
        out.push({ key, model: Number(d.model), modelId: Number(d.modelId), street, name, class: cls, parts: 0,
                   entry: dump(this.entryFor(d.carId)), row,
                   source: { id2: dump(this.col(t1, C.id2)[src]), id3: dump(this.col(t1, C.id3)[src]) } });
        have.add(key);
      }
      return out;
    }

    // ----- register helpers -----
    sourceIndexFor(want) {
      const t = TABLES[0], id1 = this.col(t, C.id1), id2 = this.col(t, C.id2), id3 = this.col(t, C.id3);
      const pos = new Map(id1.map((v, k) => [String(big(v)), k]));
      const W2 = BigInt(JSON.parse(want.id2)), W3 = BigInt(JSON.parse(want.id3));
      const used = new Set(this.col(TABLES[1], C.parent).map(v => Number(big(v))));
      let exact = [], kind = [];
      this.reg.forEach((v, r) => {
        const k = pos.get(String(big(v))); if (k === undefined) return;
        if (big(id3[k]) !== W3) return;
        (big(id2[k]) === W2 ? exact : kind).push(r);
      });
      const pick = l => l.find(r => used.has(r)) ?? l[0];
      const r = exact.length ? pick(exact) : kind.length ? pick(kind) : undefined;
      if (r === undefined) throw new Error('This save has no purchase record of the right kind yet (buy any car in-game first).');
      return r;
    }
    allIds() {
      const s = new Set();
      for (const t of TABLES) if (this.inv[t]) for (const v of this.col(t, C.id1)) s.add(big(v));
      for (const v of this.reg) s.add(big(v));
      return s;
    }

    // ----- add -----
    addCar(libKey, garageHash) {
      const lc = this.libByKey[libKey]; if (!lc) throw new Error('Unknown library car.');
      const street = garageHash == null;
      if (NO_GARAGE_CLASSES.has(lc.class)) throw new Error(lc.name + ' (' + lc.class + ') cannot be added.');
      if (lc.street !== street) throw new Error(lc.name + ' copy is for ' + (lc.street ? 'street parking' : 'garages') + '.');
      if (this.ownedModelIds().has(String(lc.modelId >>> 0))) throw new Error('This save already has a ' + lc.name + '.');
      if (!street) {
        garageHash = String(garageHash);
        if (!this.garages().some(g => g.hash === garageHash)) throw new Error('The player does not own that garage slot.');
        if (this.garages().find(g => g.hash === garageHash)?.car) throw new Error('That garage is not empty (use Replace).');
      }
      const need = 1;
      if (this.itemsUsed() + need > ITEM_LIMIT - RESERVED_ITEMS) throw new Error('Not enough room in this save for another car.');

      const entry = parseJSON(lc.entry);
      // car id must be unique
      const usedCar = new Set(this.cars.map(c => u32(big(c['$0x1b60404d'])).toString()));
      let carId = u32(big(entry['$0x1b60404d']));
      while (usedCar.has(carId.toString()) || carId === 0n) carId = u32(BigInt(Math.floor(Math.random() * 0xffffffff)) + 1n);
      entry['$0x1b60404d'] = mk(carId);

      const t2 = TABLES[1];
      const row = Object.fromEntries(Object.entries(lc.row).map(([k, v]) => [k, parseJSON(v)]));
      row[C.carId] = mk(s32(carId));
      row[C.garage] = mk(street ? 0n : s32(BigInt(garageHash)));
      row[C.parent] = mk(this.sourceIndexFor(lc.source));
      // fresh 64-bit id, in this save's own numbering
      const ids = this.allIds(); let next = [...ids].reduce((m, x) => (x > m ? x : m), 0n) + 1000n;
      while (ids.has(next)) next++;
      row[C.id1] = mk(next);
      // No copied parts: the game discards copied part items on its first save and the car keeps
      // working (tested in game), so the car is added on its own and is not a register parent.
      this.appendRow(t2, row);
      this.cars.push(entry);
      this.log.push('Added ' + lc.name + (street ? ' (street)' : ' to ' + this.garageName(garageHash)));
    }
    garageName(h) { const g = this.garages().find(x => x.hash === String(h)); return g ? g.name : 'garage slot'; }
    appendRow(t, row) {
      if (!this.inv[t]) throw new Error('Save is missing table ' + t);
      const cols = Object.keys(this.inv[t]);
      const have = Object.keys(row);
      if (cols.length !== have.length || cols.some(c => !(c in row))) throw new Error('Car data does not match this save version.');
      for (const c of cols) arr(this.inv[t][c]).push(row[c]);
      const pos = this.tableLen(t) - 1, tn = TABLES.indexOf(t) + 1;
      arr(this.inv['$0xabb1e011']).push(mk((BigInt(pos) << 4n) | BigInt(tn)));
    }

    // ----- remove / replace -----
    removeCarInGarage(garageHash) {
      const g = this.garages().find(x => x.hash === String(garageHash));
      if (!g || !g.car) throw new Error('No car in that garage.');
      this.removeRow(g.car.row);
      this.log.push('Removed ' + g.car.name + ' from ' + g.name);
    }
    removeRow(carRow) {
      const t2 = TABLES[1];
      const carId = u32(big(this.col(t2, C.carId)[carRow]));
      const regIdx = new Map(this.reg.map((v, r) => [String(big(v)), r]));
      // collect the car and every descendant item
      const removed = new Map(); TABLES.forEach(t => removed.set(t, new Set()));
      removed.get(t2).add(carRow);
      const queue = [String(big(this.col(t2, C.id1)[carRow]))], gone = new Set(queue);
      while (queue.length) {
        const r = regIdx.get(queue.shift()); if (r === undefined) continue;
        for (const t of TABLES) {
          if (!this.inv[t]) continue;
          const par = this.col(t, C.parent), id1 = this.col(t, C.id1);
          par.forEach((p, k) => { if (Number(big(p)) === r && !removed.get(t).has(k)) { removed.get(t).add(k); const id = String(big(id1[k])); gone.add(id); queue.push(id); } });
        }
      }
      this.dropRows(removed, gone);
      const ci = this.cars.findIndex(c => u32(big(c['$0x1b60404d'])) === carId);
      if (ci >= 0) this.cars.splice(ci, 1);
    }
    dropRows(removed, goneIds) {
      // 1) register: drop removed ids, then any parent left with no children
      let keep = this.reg.map(v => !goneIds.has(String(big(v))));
      const posMap = {};
      for (const t of TABLES) {
        if (!this.inv[t]) continue;
        const n = this.tableLen(t), rm = removed.get(t), map = new Int32Array(n).fill(-1);
        let j = 0; for (let k = 0; k < n; k++) if (!rm.has(k)) map[k] = j++;
        posMap[t] = map;
      }
      for (;;) {
        const hasKid = new Array(this.reg.length).fill(false);
        for (const t of TABLES) if (this.inv[t]) this.col(t, C.parent).forEach((p, k) => { if (posMap[t][k] >= 0) hasKid[Number(big(p))] = true; });
        let changed = false;
        keep = keep.map((kp, r) => { if (kp && !hasKid[r]) { changed = true; return false; } return kp; });
        if (!changed) break;
      }
      const regMap = []; let j = 0; keep.forEach((kp, r) => { regMap[r] = kp ? j++ : -1; });
      // 2) rewrite tables
      for (const t of TABLES) {
        if (!this.inv[t]) continue;
        const map = posMap[t];
        for (const c of Object.keys(this.inv[t])) {
          const a = arr(this.inv[t][c]); const out = a.filter((_, k) => map[k] >= 0);
          if (c === C.parent) out.forEach((p, k) => { const m = regMap[Number(big(p))]; if (m < 0) throw new Error('internal: orphaned item'); out[k] = mk(m); });
          a.length = 0; a.push(...out);
        }
      }
      const newReg = this.reg.filter((_, r) => keep[r]); this.reg.length = 0; this.reg.push(...newReg);
      // 3) items index
      const idx = arr(this.inv['$0xabb1e011']); const out = [];
      for (const e of idx) {
        const v = big(e), tn = Number(v & 15n), pos = Number(v >> 4n), t = TABLES[tn - 1];
        if (!t || !posMap[t]) { out.push(e); continue; }
        const np = posMap[t][pos]; if (np >= 0) out.push(np === pos ? e : mk((BigInt(np) << 4n) | BigInt(tn)));
      }
      idx.length = 0; idx.push(...out);
    }
    replaceCar(garageHash, libKey) {
      const lc = this.libByKey[libKey]; if (!lc) throw new Error('Unknown library car.');
      const g = this.garages().find(x => x.hash === String(garageHash));
      if (!g) throw new Error('The player does not own that garage.');
      if (g.car && String(lc.modelId >>> 0) === g.car.modelId) throw new Error('That garage already has a ' + lc.name + '.');
      if (g.car) this.removeCarInGarage(garageHash);
      this.addCar(libKey, garageHash);
    }

    // ----- validation + output -----
    validate() {
      const problems = [];
      const total = this.itemsUsed(), idx = arr(this.inv['$0xabb1e011']);
      if (total > ITEM_LIMIT) problems.push('too many items');
      const seen = new Set();
      for (const e of idx) seen.add(String(big(e)));
      if (seen.size !== idx.length || idx.length !== total) problems.push('items index does not match tables');
      for (const t of TABLES) if (this.inv[t]) for (let k = 0; k < this.tableLen(t); k++) if (!seen.has(String((BigInt(k) << 4n) | BigInt(TABLES.indexOf(t) + 1)))) { problems.push('item missing from index'); break; }
      const hasKid = new Array(this.reg.length).fill(false);
      for (const t of TABLES) if (this.inv[t]) for (const p of this.col(t, C.parent)) { const n = Number(big(p)); if (n >= this.reg.length) problems.push('bad parent link'); else hasKid[n] = true; }
      if (hasKid.some(x => !x)) problems.push('register entry without children');
      const rows = new Set(this.carRows().map(k => String(u32(big(this.col(TABLES[1], C.carId)[k])))));
      for (const r of rows) if (!this.cars.some(c => String(u32(big(c['$0x1b60404d']))) === r)) problems.push('garage row without vehicle data');
      const occ = {};
      for (const k of this.carRows()) { const g = this.describeRow(k).garage; if (g !== '0') { occ[g] = (occ[g] || 0) + 1; if (occ[g] > 1) problems.push('two cars in one garage'); } }
      return problems;
    }
    toBytes() {
      const problems = this.validate().filter(p => !this.baseline.has(p));
      if (problems.length) throw new Error('Edit failed safety checks: ' + [...new Set(problems)].join(', '));
      let out = this.bytes;
      const newRecs = this.recs.map(r => r.slice());
      for (const b of [3, 16].sort((a, c) => this.recs[c][3] - this.recs[a][3])) {
        const [, , , off, size] = this.recs[b];
        const nb = enc.encode(dump(this.blocks[b])), d = nb.length - size;
        const o = new Uint8Array(out.length + d);
        o.set(out.subarray(0, off), 0); o.set(nb, off); o.set(out.subarray(off + size), off + nb.length);
        out = o; newRecs[b][4] += d;
        for (let k = 0; k < newRecs.length; k++) if (this.recs[k][3] > off) newRecs[k][3] += d;
      }
      const dv = new DataView(out.buffer, out.byteOffset, out.byteLength);
      newRecs.forEach((r, k) => r.forEach((v, j) => dv.setUint32(16 + 20 * k + 4 * j, v >>> 0, true)));
      // re-open the result to prove it parses
      new Save(out, this.lib);
      return out;
    }
  }

  function mergeCars(library, cars) {
    const have = new Set(library.cars.map(c => c.key || ((c.model >>> 0) + (c.street ? 's' : 'g'))));
    let added = 0;
    for (const c of cars || []) {
      if (!c || typeof c.entry !== 'string' || !c.row || !c.source) continue;
      try { parseJSON(c.entry); for (const v of Object.values(c.row)) parseJSON(v); } catch (e) { continue; }
      const key = (c.model >>> 0) + (c.street ? 's' : 'g'); if (have.has(key)) continue;
      library.cars.push(Object.assign({}, c, { key })); have.add(key); added++;
    }
    return added;
  }
  return { VERSION, open: (bytes, library) => new Save(bytes, library), mergeCars, nameOf, NO_GARAGE_CLASSES, _internal: { parseJSON, dump } };
});
