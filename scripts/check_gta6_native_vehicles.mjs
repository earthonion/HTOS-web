import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {basename} from 'node:path';
import {crc32} from 'node:zlib';
import {LANDSTALKER_XL} from '../static/gta6-cars/native/landstalker-xl.mjs';
import {experimentalVehicles} from '../static/gta6-cars/native/native-catalog.mjs';
import {availableNativeGarages,mintNativeVehicle,verifiedNativeGarages} from '../static/gta6-cars/native/native-vehicle-mint.mjs';
import {openManualSave} from '../static/gta6-cars/native/manual-save.mjs';

const sourcePath = process.argv[2];
if (!sourcePath) throw new Error('Usage: node scripts/check_gta6_native_vehicles.mjs <decrypted manual SARC or ZIP>');
const engine = createRequire(import.meta.url)('../static/gta6-cars/gta6-cars.js');
const file = new File([await readFile(sourcePath)], basename(sourcePath));
const {name, bytes} = await openManualSave(file);
const sha256 = data => createHash('sha256').update(data).digest('hex');
const sourceSha256 = sha256(bytes);
const open = story => engine.open(story, {cars: [], garages: []});
const original = open(bytes);
const garages = availableNativeGarages(bytes, engine);
assert.ok(garages.length, 'The fixture needs a supported empty garage.');
const vehicles = [LANDSTALKER_XL, ...experimentalVehicles];
assert.equal(new Set(vehicles.map(vehicle => vehicle.model)).size, vehicles.length);

const number = token => Number(BigInt.asUintN(32, BigInt(token.r)));
const results = vehicles.map(preset => {
  const identity = {name: preset.name, model: preset.model, category: preset.category ?? 'SUVs',
    variant: preset.variant, config: preset.config, donorAvailable: preset.donorAvailable ?? false};
  const garageTrial = preset.garageTrial === true;
  if (preset.eligible === false && !garageTrial) return {...identity, status: 'unavailable', reason: preset.blockReason};
  const verifiedGarage = verifiedNativeGarages[preset.model];
  const garageId = garages.find(garage => garage.id === verifiedGarage)?.id ?? garages[0].id;
  if (garageTrial) {
    assert.throws(() => mintNativeVehicle(bytes, {engine, preset}), /eligible|garage.fit/i, preset.name);
  }
  let output;
  try {
    output = mintNativeVehicle(bytes, {engine, preset, garageId, allowGarageTrial: garageTrial});
  } catch (error) {
    return {...identity, status: 'source rejected', garageTrial, reason: error.message};
  }

  const edited = open(output.bytes);
  assert.equal(edited.cars.length, original.cars.length + 1, preset.name);
  assert.equal(output.report.garage, garageId, preset.name);
  assert.equal(output.report.experimental, garageId !== verifiedGarage, preset.name);
  assert.deepEqual(edited.cars.slice(0, -1), original.cars, preset.name);
  const added = edited.cars.at(-1);
  const body = Uint8Array.from(added['$0x08ccb621'].$data, number);
  const view = new DataView(body.buffer, body.byteOffset, body.byteLength);
  assert.equal(view.getUint32(0, true), body.length, preset.name);
  assert.equal(view.getUint32(16, true), preset.variant, preset.name);
  assert.equal(view.getUint32(20, true), preset.model, preset.name);
  assert.equal(view.getUint16(24, true), 0, preset.name);
  assert.equal(view.getUint16(26, true), 1, preset.name);
  assert.equal(view.getUint32(38, true), preset.parts.length, preset.name);
  assert.deepEqual(preset.parts, Array.from({length: preset.parts.length},
    (_, index) => view.getUint32(42 + 4 * index, true)), preset.name);
  assert.equal(number(added['$0x5405a4f4']), crc32(body), preset.name);
  return {...identity, status: garageTrial ? 'garage trial format passed' : 'format passed', bytes: body.length, parts: preset.parts.length,
    defaultEvidence: preset.defaultEvidence ?? null, ps5VerifiedPlacement: garageId === verifiedGarage};
});
assert.equal(sha256(bytes), sourceSha256);

const counts = {};
for (const {status} of results) counts[status] = (counts[status] ?? 0) + 1;
console.log(JSON.stringify({source: name, sourceSha256, counts, results}, null, 2));
if (counts['source rejected']) process.exitCode = 1;
