const maxStoryBytes = 8 * 1024 * 1024;
const maxZipBytes = 32 * 1024 * 1024;
const isManualName = name => /^SARC00(?:0[0-9]|1[0-4])$/.test(name);
const utf8 = new TextDecoder('utf-8', {fatal: true});

function invalidZip() {
  return new Error('The ZIP is damaged or uses an unsupported format.');
}

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

async function inflateStory(compressed, size) {
  const result = new Uint8Array(size);
  const reader = new Blob([compressed]).stream().pipeThrough(new DecompressionStream('deflate-raw')).getReader();
  let offset = 0;
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    if (offset + value.length > size) {
      await reader.cancel();
      throw invalidZip();
    }
    result.set(value, offset);
    offset += value.length;
  }
  if (offset !== size) throw invalidZip();
  return result;
}

async function readManualZip(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const u16 = at => view.getUint16(at, true);
  const u32 = at => view.getUint32(at, true);
  let end = bytes.length - 22;
  const limit = Math.max(0, end - 65535);
  while (end >= limit && (u32(end) !== 0x06054b50 || end + 22 + u16(end + 20) !== bytes.length)) end--;
  if (end < limit || u16(end + 4) || u16(end + 6)) throw invalidZip();
  const count = u16(end + 10);
  const directory = u32(end + 16);
  if (!count || count > 128 || count !== u16(end + 8) || directory + u32(end + 12) !== end) throw invalidZip();

  let cursor = directory;
  let member;
  for (let index = 0; index < count; index++) {
    if (cursor + 46 > end || u32(cursor) !== 0x02014b50) throw invalidZip();
    const nameEnd = cursor + 46 + u16(cursor + 28);
    const next = nameEnd + u16(cursor + 30) + u16(cursor + 32);
    if (next > end) throw invalidZip();
    const path = utf8.decode(bytes.subarray(cursor + 46, nameEnd));
    const pieces = path.split('/');
    const name = pieces.at(-1);
    const container = `SAVEDATA${name}`;
    if (isManualName(name) && (pieces.length === 1 || pieces.length === 2 &&
        [container, `sdimg_${container}`].includes(pieces[0]))) {
      if (member) throw new Error('Choose a ZIP containing exactly one manual save.');
      member = {
        name, path, flags: u16(cursor + 8), method: u16(cursor + 10), crc: u32(cursor + 16),
        compressedSize: u32(cursor + 20), size: u32(cursor + 24), local: u32(cursor + 42),
      };
    }
    cursor = next;
  }
  if (cursor !== end) throw invalidZip();
  if (!member) throw new Error('No SARC manual save was found in the ZIP.');
  const {name, path, flags, method, crc, compressedSize, size, local} = member;
  if (flags & 1 || ![0, 8].includes(method) || size < 16 || size > maxStoryBytes ||
      compressedSize > maxZipBytes || local + 30 > directory || u32(local) !== 0x04034b50 ||
      u16(local + 6) !== flags || u16(local + 8) !== method) throw invalidZip();
  const localNameEnd = local + 30 + u16(local + 26);
  const start = localNameEnd + u16(local + 28);
  if (start + compressedSize > directory ||
      utf8.decode(bytes.subarray(local + 30, localNameEnd)) !== path) throw invalidZip();
  const compressed = bytes.subarray(start, start + compressedSize);
  let story = compressed;
  if (method === 8) {
    if (typeof DecompressionStream !== 'function') throw new Error('Unzip the file first and choose the SARC manual save.');
    try {
      story = await inflateStory(compressed, size);
    } catch {
      throw invalidZip();
    }
  }
  if (story.length !== size || crc32(story) !== crc) throw invalidZip();
  return {name, bytes: story};
}

export async function openManualSave(file) {
  if (file.size < 16 || file.size > maxZipBytes) throw new Error('Choose a manual save or ZIP up to 32 MiB.');
  const bytes = new Uint8Array(await file.arrayBuffer());
  const save = bytes[0] === 0x50 && bytes[1] === 0x4b
    ? await readManualZip(bytes)
    : {name: file.name, bytes};
  if (!isManualName(save.name) || save.bytes.length > maxStoryBytes ||
      String.fromCharCode(...save.bytes.subarray(0, 4)) !== 'JSAV') {
    throw new Error('Choose a decrypted GTA VI PS5 manual SARC save.');
  }
  return save;
}
