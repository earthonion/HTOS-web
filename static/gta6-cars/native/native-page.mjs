import {openManualSave} from './manual-save.mjs';
import {availableNativeGarages,mintNativeVehicle,verifiedNativeGarages} from './native-vehicle-mint.mjs';
import {LANDSTALKER_XL} from './landstalker-xl.mjs';
import {experimentalVehicles} from './native-catalog.mjs';

const byId = id => document.getElementById(id);
const fileInput = byId('save-file');
const modelInput = byId('vehicle-model');
const vehicleHelp = byId('vehicle-help');
const garageInput = byId('garage');
const garageHelp = byId('garage-help');
const build = byId('build');
const status = byId('status');
const result = byId('result');
const download = byId('download');
const vehicles = [LANDSTALKER_XL, ...experimentalVehicles.filter(vehicle => vehicle.eligible || vehicle.garageTrial)];
const presets = new Map(vehicles.map(vehicle => [String(vehicle.model), vehicle]));
let selectedSave;
let fileRevision = 0;
let downloadUrl;

function showStatus(text, error = false) {
  status.textContent = text;
  status.toggleAttribute('data-error', error);
}

function clearDownload() {
  if (downloadUrl) URL.revokeObjectURL(downloadUrl);
  downloadUrl = undefined;
  download.removeAttribute('href');
  result.hidden = true;
}

function addOptions(label, vehicles) {
  const group = document.createElement('optgroup');
  group.label = label;
  for (const vehicle of vehicles) {
    const optionLabel = vehicle.category ? `${vehicle.name} · ${vehicle.category}` : vehicle.name;
    group.append(new Option(optionLabel, String(vehicle.model)));
  }
  modelInput.append(group);
}

function updateSelection(preferVerified = false) {
  const preset = presets.get(modelInput.value);
  const verifiedGarage = verifiedNativeGarages[preset.model];
  const available = selectedSave?.garages ?? [];
  if (preferVerified && available.some(garage => garage.id === verifiedGarage)) garageInput.value = verifiedGarage;
  const hasGarage = Boolean(garageInput.value);
  const verifiedPlacement = hasGarage && garageInput.value === verifiedGarage;
  vehicleHelp.textContent = verifiedGarage
    ? 'Previously delivered on PS5 in its tested garage.'
    : preset.garageTrial && preset.blockReason
      ? preset.blockReason
      : 'Experimental stock preset.';
  garageHelp.textContent = !selectedSave ? 'Open a manual save to see its available garages.'
    : !hasGarage ? 'This save has no supported empty garage.'
    : verifiedPlacement ? 'This model and garage placement was tested on PS5.'
    : 'This model and garage placement is experimental.';
  build.textContent = verifiedPlacement ? 'Build save' : 'Build test save';
  build.disabled = !hasGarage;
}

function showGarages() {
  const garages = selectedSave?.garages ?? [];
  garageInput.replaceChildren(...(garages.length
    ? garages.map(garage => new Option(garage.label, garage.id))
    : [new Option(selectedSave ? 'No available garage' : 'Open a manual save first', '')]));
  garageInput.disabled = !garages.length;
  updateSelection(true);
}

addOptions('PS5 verified', vehicles.filter(vehicle => verifiedNativeGarages[vehicle.model]));
addOptions('Experimental road vehicles', vehicles.filter(vehicle => vehicle.eligible && !verifiedNativeGarages[vehicle.model]));
addOptions('Experimental garage fit', vehicles.filter(vehicle => vehicle.garageTrial && !verifiedNativeGarages[vehicle.model]));
modelInput.disabled = false;
showGarages();

const blocked = experimentalVehicles.filter(vehicle => !vehicle.eligible && !vehicle.garageTrial);
if (blocked.length) {
  const details = byId('coverage');
  byId('coverage-summary').textContent = blocked.length === 1 ? 'One catalog vehicle is missing build data' : `${blocked.length} catalog vehicles are missing build data`;
  details.addEventListener('toggle', () => {
    const list = byId('coverage-list');
    if (!details.open || list.childElementCount) return;
    for (const vehicle of blocked) {
      const entry = document.createElement('li');
      entry.textContent = `${vehicle.name} · ${vehicle.blockReason}`;
      list.append(entry);
    }
  });
  details.hidden = false;
}

modelInput.addEventListener('change', () => {
  clearDownload();
  updateSelection(true);
  if (selectedSave) showStatus('');
});

garageInput.addEventListener('change', () => {
  clearDownload();
  updateSelection();
  if (selectedSave) showStatus('');
});

fileInput.addEventListener('change', async () => {
  const revision = ++fileRevision;
  selectedSave = undefined;
  clearDownload();
  showGarages();
  const file = fileInput.files[0];
  if (!file) {
    showStatus('');
    return;
  }
  showStatus('Opening the selected save…');
  try {
    const save = await openManualSave(file);
    if (revision !== fileRevision) return;
    selectedSave = {...save, garages: availableNativeGarages(save.bytes)};
    showGarages();
    showStatus(selectedSave.garages.length ? `${save.name} is ready to edit.` : '');
  } catch (error) {
    if (revision === fileRevision) showStatus(error.message, true);
  }
});

build.addEventListener('click', () => {
  const preset = presets.get(modelInput.value);
  const garageId = garageInput.value;
  if (!selectedSave || !preset || !garageId) return;
  clearDownload();
  try {
    const {bytes} = mintNativeVehicle(selectedSave.bytes, {preset, garageId, allowGarageTrial: preset.garageTrial === true});
    downloadUrl = URL.createObjectURL(new Blob([bytes], {type: 'application/octet-stream'}));
    download.href = downloadUrl;
    download.download = selectedSave.name;
    result.hidden = false;
    build.disabled = true;
    showStatus(`${preset.name} added. Your edited ${selectedSave.name} is ready to download.`);
  } catch (error) {
    showStatus(error.message, true);
  }
});
