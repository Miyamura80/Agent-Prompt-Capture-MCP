/**
 * Generates extension/icons/icon{16,32,48,128}.png.
 *
 * No image tooling is assumed: this rasterises a tiny flat-colour design into
 * an RGBA buffer and encodes a PNG by hand with node's built-in zlib.
 *
 *   node extension/scripts/make-icons.js
 *
 * The generated PNGs are checked in, so this only needs re-running when the
 * design changes.
 */

import { deflateSync } from 'node:zlib';
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const ICON_DIR = join(HERE, '..', 'icons');
const SIZES = [16, 32, 48, 128];

/* ----------------------------------------------------------------- colours */

const BG = [0x1c, 0x24, 0x3a, 0xff]; // deep slate square
const ACCENT = [0x4c, 0x8d, 0xff, 0xff]; // blue speech bubble
const DOT = [0x1c, 0x24, 0x3a, 0xff]; // bubble "text" dots, punched back to BG

/* ------------------------------------------------------------- raster core */

function makeCanvas(size) {
  return { size, data: new Uint8Array(size * size * 4) };
}

function setPx(canvas, x, y, [r, g, b, a]) {
  if (x < 0 || y < 0 || x >= canvas.size || y >= canvas.size) return;
  const i = (y * canvas.size + x) * 4;
  if (a === 255) {
    canvas.data[i] = r;
    canvas.data[i + 1] = g;
    canvas.data[i + 2] = b;
    canvas.data[i + 3] = 255;
    return;
  }
  // simple source-over blend
  const sa = a / 255;
  const da = canvas.data[i + 3] / 255;
  const oa = sa + da * (1 - sa);
  if (oa === 0) return;
  canvas.data[i] = Math.round((r * sa + canvas.data[i] * da * (1 - sa)) / oa);
  canvas.data[i + 1] = Math.round((g * sa + canvas.data[i + 1] * da * (1 - sa)) / oa);
  canvas.data[i + 2] = Math.round((b * sa + canvas.data[i + 2] * da * (1 - sa)) / oa);
  canvas.data[i + 3] = Math.round(oa * 255);
}

/** Coverage of a rounded rect at a point, sampled 3x3 for cheap antialiasing. */
function roundedRectCoverage(px, py, x0, y0, x1, y1, radius) {
  let hits = 0;
  for (let sy = 0; sy < 3; sy += 1) {
    for (let sx = 0; sx < 3; sx += 1) {
      const x = px + (sx + 0.5) / 3;
      const y = py + (sy + 0.5) / 3;
      if (x < x0 || x > x1 || y < y0 || y > y1) continue;
      const cx = Math.min(Math.max(x, x0 + radius), x1 - radius);
      const cy = Math.min(Math.max(y, y0 + radius), y1 - radius);
      const dx = x - cx;
      const dy = y - cy;
      if (dx * dx + dy * dy <= radius * radius + 1e-9) hits += 1;
    }
  }
  return hits / 9;
}

function fillRoundedRect(canvas, x0, y0, x1, y1, radius, colour) {
  const lo = Math.max(0, Math.floor(x0) - 1);
  const hi = Math.min(canvas.size, Math.ceil(x1) + 1);
  const top = Math.max(0, Math.floor(y0) - 1);
  const bot = Math.min(canvas.size, Math.ceil(y1) + 1);
  for (let y = top; y < bot; y += 1) {
    for (let x = lo; x < hi; x += 1) {
      const cov = roundedRectCoverage(x, y, x0, y0, x1, y1, radius);
      if (cov <= 0) continue;
      setPx(canvas, x, y, [colour[0], colour[1], colour[2], Math.round(colour[3] * cov)]);
    }
  }
}

function fillTriangle(canvas, pts, colour) {
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const lo = Math.max(0, Math.floor(Math.min(...xs)));
  const hi = Math.min(canvas.size, Math.ceil(Math.max(...xs)) + 1);
  const top = Math.max(0, Math.floor(Math.min(...ys)));
  const bot = Math.min(canvas.size, Math.ceil(Math.max(...ys)) + 1);
  const sign = (ax, ay, bx, by, cx, cy) => (ax - cx) * (by - cy) - (bx - cx) * (ay - cy);
  for (let y = top; y < bot; y += 1) {
    for (let x = lo; x < hi; x += 1) {
      let hits = 0;
      for (let sy = 0; sy < 3; sy += 1) {
        for (let sx = 0; sx < 3; sx += 1) {
          const px = x + (sx + 0.5) / 3;
          const py = y + (sy + 0.5) / 3;
          const d1 = sign(px, py, pts[0][0], pts[0][1], pts[1][0], pts[1][1]);
          const d2 = sign(px, py, pts[1][0], pts[1][1], pts[2][0], pts[2][1]);
          const d3 = sign(px, py, pts[2][0], pts[2][1], pts[0][0], pts[0][1]);
          const neg = d1 < 0 || d2 < 0 || d3 < 0;
          const pos = d1 > 0 || d2 > 0 || d3 > 0;
          if (!(neg && pos)) hits += 1;
        }
      }
      if (hits === 0) continue;
      setPx(canvas, x, y, [colour[0], colour[1], colour[2], Math.round(colour[3] * (hits / 9))]);
    }
  }
}

/* --------------------------------------------------------------- PNG codec */

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i += 1) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length, 0);
  const typeBuf = Buffer.from(type, 'ascii');
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])), 0);
  return Buffer.concat([length, typeBuf, data, crc]);
}

function encodePng(canvas) {
  const { size, data } = canvas;
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // colour type: RGBA
  ihdr[10] = 0; // deflate
  ihdr[11] = 0; // adaptive filtering
  ihdr[12] = 0; // no interlace

  const stride = size * 4;
  const raw = Buffer.alloc((stride + 1) * size);
  for (let y = 0; y < size; y += 1) {
    raw[y * (stride + 1)] = 0; // filter type: none
    Buffer.from(data.buffer, y * stride, stride).copy(raw, y * (stride + 1) + 1);
  }

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

/* ----------------------------------------------------------------- design */

/**
 * A rounded slate square holding a blue speech bubble with three punched-out
 * dots: "a prompt, captured".
 */
function drawIcon(size) {
  const canvas = makeCanvas(size);
  const u = size / 128; // design units

  fillRoundedRect(canvas, 0, 0, size, size, 26 * u, BG);

  const bx0 = 20 * u;
  const bx1 = 108 * u;
  const by0 = 28 * u;
  const by1 = 84 * u;
  fillRoundedRect(canvas, bx0, by0, bx1, by1, 16 * u, ACCENT);
  fillTriangle(
    canvas,
    [
      [36 * u, by1 - 6 * u],
      [62 * u, by1 - 6 * u],
      [32 * u, 104 * u],
    ],
    ACCENT,
  );

  // Three dots. Below 32px they merge into mush, so use a single bar instead.
  if (size >= 32) {
    const r = 7 * u;
    const cy = (by0 + by1) / 2;
    [40, 64, 88].forEach((cx) => {
      fillRoundedRect(canvas, cx * u - r, cy - r, cx * u + r, cy + r, r, DOT);
    });
  } else {
    const cy = (by0 + by1) / 2;
    fillRoundedRect(canvas, 36 * u, cy - 6 * u, 92 * u, cy + 6 * u, 6 * u, DOT);
  }

  return canvas;
}

mkdirSync(ICON_DIR, { recursive: true });
for (const size of SIZES) {
  const out = join(ICON_DIR, `icon${size}.png`);
  writeFileSync(out, encodePng(drawIcon(size)));
  console.log(`wrote ${out}`);
}
