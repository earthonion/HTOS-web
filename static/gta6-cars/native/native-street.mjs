// Native "no garage" vehicles for the GTA VI Car Giver.
// Builds stock vehicles from Malygos's native presets (HTOS-web PR #12) and stores them
// with no garage, like cars the player picked up on the street, so Scooter Bros can deliver them.
// In-game tested 2026-11-23: 160 generated vehicles saved and loaded; the phone lists vehicles up to
// a 40-vehicle total and Scooter Bros delivers them.
//
// Aircraft: the phone leaves aircraft out of the delivery list, and one parked in a garage gets stuck in
// the roof. The fix found 2026-11-23 is to give the aircraft row the inventory id of a land vehicle the
// player does not own (the phone decides what to list from that id) while the vehicle data itself stays
// the aircraft. Scooter Bros then drops the real aircraft on the nearest road, and a jet can take off from
// it. A nickname ("Flying <word>") is written so the entry does not read as the borrowed car.
// Confirmed on PS5: Buzzard, Lazer, Duster; a real car sharing the borrowed id still delivers correctly.
import { mintNativeVehicle, garageProfiles } from "./nvm2.mjs";
import { experimentalVehicles } from "./native-catalog.mjs";

export const VEHICLE_LIMIT = 40;            // the phone list stops at 40 vehicles
const INDEX_LIMIT = 4095, INDEX_RESERVE = 100;
const BLOCKED_CLASSES = new Set(["Aircraft", "Watercraft", "Trains", "Trailers", "Unclassified"]);
// Presets the catalog marks experimental (eligible=false) are only offered once confirmed in game.
// Confirmed 2026-11-23 on PS5: stored with no garage, listed by the phone and delivered by Scooter Bros.
const TESTED_EXPERIMENTAL = new Set(["'32 Golfer", "'70 Reclaimer Tow", "Rhino Tank", "APC", "'92 Patriot", "Ridgway",
  "Brigade", "Police Riot", "'99 Stockade", "Pumper", "Police Montagne"]);
const AIRCRAFT_NAMES = new Set(["Buzzard", "Cargobob", "Valkyrie", "lazer"]);   // aircraft filed under Military
// Aircraft confirmed in game with the borrowed-id trick. Others stay hidden until someone tests them.
const TESTED_AIRCRAFT = new Set(["Buzzard", "lazer", "Duster", "015A", "300-H", "Cargobob", "Crake", "Dodo",
  "Mammatus", "Maverick", "Nimbus", "Police Maverick", "Sea Sparrow", "Shamal", "Streamer 216-C",
  "SuperVolito", "Swift", "Valkyrie"]);
// Confirmed in game to be drones, not flyable aircraft - never offer these.
const BLOCKED_AIRCRAFT = new Set(["DZY 200", "DZY 80"]);
// Scooter Bros nickname words, as the game stores them (hash of the label, from the GTA VI label table).
const NICK_FIRST = { Flying: 85553288 };
const NICK_SECOND = { Machine: 1404761145, Beast: 3911333855, Monster: 3707569851, Wagon: 3418122854,
  Hammer: 2176810729, Cruiser: 448402357, Rider: 240150593, Dream: 416972973, Rocket: 990643568,
  Whale: 2496602763, Shark: 2190640229, Eagle: 3109335733, Hawk: 3021738190, Angel: 4135797467,
  Tiger: 2927573053, Snake: 1120149194, Panther: 1881956732, Dolphin: 3254837649,
  Bird: 427872747, Rat: 2113932330, Bat: 3787623550, Spider: 639641825, Crow: 1361734225,
  Owl: 2948715080, Goose: 3698965452, Duck: 3277602852, Pigeon: 534772467, Turtle: 2732451294,
  Rabbit: 93448477, Mouse: 780258465, Cat: 2433870751, Dog: 668095695, Horse: 1045665661,
  Goat: 1909443099, Monkey: 2046445964, Crab: 773011103, Fish: 2625948152, Fox: 1492081830,
  Bear: 4071823047 };
const T0 = "$0xfc8f2620", T1 = "$0x6ebbccb1", TABLES = [T0, T1, "$0xc500ddf4", "$0x9098d12e", "$0x1085013f"];
const C = { id1: "$0xb4bfeab1", parent: "$0xc5730137", id2: "$0xbe8e255c", id3: "$0x12ae4287",
  carId: "$0xd65b1f97", garage: "$0xb4ca9f4d", owner2: "$0xc63eb2eb", flag: "$0x63b44f39",
  nick1: "$0xb3d46c50", nick2: "$0xa5a2cfed" };
const B = x => BigInt(x.r ?? x), u32 = x => BigInt.asUintN(32, B(x));

export function landPresets() {
  return experimentalVehicles.filter(v => (v.eligible === true || TESTED_EXPERIMENTAL.has(v.name)) && !BLOCKED_CLASSES.has(v.category) && !AIRCRAFT_NAMES.has(v.name))
    .map(v => ({ name: v.name, category: v.category, model: String(v.model >>> 0), inventoryType: v.inventoryType >>> 0 }));
}

// Aircraft offered by the panel. Each borrows a land vehicle's inventory id, so they are listed separately.
export function aircraftPresets() {
  return experimentalVehicles.filter(v => TESTED_AIRCRAFT.has(v.name) && !BLOCKED_AIRCRAFT.has(v.name))
    .map(v => ({ name: v.name, category: "Aircraft", model: String(v.model >>> 0), aircraft: true }));
}

function deriveProfiles(G, bytes) {
  const s = G.open(bytes, { cars: [], garages: [] });
  const id1 = s.col(T0, C.id1), id2 = s.col(T0, C.id2), id3 = s.col(T0, C.id3), par = s.col(T0, C.parent);
  const rowOf = new Map(id1.map((v, k) => [String(B(v)), k]));
  const up = k => { const id = s.reg[Number(B(par[k]))]; return id ? rowOf.get(String(B(id))) ?? -1 : -1; };
  const found = [];
  for (let k = 0; k < id3.length; k++) {
    if (B(id3[k]) !== 1220699669n) continue;
    const it = up(k); if (it < 0 || B(id3[it]) !== 2372380198n) continue;
    const pr = up(it); if (pr < 0 || B(id3[pr]) !== 3919519363n) continue;
    const garage = Number(u32(id2[pr])), inter = Number(u32(id2[it]));
    if (!garageProfiles.some(p => p.garage === garage)) garageProfiles.push({ garage, intermediateTag: inter, garageValue: 0xE3D4F136 });
    found.push(garage);
  }
  return found;
}

// What the save allows before anything is changed.
export function plan(G, bytes, lib) {
  const s = G.open(bytes, lib);
  const vehicles = s.carRows().length, indexUsed = Math.max((s.inv["$0xabb1e011"].$data ?? s.inv["$0xabb1e011"]).length, s.reg.length);
  const owned = new Set(s.cars.map(e => s.modelOf(e)));
  const template = s.carRows().find(k => { const d = s.describeRow(k); return d.garage === "0" && d.hasData; });
  const garages = deriveProfiles(G, bytes);
  const slotsLeft = Math.max(0, VEHICLE_LIMIT - vehicles);
  const itemRoom = Math.max(0, INDEX_LIMIT - INDEX_RESERVE - indexUsed - 4);   // generator needs 4 temporary slots
  const problems = [];
  if (template === undefined) problems.push("This save has no vehicle outside a garage to copy the no-garage settings from. Take any car out of a garage in game, save, and try again.");
  if (!garages.length) problems.push("This save has no owned garage the generator can work through.");
  if (!slotsLeft) problems.push(`This save already has ${vehicles} vehicles. The phone only lists ${VEHICLE_LIMIT}.`);
  if (!itemRoom) problems.push("This save is at the item limit (usually from a very large bank balance). Lower the bank with the money editor to free item slots.");
  const land = landPresets().filter(p => !owned.has(p.model));
  // an aircraft needs a spare land-vehicle inventory id to borrow
  const usedIds = new Set(s.carRows().map(k => String(u32(s.col(T1, C.id2)[k]))));
  const masks = landPresets().filter(p => !usedIds.has(String(p.inventoryType)));
  const air = masks.length ? aircraftPresets().filter(p => !owned.has(p.model)) : [];
  if (!masks.length && aircraftPresets().length) problems.push("No spare vehicle id is free for aircraft in this save.");
  return { vehicles, slotsLeft, itemRoom, maxAdd: Math.min(slotsLeft, itemRoom), owned, problems,
    choices: land.concat(air) };
}

// Adds the named presets with no garage. Returns { bytes, added, skipped }. Never returns a save that failed checks.
export function addStreetVehicles(G, bytes, lib, names) {
  const p = plan(G, bytes, lib);
  if (p.problems.length) throw new Error(p.problems.join(" "));
  const wanted = [...new Set(names)];
  if (wanted.length > p.maxAdd) throw new Error(`Only ${p.maxAdd} more vehicle(s) fit in this save.`);
  const baseline = new Set(G.open(bytes, lib).validate());
  const garage = deriveProfiles(G, bytes)[0];
  let src = bytes; const added = [], skipped = [], nicknames = {};
  const nickWords = Object.keys(NICK_SECOND); let nickUsed = 0;
  const prev = globalThis.ALLOW_OCCUPIED; globalThis.ALLOW_OCCUPIED = true;
  try {
    for (const name of wanted) {
      const preset = experimentalVehicles.find(v => v.name === name);
      const isAir = aircraftPresets().some(x => x.name === name);
      const info = preset && (landPresets().find(x => x.name === name) || aircraftPresets().find(x => x.name === name));
      if (!info) { skipped.push(name + " (not a supported vehicle)"); continue; }
      if (p.owned.has(info.model)) { skipped.push(name + " (already owned)"); continue; }
      try {
        const n0 = G.open(src, lib).tableLen(T0);
        const r = mintNativeVehicle(src, { engine: G, preset, allowGarageTrial: true, garageId: "0x" + garage.toString(16).toUpperCase() });
        const v = G.open(r.bytes, lib);
        const proto = Object.getPrototypeOf(v.col(T1, C.garage)[0]); const mk = s => { const o = Object.create(proto); o.r = String(s); return o; };
        const newId = BigInt(parseInt(r.report.vehicleId, 16));
        const k = v.col(T1, C.carId).findIndex(x => u32(x) === newId);
        const ref = v.carRows().find(j => { const d = v.describeRow(j); return d.garage === "0" && d.hasData && u32(v.col(T1, C.carId)[j]) !== newId; });
        if (k < 0 || ref === undefined) throw new Error("could not place it");
        v.col(T1, C.garage)[k] = mk(0); v.col(T1, C.owner2)[k] = mk(0); v.col(T1, C.flag)[k] = mk("true");
        v.col(T1, C.parent)[k] = mk(v.col(T1, C.parent)[ref].r);
        if (isAir) {
          // borrow a free land vehicle's inventory id so the phone lists it, and label it so it reads as an aircraft
          const used = new Set(v.carRows().map(j => String(u32(v.col(T1, C.id2)[j]))));
          const mask = landPresets().find(x => !used.has(String(x.inventoryType)));
          if (!mask) throw new Error("no spare vehicle id left to borrow");
          v.col(T1, C.id2)[k] = mk(BigInt.asIntN(32, BigInt(mask.inventoryType)));
          const word = nickWords[nickUsed++ % nickWords.length];
          v.col(T1, C.nick1)[k] = mk(BigInt.asIntN(32, BigInt(NICK_FIRST.Flying)));
          v.col(T1, C.nick2)[k] = mk(BigInt.asIntN(32, BigInt(NICK_SECOND[word])));
          nicknames[name] = "Flying " + word;
        }
        // drop the temporary garage records the generator adds
        const removed = new Map(TABLES.map(t => [t, new Set()])), gone = new Set();
        for (let j = n0; j < v.tableLen(T0); j++) { removed.get(T0).add(j); gone.add(String(B(v.col(T0, C.id1)[j]))); }
        v.dropRows(removed, gone);
        src = v.toBytes(); added.push(name); p.owned.add(info.model);
      } catch (e) { skipped.push(name + " (" + e.message.slice(0, 80) + ")"); }
    }
  } finally { globalThis.ALLOW_OCCUPIED = prev; }
  const after = G.open(src, lib), newProblems = after.validate().filter(x => !baseline.has(x));
  if (newProblems.length) throw new Error("Safety check failed, nothing changed: " + newProblems.join(", "));
  if (after.carRows().length > VEHICLE_LIMIT) throw new Error("Safety check failed: over the vehicle limit.");
  return { bytes: src, added, skipped, nicknames };
}
