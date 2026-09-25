// Paired direct-room creation and signed per-user sidebar Turbo fanout probe.
import net from 'node:net';
import crypto from 'node:crypto';
import fs from 'node:fs';
import { performance } from 'node:perf_hooks';

const entries = process.argv.slice(2);
const args = Object.fromEntries(entries.reduce((pairs, value, index) => {
  if (index % 2 === 0) pairs.push([value.slice(2), entries[index + 1]]);
  return pairs;
}, []));
const base = new URL(args.base);
const cookie = args.cookie;
const csrf = args.csrf;
const sockets = Number(args.sockets ?? 200);
const rooms = Number(args.rooms ?? 10);
const warmupRooms = Number(args['warmup-rooms'] ?? 2);
const socketWarmupRooms = Number(args['socket-warmup-rooms'] ?? 2);
const settleMs = Number(args['settle-ms'] ?? 500);
const timeout = Number(args.timeout ?? 30000);
if (base.protocol !== 'http:' || !cookie || !csrf || !Number.isSafeInteger(sockets) || sockets < 1 || !Number.isSafeInteger(rooms) || rooms < 1 || warmupRooms < 0 || socketWarmupRooms < 0 || warmupRooms + socketWarmupRooms + rooms > 50) {
  throw new Error('Use --base http://host:port --cookie name=value --csrf token --sockets 200 --rooms 10 (at most 50 total rooms)');
}
const sidebarResponse = await fetch(new URL('/users/me/sidebar', base), { headers: { Cookie: cookie } });
if (!sidebarResponse.ok) throw new Error(`Sidebar returned ${sidebarResponse.status}`);
const sidebar = await sidebarResponse.text();
if (!/id=['"]direct_rooms['"]/.test(sidebar)) throw new Error('Direct-room target is missing');
const sources = [...sidebar.matchAll(/<turbo-cable-stream-source\b[^>]*>/gi)].map(match => match[0]);
const userSource = sources.find(tag => /channel=(['"])Turbo::StreamsChannel\1/.test(tag) && /signed-stream-name=(['"])IloybGtPaTh2/.test(tag));
const signedName = userSource?.match(/signed-stream-name=(['"])(.*?)\1/)?.[2];
if (!signedName) throw new Error('Signed user room-list stream is missing');
const identifier = JSON.stringify({ channel: 'Turbo::StreamsChannel', signed_stream_name: signedName });
const clients = [];
const seen = Array.from({ length: sockets }, () => new Set());
const warmSeen = Array.from({ length: sockets }, () => new Set());
const sent = new Map();
const latencies = [];
const requestMs = [];
let received = 0;
let warmReceived = 0;
let bytes = 0;
let unexpected = 0;
let closedEarly = 0;
let sample;

function frame(value) {
  const payload = Buffer.from(value);
  const head = payload.length < 126 ? 2 : 4;
  const packet = Buffer.alloc(head + 4 + payload.length);
  packet[0] = 0x81;
  packet[1] = 0x80 | (head === 2 ? payload.length : 126);
  if (head === 4) packet.writeUInt16BE(payload.length, 2);
  const mask = crypto.randomBytes(4);
  mask.copy(packet, head);
  for (let index = 0; index < payload.length; index++) packet[head + 4 + index] = payload[index] ^ mask[index % 4];
  return packet;
}

function connect(index) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: base.hostname, port: Number(base.port) });
    clients.push(socket);
    let buffer = Buffer.alloc(0);
    let handshake = false;
    let ready = false;
    const timer = setTimeout(() => reject(new Error(`Socket ${index} subscription timed out`)), timeout);
    const fail = error => { if (!ready) { clearTimeout(timer); reject(error); } else closedEarly++; };
    socket.on('error', fail);
    socket.on('close', () => { if (!ready) fail(new Error(`Socket ${index} closed before subscription`)); else closedEarly++; });
    socket.on('connect', () => {
      const key = crypto.randomBytes(16).toString('base64');
      socket.write(`GET /cable HTTP/1.1\r\nHost: ${base.host}\r\nOrigin: ${base.origin}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Protocol: actioncable-v1-json\r\nCookie: ${cookie}\r\n\r\n`);
    });
    socket.on('data', chunk => {
      buffer = Buffer.concat([buffer, chunk]);
      if (!handshake) {
        const end = buffer.indexOf('\r\n\r\n');
        if (end < 0) return;
        const head = buffer.subarray(0, end).toString();
        if (!head.startsWith('HTTP/1.1 101')) return fail(new Error(head.split('\r\n')[0]));
        buffer = buffer.subarray(end + 4);
        handshake = true;
      }
      while (buffer.length >= 2) {
        let length = buffer[1] & 127;
        let offset = 2;
        if (length === 126) { if (buffer.length < 4) break; length = buffer.readUInt16BE(2); offset = 4; }
        if (length === 127) { if (buffer.length < 10) break; length = Number(buffer.readBigUInt64BE(2)); offset = 10; }
        if (buffer.length < offset + length) break;
        const opcode = buffer[0] & 15;
        const body = buffer.subarray(offset, offset + length);
        buffer = buffer.subarray(offset + length);
        if (opcode !== 1) continue;
        let event;
        try { event = JSON.parse(body.toString()); } catch { unexpected++; continue; }
        if (event.type === 'welcome') socket.write(frame(JSON.stringify({ command: 'subscribe', identifier })));
        else if (event.type === 'confirm_subscription' && event.identifier === identifier) { ready = true; clearTimeout(timer); resolve(); }
        else if (event.type === 'reject_subscription' && event.identifier === identifier) fail(new Error('User stream rejected'));
        else if (event.identifier === identifier && typeof event.message === 'string') {
          const html = event.message;
          const match = html.match(/id="list_rooms_direct_(\d+)"/);
          const roomId = match ? Number(match[1]) : NaN;
          if (!html.startsWith('<turbo-stream action="prepend" target="direct_rooms"><template>') || !Number.isInteger(roomId)) { unexpected++; continue; }
          const warmIndex = roomId - 2 - warmupRooms;
          if (warmIndex >= 0 && warmIndex < socketWarmupRooms) {
            if (warmSeen[index].has(warmIndex)) unexpected++;
            else { warmSeen[index].add(warmIndex); warmReceived++; }
            continue;
          }
          const roomIndex = warmIndex - socketWarmupRooms;
          if (roomIndex < 0 || roomIndex >= rooms || seen[index].has(roomIndex) || !sent.has(roomIndex)) { unexpected++; continue; }
          seen[index].add(roomIndex);
          received++;
          bytes += Buffer.byteLength(html);
          latencies.push(performance.now() - sent.get(roomIndex));
          sample ??= html;
        }
      }
    });
  });
}

async function createRoom(index) {
  const userId = index + 2;
  const response = await fetch(new URL('/rooms/directs', base), {
    method: 'POST', redirect: 'manual',
    headers: { Cookie: cookie, 'X-CSRF-Token': csrf, 'Content-Type': 'application/x-www-form-urlencoded', Connection: 'close' },
    body: new URLSearchParams({ 'user_ids[]': String(userId), authenticity_token: csrf }),
  });
  if (![302, 303].includes(response.status)) throw new Error(`Create direct room with user ${userId} returned ${response.status}: ${(await response.text()).slice(0, 250)}`);
  const roomId = Number(response.headers.get('location')?.match(/\/rooms\/(\d+)/)?.[1]);
  if (roomId !== index + 2) throw new Error(`Expected direct room ${index + 2}; got ${roomId}`);
}

try {
  for (let index = 0; index < warmupRooms; index++) await createRoom(index);
  for (let start = 0; start < sockets; start += 50) {
    await Promise.all(Array.from({ length: Math.min(50, sockets - start) }, (_, index) => connect(start + index)));
  }
  for (let index = 0; index < socketWarmupRooms; index++) await createRoom(warmupRooms + index);
  const warmBegin = performance.now();
  while (warmReceived < sockets * socketWarmupRooms && performance.now() - warmBegin < timeout) await new Promise(resolve => setTimeout(resolve, 20));
  if (warmReceived !== sockets * socketWarmupRooms) throw new Error(`Socket warmup received ${warmReceived}/${sockets * socketWarmupRooms}`);
  await new Promise(resolve => setTimeout(resolve, settleMs));
  const begin = performance.now();
  for (let index = 0; index < rooms; index++) {
    sent.set(index, performance.now());
    await createRoom(warmupRooms + socketWarmupRooms + index);
    requestMs.push(Math.round(performance.now() - sent.get(index)));
  }
  const expected = sockets * rooms;
  while (received < expected && performance.now() - begin < timeout) await new Promise(resolve => setTimeout(resolve, 20));
  latencies.sort((a, b) => a - b);
  if (args['sample-file'] && sample) fs.writeFileSync(args['sample-file'], sample);
  const percentile = fraction => latencies.length ? Math.round(latencies[Math.floor((latencies.length - 1) * fraction)]) : null;
  const result = {
    sockets, rooms, warmup_rooms: warmupRooms, socket_warmup_rooms: socketWarmupRooms, settle_ms: settleMs,
    expected, received, missed: expected - received, unexpected, closed_early: closedEarly,
    elapsed_ms: Math.round(performance.now() - begin), average_event_bytes: received ? Math.round(bytes / received) : 0,
    p50_ms: percentile(.5), p95_ms: percentile(.95), p99_ms: percentile(.99), request_ms: requestMs,
  };
  console.log(JSON.stringify(result));
  if (result.missed || unexpected || closedEarly) process.exitCode = 1;
} finally {
  for (const socket of clients) socket.destroy();
}
