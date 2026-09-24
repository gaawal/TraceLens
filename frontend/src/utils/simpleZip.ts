export interface ZipTextFile {
  name: string;
  content: string;
}

const encoder = new TextEncoder();

function crc32(data: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of data) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function pushUint16(target: number[], value: number) {
  target.push(value & 0xff, (value >>> 8) & 0xff);
}

function pushUint32(target: number[], value: number) {
  target.push(value & 0xff, (value >>> 8) & 0xff, (value >>> 16) & 0xff, (value >>> 24) & 0xff);
}

function dosDateTime(date: Date): { date: number; time: number } {
  const year = Math.max(1980, date.getFullYear());
  return {
    date: ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
    time: (date.getHours() << 11) | (date.getMinutes() << 5) | Math.floor(date.getSeconds() / 2),
  };
}

export function createStoredZip(files: ZipTextFile[]): Blob {
  const localParts: Uint8Array[] = [];
  const centralParts: Uint8Array[] = [];
  let offset = 0;
  const stamp = dosDateTime(new Date());

  for (const file of files) {
    const name = encoder.encode(file.name.replace(/\\/g, '/'));
    const data = encoder.encode(file.content);
    const checksum = crc32(data);

    const localHeader: number[] = [];
    pushUint32(localHeader, 0x04034b50);
    pushUint16(localHeader, 20);
    pushUint16(localHeader, 0x0800);
    pushUint16(localHeader, 0);
    pushUint16(localHeader, stamp.time);
    pushUint16(localHeader, stamp.date);
    pushUint32(localHeader, checksum);
    pushUint32(localHeader, data.byteLength);
    pushUint32(localHeader, data.byteLength);
    pushUint16(localHeader, name.byteLength);
    pushUint16(localHeader, 0);
    const localHeaderBytes = new Uint8Array(localHeader);
    localParts.push(localHeaderBytes, name, data);

    const centralHeader: number[] = [];
    pushUint32(centralHeader, 0x02014b50);
    pushUint16(centralHeader, 20);
    pushUint16(centralHeader, 20);
    pushUint16(centralHeader, 0x0800);
    pushUint16(centralHeader, 0);
    pushUint16(centralHeader, stamp.time);
    pushUint16(centralHeader, stamp.date);
    pushUint32(centralHeader, checksum);
    pushUint32(centralHeader, data.byteLength);
    pushUint32(centralHeader, data.byteLength);
    pushUint16(centralHeader, name.byteLength);
    pushUint16(centralHeader, 0);
    pushUint16(centralHeader, 0);
    pushUint16(centralHeader, 0);
    pushUint16(centralHeader, 0);
    pushUint32(centralHeader, 0);
    pushUint32(centralHeader, offset);
    centralParts.push(new Uint8Array(centralHeader), name);

    offset += localHeaderBytes.byteLength + name.byteLength + data.byteLength;
  }

  const centralSize = centralParts.reduce((sum, part) => sum + part.byteLength, 0);
  const end: number[] = [];
  pushUint32(end, 0x06054b50);
  pushUint16(end, 0);
  pushUint16(end, 0);
  pushUint16(end, files.length);
  pushUint16(end, files.length);
  pushUint32(end, centralSize);
  pushUint32(end, offset);
  pushUint16(end, 0);

  return new Blob([...localParts, ...centralParts, new Uint8Array(end)].map((part) => new Uint8Array(part).buffer), { type: 'application/zip' });
}
