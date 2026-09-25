// Paired shared-room creation or update and signed sidebar Turbo fanout probe.
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
const operation = args.operation ?? 'create';
const sockets = Number(args.sockets ?? 200);
const rooms = Number(args.rooms ?? 10);
const warmupRooms = Number(args['warmup-rooms'] ?? 2);
const socketWarmupRooms = Number(args['socket-warmup-rooms'] ?? 2);
const settleMs = Number(args['settle-ms'] ?? 500);
const timeout = Number(args.timeout ?? 30000);
const runId = args['run-id'] ?? crypto.randomUUID();
if (base.protocol !== 'http:' || !cookie || !csrf || !['create', 'update'].includes(operation) || !Number.isSafeInteger(sockets) || sockets < 1 || !Number.isSafeInteger(rooms) || rooms < 1 || !Number.isSafeInteger(warmupRooms) || warmupRooms < 0 || !Number.isSafeInteger(socketWarmupRooms) || socketWarmupRooms < 0 || !Number.isFinite(settleMs) || settleMs < 0) {
  throw new Error('Use --base http://host:port --cookie name=value --csrf token --sockets 200 --rooms 10');
}
const sidebarResponse = await fetch(new URL('/users/me/sidebar', base), { headers: { Cookie: cookie } });
if (!sidebarResponse.ok) throw new Error(`Sidebar returned ${sidebarResponse.status}`);
const sidebar = await sidebarResponse.text();
if (!/id=['"]shared_rooms['"]/.test(sidebar)) throw new Error('Shared-room target is missing');
const sources = [...sidebar.matchAll(/<turbo-cable-stream-source\b[^>]*>/gi)].map(match => match[0]);
const globalSource = sources.find(tag => /channel=(['"])Turbo::StreamsChannel\1/.test(tag) && /signed-stream-name=(['"])InJvb21zIg==--/.test(tag));
const signedName = globalSource?.match(/signed-stream-name=(['"])(.*?)\1/)?.[2];
if (!signedName) throw new Error('Signed global room-list stream is missing');
const identifier = JSON.stringify({ channel: 'Turbo::StreamsChannel', signed_stream_name: signedName });
const clients = [];
const seen = Array.from({ length: sockets }, () => new Set());
const warmSeen = Array.from({ length: sockets }, () => new Set());
const sent = new Map();
const latencies = [];
const requestMs = [];
const roomLatency = Array.from({ length: rooms }, () => []);
let received = 0;
let bytes = 0;
let unexpected = 0;
let closedEarly = 0;
let sample;
let warmReceived = 0;
let updateRoomId;

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
        else if (event.type === 'reject_subscription' && event.identifier === identifier) fail(new Error('Sidebar stream rejected'));
        else if (event.identifier === identifier && typeof event.message === 'string') {
          const html = event.message;
          const warm = html.match(/sidebar-socket-warmup-([a-f\d-]+)-(\d+)/);
          if (warm && warm[1] === runId) {
            const warmIndex = Number(warm[2]);
            if (warmIndex >= 0 && warmIndex < socketWarmupRooms && !warmSeen[index].has(warmIndex)) {
              warmSeen[index].add(warmIndex);
              warmReceived++;
            } else unexpected++;
            continue;
          }
          const match = html.match(/sidebar-fanout-([a-f\d-]+)-(\d+)/);
          const roomIndex = match && match[1] === runId ? Number(match[2]) : NaN;
          const expectedTarget = operation === 'create' ? 'target="shared_rooms"' : `target="list_rooms_open_${updateRoomId}"`;
          if (!html.includes(`action="${operation === 'create' ? 'prepend' : 'replace'}"`) || !html.includes(expectedTarget) || !html.includes('list_rooms_open_') || !Number.isInteger(roomIndex) || roomIndex < 0 || roomIndex >= rooms || seen[index].has(roomIndex) || !sent.has(roomIndex)) {
            unexpected++;
            continue;
          }
          seen[index].add(roomIndex);
          received++;
          bytes += Buffer.byteLength(html);
          const latency = performance.now() - sent.get(roomIndex);
          latencies.push(latency);
          roomLatency[roomIndex].push(latency);
          sample ??= html;
        }
      }
    });
  });
}

async function createRoom(name) {
  const response = await fetch(new URL('/rooms/opens', base), {
    method: 'POST', redirect: 'manual',
    headers: { Cookie: cookie, 'X-CSRF-Token': csrf, 'Content-Type': 'application/x-www-form-urlencoded', Connection: 'close' },
    body: new URLSearchParams({ 'room[name]': name, authenticity_token: csrf }),
  });
  if (![302, 303].includes(response.status)) throw new Error(`Create room ${name} returned ${response.status}: ${(await response.text()).slice(0, 250)}`);
  return Number(new URL(response.headers.get('location'), base).pathname.match(/^\/rooms\/(\d+)$/)?.[1]);
}

async function updateRoom(name) {
  const response = await fetch(new URL(`/rooms/opens/${updateRoomId}`, base), {
    method: 'POST', redirect: 'manual',
    headers: { Cookie: cookie, 'X-CSRF-Token': csrf, 'Content-Type': 'application/x-www-form-urlencoded', Connection: 'close' },
    body: new URLSearchParams({ _method: 'patch', 'room[name]': name, authenticity_token: csrf }),
  });
  if (response.status !== 302 || new URL(response.headers.get('location'), base).pathname !== `/rooms/${updateRoomId}`) {
    throw new Error(`Update room ${name} returned ${response.status}: ${(await response.text()).slice(0, 250)}`);
  }
}

try {
  if (operation === 'update') {
    updateRoomId = await createRoom(`sidebar-update-fixture-${runId}`);
    if (!Number.isSafeInteger(updateRoomId) || updateRoomId < 1) throw new Error('Update fixture room ID missing');
  }
  const mutate = operation === 'create' ? createRoom : updateRoom;
  for (let index = 0; index < warmupRooms; index++) await mutate(`sidebar-warmup-${runId}-${index}`);
  for (let start = 0; start < sockets; start += 50) {
    await Promise.all(Array.from({ length: Math.min(50, sockets - start) }, (_, index) => connect(start + index)));
  }
  for (let index = 0; index < socketWarmupRooms; index++) await mutate(`sidebar-socket-warmup-${runId}-${index}`);
  const warmBegin = performance.now();
  while (warmReceived < sockets * socketWarmupRooms && performance.now() - warmBegin < timeout) await new Promise(resolve => setTimeout(resolve, 20));
  if (warmReceived !== sockets * socketWarmupRooms) throw new Error(`Socket warmup received ${warmReceived}/${sockets * socketWarmupRooms}`);
  await new Promise(resolve => setTimeout(resolve, settleMs));
  const begin = performance.now();
  for (let index = 0; index < rooms; index++) {
    sent.set(index, performance.now());
    await mutate(`sidebar-fanout-${runId}-${index}`);
    requestMs.push(Math.round(performance.now() - sent.get(index)));
  }
  const expected = sockets * rooms;
  while (received < expected && performance.now() - begin < timeout) await new Promise(resolve => setTimeout(resolve, 20));
  latencies.sort((a, b) => a - b);
  if (args['sample-file'] && sample) fs.writeFileSync(args['sample-file'], sample);
  const result = {
    operation, sockets, rooms, warmup_rooms: warmupRooms, socket_warmup_rooms: socketWarmupRooms, settle_ms: settleMs, expected, received, missed: expected - received, unexpected, closed_early: closedEarly,
    elapsed_ms: Math.round(performance.now() - begin), average_event_bytes: received ? Math.round(bytes / received) : 0,
    p50_ms: latencies.length ? Math.round(latencies[Math.floor((latencies.length - 1) * .5)]) : null,
    p95_ms: latencies.length ? Math.round(latencies[Math.floor((latencies.length - 1) * .95)]) : null,
    p99_ms: latencies.length ? Math.round(latencies[Math.floor((latencies.length - 1) * .99)]) : null,
    request_ms: requestMs,
    room_p95_ms: roomLatency.map(values => {
      values.sort((a, b) => a - b);
      return values.length ? Math.round(values[Math.floor((values.length - 1) * .95)]) : null;
    }),
  };
  console.log(JSON.stringify(result));
  if (result.missed || unexpected || closedEarly) process.exitCode = 1;
} finally {
  for (const socket of clients) socket.destroy();
}
