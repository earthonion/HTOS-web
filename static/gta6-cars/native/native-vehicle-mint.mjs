import { LANDSTALKER_XL } from "./landstalker-xl.mjs";
import { experimentalVehicles } from "./native-catalog.mjs";

const MAX_STORY_BYTES = 8 * 1024 * 1024;
const MAX_PRESET_PARTS = 512;
const PLATE_SPACE = 36 ** 6;
const BLOCK_COUNT = 82;
const DIRECTORY_END = 16 + 20 * BLOCK_COUNT;
const INVENTORY_BLOCK = 3, VEHICLE_BLOCK = 16;
const INVENTORY_HASH = 0xD3F59A5E, VEHICLE_HASH = 0x7E56D644;
const TABLE1 = "$0xfc8f2620", TABLE2 = "$0x6ebbccb1";
const ITEM = "$0xb4bfeab1", PARENT = "$0xc5730137", TAG = "$0xbe8e255c", KIND = "$0x12ae4287";
const GARAGE = "$0xb4ca9f4d", CAR = "$0xd65b1f97";
const REGISTER = "$0xd558e4da", INDEX = "$0xabb1e011";
const ENTRY_ID = "$0x1b60404d", ENTRY_KIND = "$0x68c27e33", ENTRY_CRC = "$0x5405a4f4", ENTRY_BODY = "$0x08ccb621";
const MIN_I64 = -(1n << 63n), MAX_I64 = (1n << 63n) - 1n;
const col = (table, field) => Array.isArray(table[field]) ? table[field] : table[field]?.$data ?? [];
const u32 = value => Number(BigInt.asUintN(32, value));
const uid = value => BigInt.asUintN(64, value).toString();
const hex = n => "0x" + n.toString(16).toUpperCase().padStart(8, "0");
const viewOf = bytes => new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
const garageProfiles = [
  { garage: LANDSTALKER_XL.garage, intermediateTag: 0x204CFC5A, garageValue: LANDSTALKER_XL.garageValue },
  { garage: 0x6B73F22B, intermediateTag: 0xFB720888, garageValue: 0xCBC02B72 },
  { garage: 0x4035403B, intermediateTag: 0x2E05A8A7, garageValue: 0xE3D4F136 },
  { garage: 0x7223969A, intermediateTag: 0x8C8D5DF7, garageValue: 0x2D31AB60 },
];

export const verifiedNativeGarages = Object.freeze({
  [LANDSTALKER_XL.model]: '0x25797657',
  [0x16C43D15]: '0x25797657',
  [0x6F946279]: '0x6B73F22B',
  [0xA774B5A6]: '0x4035403B',
  [0x2EA68690]: '0x6B73F22B',
});

function requireThat(condition, message) {
  if (!condition) throw new Error(message);
}

function integer(raw) {
  requireThat(typeof raw?.r === "string" && /^-?(?:0|[1-9][0-9]*)$/.test(raw.r),
    "Unexpected integer token in the save.");
  return BigInt(raw.r);
}

function position(raw) {
  const value = integer(raw);
  requireThat(value >= 0n && value <= 4095n, "Inventory position is outside the supported range.");
  return Number(value);
}

function readDirectory(bytes) {
  requireThat(bytes instanceof Uint8Array && bytes.length > DIRECTORY_END && bytes.length <= MAX_STORY_BYTES,
    "Choose a decoded PS5 SARC file between 1.7 KB and 8 MiB.");
  const view = viewOf(bytes);
  requireThat(view.getUint32(0, true) === 0x5641534A &&
    view.getUint32(4, true) === 3 && view.getUint32(8, true) === 38 && view.getUint32(12, true) === 44,
  "Choose a decoded GTA VI PS5 JSAV Story file with the supported 82-block layout.");

  const blocks = [];
  let cursor = DIRECTORY_END;
  for (let i = 0; i < BLOCK_COUNT; i++) {
    const entry = 16 + 20 * i;
    const hash = view.getUint32(entry, true);
    const start = view.getUint32(entry + 12, true);
    const end = start + view.getUint32(entry + 16, true);
    requireThat(view.getUint32(entry + 4, true) === 1 && view.getUint32(entry + 8, true) === 0 &&
      cursor <= start && start < end && end <= bytes.length, "The JSAV directory is malformed.");
    blocks.push({ hash, start, end });
    cursor = end;
  }
  requireThat(blocks[INVENTORY_BLOCK].hash === INVENTORY_HASH && blocks[VEHICLE_BLOCK].hash === VEHICLE_HASH,
    "The inventory or vehicle block does not match the supported format.");
  return blocks;
}

const CRC_TABLE = Uint32Array.from({ length: 256 }, (_, byte) => {
  let n = byte;
  for (let i = 0; i < 8; i++) n = (n >>> 1) ^ (n & 1 ? 0xEDB88320 : 0);
  return n >>> 0;
});

function crc32(bytes) {
  let n = 0xFFFFFFFF;
  for (const byte of bytes) n = (n >>> 8) ^ CRC_TABLE[(n ^ byte) & 255];
  return (n ^ 0xFFFFFFFF) >>> 0;
}

function readPacked(entry) {
  const tokens = col(entry, ENTRY_BODY);
  requireThat(Array.isArray(tokens) && tokens.length >= 50 && tokens.length <= 65536,
    "An existing vehicle has an unsupported packed size.");
  const body = Uint8Array.from(tokens, token => {
    const n = integer(token);
    requireThat(n >= -128n && n <= 255n, "An existing vehicle contains an invalid packed byte.");
    return Number(n);
  });
  const view = viewOf(body);
  requireThat(view.getUint32(0, true) === body.length && view.getUint32(4, true) === 0 &&
    view.getUint32(8, true) === 10 && view.getUint32(12, true) === 1,
  "An existing vehicle has an unsupported packed header.");
  requireThat(u32(integer(entry[ENTRY_CRC])) === crc32(body), "An existing vehicle failed its CRC-32 check.");

  const kinds = [];
  let plate, terminalSeen = false;
  for (let at = 26; at + 12 <= body.length;) {
    const kind = view.getUint32(at, true);
    const size = view.getUint32(at + 4, true);
    const flags = view.getUint32(at + 8, true);
    at += 12;
    if (kind === 0xFFFFFFFF) {
      requireThat(at === body.length && flags === 0, "An existing vehicle has a malformed terminator.");
      terminalSeen = true;
      break;
    }
    requireThat(kind >= 1 && kind <= 9 && flags === Number(kind >= 3 && kind <= 5) &&
      at + size <= body.length, "An existing vehicle has malformed packed sections.");
    if (kind === 1) {
      requireThat(size >= 4 && size === 4 * (view.getUint32(at, true) + 1),
        "An existing vehicle has a malformed parts section.");
    }
    if (kind === 7) {
      requireThat(size === 8, "An existing vehicle has a malformed plate section.");
      plate = new TextDecoder().decode(body.subarray(at, at + size));
    }
    kinds.push(kind);
    at += size;
  }
  const order = kinds.join(",");
  requireThat(terminalSeen && (order === "1,7" || order === "1,2,3,4,5,6,7,8,9"),
    "An existing vehicle has an unsupported packed section order.");
  return { body, model: view.getUint32(20, true), plate };
}

function findPurchase(table, register, index, { garage, intermediateTag }) {
  const ids = col(table, ITEM), kinds = col(table, KIND), tags = col(table, TAG), parents = col(table, PARENT);
  requireThat([ids, kinds, tags, parents].every(Array.isArray) &&
    ids.length === kinds.length && ids.length === tags.length && ids.length === parents.length,
  "The primary property table is not aligned.");
  const byItem = new Map();
  for (let row = 0; row < ids.length; row++) {
    if (index.get((row << 4) | 1) !== 1) continue;
    const id = uid(integer(ids[row]));
    requireThat(!byItem.has(id), "The save has ambiguous indexed property IDs.");
    byItem.set(id, { row, rawId: ids[row], kind: u32(integer(kinds[row])),
      tag: u32(integer(tags[row])), parent: position(parents[row]) });
  }
  const registered = register.map(token => uid(integer(token)));
  const parentOf = row => row && byItem.get(registered[row.parent]);
  const paths = [...byItem.values()].filter(purchase => {
    if (purchase.kind !== 0x48C26615 || purchase.tag !== 0xB614395B) return false;
    const intermediate = parentOf(purchase);
    const property = parentOf(intermediate);
    return intermediate?.kind === 0x8D67A626 && intermediate.tag === intermediateTag &&
      property?.kind === 0xE99F1E83 && property.tag === garage &&
      parentOf(property)?.tag === 0xBAD2E902;
  });
  if (paths.length !== 1) return null;
  const purchase = paths[0];
  const priorPositions = registered.flatMap((id, index) => id === uid(integer(purchase.rawId)) ? [index] : []);
  requireThat(priorPositions.length <= 1, "The garage purchase has duplicate register entries.");
  return { purchase, intermediate: parentOf(purchase), property: parentOf(parentOf(purchase)),
    registerPosition: priorPositions[0] ?? register.length, appendRegister: priorPositions.length === 0 };
}

function inspectInventory(storyBytes, engine) {
  requireThat(typeof engine?.open === "function" && typeof engine?._internal?.parseJSON === "function",
    "Load the HTOS GTA6Cars engine before running the native-vehicle proof.");
  const layout = readDirectory(storyBytes);
  const save = engine.open(storyBytes, { cars: [], garages: garageProfiles.map(profile => profile.garage) });
  const groups = save.blocks[INVENTORY_BLOCK]["$0x7a47cb05"]["$0xd3f59a5e"]["$0x789bdd41"];
  const primary = groups?.[0], table = primary?.[TABLE2];
  requireThat(Array.isArray(groups) && primary?.[TABLE1] && table,
    "The save has no supported property and vehicle inventory.");
  const register = col(primary, REGISTER), indexTokens = col(primary, INDEX);
  requireThat(Array.isArray(register) && register.length < 4095 && Array.isArray(indexTokens) &&
    indexTokens.length < 4095, "The primary inventory is too large for another registered vehicle.");
  const index = new Map();
  for (const token of indexTokens) {
    const n = integer(token);
    requireThat(n >= 0n && n < 65536n, "The inventory has an out-of-range index entry.");
    index.set(Number(n), (index.get(Number(n)) ?? 0) + 1);
  }
  const occupied = new Set(groups.flatMap(group => group[TABLE2]
    ? col(group[TABLE2], GARAGE).map(n => u32(integer(n))) : []));
  return { layout, save, groups, primary, table, register, indexTokens, index, occupied };
}

export function availableNativeGarages(storyBytes, engine = globalThis.GTA6Cars) {
  const { primary, register, index, occupied } = inspectInventory(storyBytes, engine);
  return garageProfiles
    .filter(profile => !occupied.has(profile.garage) && findPurchase(primary[TABLE1], register, index, profile))
    .map(profile => ({ id: hex(profile.garage), label: "Garage " + hex(profile.garage) }));
}

function chooseIds(storyCrc, groups, vehicleIds, model) {
  const usedItems = new Set();
  let maxItem = 0n;
  for (const group of groups) {
    for (const token of col(group, REGISTER)) {
      const n = integer(token);
      requireThat(n >= MIN_I64 && n < 2n ** 64n, "Inventory contains an out-of-range registered ID.");
      usedItems.add(uid(n));
    }
    for (const table of Object.values(group)) {
      for (const token of col(table, ITEM) ?? []) {
        const n = integer(token);
        requireThat(n >= MIN_I64 && n <= 2n ** 64n - 1n, "Inventory contains an out-of-range 64-bit ID.");
        usedItems.add(uid(n));
        if (n > maxItem) maxItem = n;
      }
      for (const token of col(table, CAR) ?? []) vehicleIds.add(u32(integer(token)));
    }
  }
  let item = maxItem <= MAX_I64 - 1000n ? maxItem + 1000n : 1n;
  while (usedItems.has(uid(item))) item = item === MAX_I64 ? 1n : item + 1n;
  let vehicle = (storyCrc ^ model) >>> 0;
  while (!vehicle || vehicleIds.has(vehicle)) vehicle = (vehicle + 1) >>> 0;
  return { item, vehicle };
}

function choosePlate(storyCrc, preset, context) {
  const normalize = value => value.replace(/\0+$/, "").trim().toUpperCase();
  const used = new Set(context.filter(entry => entry.plate).map(entry => normalize(entry.plate)));
  if (preset === LANDSTALKER_XL) {
    requireThat(!used.has(normalize(preset.plate)), "The test plate is already present in this save.");
    return preset.plate;
  }
  let serial = ((storyCrc ^ preset.model ^ preset.config) >>> 0) % PLATE_SPACE;
  let plate;
  do {
    plate = "NV" + serial.toString(36).toUpperCase().padStart(6, "0");
    serial = (serial + 1) % PLATE_SPACE;
  } while (used.has(plate));
  return plate;
}

function encodeBody(preset, plateText, context) {
  const partsSize = 4 * (preset.parts.length + 1);
  const plate = new TextEncoder().encode(plateText);
  const bytes = new Uint8Array(26 + 12 + partsSize + 12 + plate.length + 12);
  const view = viewOf(bytes);
  const terminalOffset = bytes.length - 8;
  const terminalSource = context.findLast(entry => entry.body.length >= terminalOffset + 4);
  requireThat(terminalSource, "This save has no existing vehicle record long enough for the tested four-byte footer rule.");
  const terminal = viewOf(terminalSource.body).getUint32(terminalOffset, true);
  for (const [offset, value] of [[0, bytes.length], [8, 10], [12, 1], [16, preset.variant],
    [20, preset.model], [26, 1], [30, partsSize], [38, preset.parts.length]]) {
    view.setUint32(offset, value, true);
  }
  view.setUint16(24, LANDSTALKER_XL.word24, true);
  preset.parts.forEach((part, i) => view.setUint32(42 + 4 * i, part, true));
  const plateAt = 38 + partsSize;
  view.setUint32(plateAt, 7, true);
  view.setUint32(plateAt + 4, plate.length, true);
  bytes.set(plate, plateAt + 12);
  view.setUint32(terminalOffset - 4, 0xFFFFFFFF, true);
  view.setUint32(terminalOffset, terminal, true);
  return { bytes, terminal };
}

function sameBytes(a, b) {
  return a.length === b.length && a.every((byte, i) => b[i] === byte);
}

function validateOutput(before, after, baseLayout) {
  const layout = readDirectory(after);
  let beforeEnd = DIRECTORY_END, afterEnd = DIRECTORY_END;
  for (let i = 0; i < layout.length; i++) {
    const old = baseLayout[i], current = layout[i];
    requireThat(old.hash === current.hash &&
      sameBytes(before.subarray(beforeEnd, old.start), after.subarray(afterEnd, current.start)),
    "Repacking changed the JSAV envelope.");
    if (i !== INVENTORY_BLOCK && i !== VEHICLE_BLOCK) {
      requireThat(sameBytes(before.subarray(old.start, old.end), after.subarray(current.start, current.end)),
        "Repacking changed an unrelated save block.");
    }
    beforeEnd = old.end;
    afterEnd = current.end;
  }
  requireThat(sameBytes(before.subarray(beforeEnd), after.subarray(afterEnd)), "Repacking changed the JSAV trailer.");
}

export const nativeVehicles = Object.freeze([LANDSTALKER_XL]);

export function mintNativeVehicle(storyBytes, {
  engine = globalThis.GTA6Cars, preset = LANDSTALKER_XL, allowGarageTrial = false,
  garageId = hex(LANDSTALKER_XL.garage),
} = {}) {
  requireThat(typeof engine?.open === "function" && typeof engine?._internal?.parseJSON === "function",
    "Load the HTOS GTA6Cars engine before running the native-vehicle proof.");
  const catalogPreset = preset !== LANDSTALKER_XL;
  const garageTrial = catalogPreset && allowGarageTrial === true && preset?.garageTrial === true &&
    preset.eligible === false && preset.garageEligible === false;
  if (catalogPreset) {
    const ordinaryGaragePreset = preset?.eligible === true && preset.garageEligible === true &&
      !engine.NO_GARAGE_CLASSES?.has(preset.category);
    requireThat(experimentalVehicles.includes(preset) && (ordinaryGaragePreset || garageTrial),
      "Choose a native preset marked eligible in the experimental catalog or explicitly enable a catalog garage trial.");
    requireThat([preset.model, preset.variant, preset.config, preset.inventoryType].every(value =>
      Number.isInteger(value) && value > 0 && value <= 0xFFFFFFFF) &&
      Array.isArray(preset.parts) && preset.parts.length > 0 && preset.parts.length <= MAX_PRESET_PARTS &&
      preset.parts.every(part => Number.isInteger(part) && part > 0 && part <= 0xFFFFFFFF && part !== 0x41C9FAC3),
    "The selected native preset exceeds the supported configuration limits.");
  }
  const profile = garageProfiles.find(({ garage }) => hex(garage) === garageId);
  requireThat(profile, "Choose a garage with a known native placement profile.");
  const experimental = verifiedNativeGarages[preset.model] !== garageId;
  const { garage, garageValue } = profile;
  const { word24 } = LANDSTALKER_XL;
  const { layout: baseLayout, save, groups, primary, table, register, indexTokens, index, occupied } =
    inspectInventory(storyBytes, engine);
  const { parseJSON, dump } = engine._internal;
  requireThat(Array.isArray(save.cars) && save.cars.length < 4095, "The packed vehicle list is too large.");
  const beforeCount = save.cars.length;
  const context = save.cars.map(readPacked);
  requireThat(!context.some(entry => entry.model === preset.model), "This save already contains a " + preset.name + ".");
  const vehicleIds = new Set(save.cars.map(entry => u32(integer(entry[ENTRY_ID]))));
  requireThat(vehicleIds.size === save.cars.length, "The save has duplicate packed vehicle IDs.");
  for (const group of groups) {
    if (!group[TABLE2]) continue;
    requireThat(!col(group[TABLE2], TAG).some(n => u32(integer(n)) === preset.inventoryType),
      "The inventory already contains a " + preset.name + " model row.");
  }
  requireThat(!occupied.has(garage), "The tested garage already contains a vehicle. No save was changed.");
  const path = findPurchase(primary[TABLE1], register, index, profile);
  requireThat(path, "The tested garage does not have one unambiguous owned purchase chain in this save.");
  const storyCrc = crc32(storyBytes);
  const ids = chooseIds(storyCrc, groups, vehicleIds, preset.model);
  const rowFields = {
    [ITEM]: ids.item, [PARENT]: path.registerPosition, [TAG]: preset.inventoryType | 0,
    [KIND]: 576059957, "$0xe3fead7a": 1, "$0x63b44f39": false, "$0x3ac987de": 0,
    "$0xc63eb2eb": 576059957, "$0x851442d9": preset.config | 0, "$0xb3d46c50": 0, "$0xa5a2cfed": 0,
    [CAR]: ids.vehicle | 0, "$0x28ce0fff": 48, "$0xfb1eff6a": 0, [GARAGE]: garage | 0,
    "$0xa0481ea1": 0, "$0xcba9e511": 0, "$0x953769aa": true, "$0x6a775761": garageValue | 0,
  };
  const fields = Object.keys(table), row = col(table, ITEM).length;
  requireThat(fields.length === Object.keys(rowFields).length && fields.every(c =>
    Object.hasOwn(rowFields, c) && Array.isArray(col(table, c)) && col(table, c).length === row),
  "The vehicle inventory columns do not match the tested format.");
  requireThat(!index.has((row << 4) | 2), "The next vehicle index is already in use.");
  for (let prior = 0; prior < row; prior++) {
    requireThat(index.get((prior << 4) | 2) === 1, "The existing vehicle rows are not indexed consistently.");
  }

  const plate = choosePlate(storyCrc, preset, context);
  const generated = encodeBody(preset, plate, context);
  const checksum = crc32(generated.bytes);
  const entry = parseJSON(JSON.stringify({
    [ENTRY_ID]: ids.vehicle, [ENTRY_KIND]: 1, [ENTRY_CRC]: checksum,
    [ENTRY_BODY]: { "$content": "char_array", "$data": Array.from(generated.bytes) },
  }));
  const originalInventory = dump(save.blocks[INVENTORY_BLOCK]);
  const originalVehicles = dump(save.blocks[VEHICLE_BLOCK]);
  if (path.appendRegister) register.push(path.purchase.rawId);
  indexTokens.push(parseJSON(String((row << 4) | 2)));
  for (const field of fields) col(table, field).push(parseJSON(String(rowFields[field])));
  save.cars.push(entry);
  const newInventory = dump(save.blocks[INVENTORY_BLOCK]);
  const newVehicles = dump(save.blocks[VEHICLE_BLOCK]);
  const output = save.toBytes();
  validateOutput(storyBytes, output, baseLayout);
  const reopened = engine.open(output, { cars: [], garages: [garage] });
  requireThat(dump(reopened.blocks[INVENTORY_BLOCK]) === newInventory &&
    dump(reopened.blocks[VEHICLE_BLOCK]) === newVehicles &&
    sameBytes(readPacked(reopened.cars.at(-1)).body, generated.bytes),
  "The rewritten vehicle and inventory could not be verified.");
  save.cars.pop();
  for (const field of fields) col(table, field).pop();
  indexTokens.pop();
  if (path.appendRegister) register.pop();
  requireThat(dump(save.blocks[INVENTORY_BLOCK]) === originalInventory &&
    dump(save.blocks[VEHICLE_BLOCK]) === originalVehicles, "Existing inventory or vehicle data changed.");

  return { bytes: output, report: {
    vehicle: preset.name, model: hex(preset.model), variant: hex(preset.variant),
    preset: hex(preset.config), inventoryModelType: hex(preset.inventoryType), word24,
    partsCount: preset.parts.length, garage: hex(garage), plate,
    vehicleId: hex(ids.vehicle), inventoryItemId: ids.item.toString(), bodyCrc32: hex(checksum),
    footer: hex(generated.terminal), vehicleCountBefore: beforeCount, vehicleCountAfter: beforeCount + 1,
    table1Rows: [path.property.row, path.intermediate.row, path.purchase.row],
    inventoryRow: row, registerPosition: path.registerPosition, registerAppended: path.appendRegister,
    changedBlocks: [INVENTORY_BLOCK, VEHICLE_BLOCK], unchangedBlocks: BLOCK_COUNT - 2,
    sourceBytes: storyBytes.length, outputBytes: output.length, experimental, garageTrial,
    assumptions: experimental ? [
      "Byte 24 is set to zero using the console-tested Landstalker procedure.",
      "The stock preset uses the same compact inventory record shape as Landstalker.",
      "Four footer bytes come from a sufficiently long vehicle in the destination save.",
    ] : [],
  } };
}
