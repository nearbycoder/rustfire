// Repeat an existing direct-room request while authenticated sidebar sockets receive every prepend.
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
const sockets = Number(args.sockets ?? 1000);
const requests = Number(args.requests ?? 10);
const roomId = Number(args['room-id']);
const peerIds = (args['peer-ids'] ?? '').split(',').map(Number);
const timeout = Number(args.timeout ?? 120000);
if (base.protocol !== 'http:' || !cookie || !csrf || !Number.isSafeInteger(sockets) || sockets < 1 ||
    !Number.isSafeInteger(requests) || requests < 1 || !Number.isSafeInteger(roomId) || roomId < 1 ||
    !peerIds.length || peerIds.some(id => !Number.isSafeInteger(id) || id < 1)) {
  throw new Error('Use --base http://host:port --cookie name=value --csrf token --sockets 1000 --requests 10 --room-id 1001 --peer-ids 2,3,4');
}
const sidebarResponse = await fetch(new URL('/users/me/sidebar', base), { headers: { Cookie: cookie } });
if (!sidebarResponse.ok) throw new Error(`Sidebar returned ${sidebarResponse.status}`);
const sidebar = await sidebarResponse.text();
if (!sidebar.includes(`id="list_rooms_direct_${roomId}"`)) throw new Error('Existing direct room is absent from the sidebar');
const sources = [...sidebar.matchAll(/<turbo-cable-stream-source\b[^>]*>/gi)].map(match => match[0]);
const userSource = sources.find(tag => /channel=(['"])Turbo::StreamsChannel\1/.test(tag) && /signed-stream-name=(['"])IloybGtPaTh2/.test(tag));
const signedName = userSource?.match(/signed-stream-name=(['"])(.*?)\1/)?.[2];
if (!signedName) throw new Error('Signed user room-list stream is missing');
const identifier = JSON.stringify({ channel: 'Turbo::StreamsChannel', signed_stream_name: signedName });
const clients = [];
const counts = Array(sockets).fill(0);
const sent = [];
const latencies = [];
const requestMs = [];
let phase = 'setup';
let warmReceived = 0;
let received = 0;
let unexpected = 0;
let closedEarly = 0;
let sample;
let shutdown = false;

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
    const fail = error => {
      if (!ready) { clearTimeout(timer); reject(error); }
      else if (!shutdown) closedEarly++;
    };
    socket.on('error', fail);
    socket.on('close', () => { if (!shutdown) fail(new Error(`Socket ${index} closed`)); });
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
        else if (event.type === 'confirm_subscription' && event.identifier === identifier) {
          ready = true;
          clearTimeout(timer);
          resolve();
        }
        else if (event.type === 'reject_subscription' && event.identifier === identifier) fail(new Error('User stream rejected'));
        else if (event.identifier === identifier && typeof event.message === 'string') {
          const html = event.message;
          if (!html.startsWith('<turbo-stream action="prepend" target="direct_rooms"><template>') ||
              !html.includes(`id="list_rooms_direct_${roomId}"`)) { unexpected++; continue; }
          if (phase === 'warmup') {
            if (counts[index] !== 0) unexpected++;
            else { counts[index] = 1; warmReceived++; }
          } else if (phase === 'measure') {
            const ordinal = counts[index];
            if (ordinal >= requests || sent[ordinal] === undefined) { unexpected++; continue; }
            if (sample && html !== sample) unexpected++;
            sample ??= html;
            counts[index]++;
            received++;
            latencies.push(performance.now() - sent[ordinal]);
          } else unexpected++;
        }
      }
    });
  });
}

async function reuseRoom() {
  const body = new URLSearchParams({ authenticity_token: csrf });
  for (const id of peerIds) body.append('user_ids[]', String(id));
  const response = await fetch(new URL('/rooms/directs', base), {
    method: 'POST', redirect: 'manual',
    headers: { Cookie: cookie, 'X-CSRF-Token': csrf, 'Content-Type': 'application/x-www-form-urlencoded', Connection: 'close' },
    body,
  });
  const path = new URL(response.headers.get('location') ?? '/', base).pathname;
  if (response.status !== 302 || path !== `/rooms/${roomId}` || (await response.text()).length !== 0) {
    throw new Error(`Existing direct room returned ${response.status} ${path}`);
  }
}

try {
  for (let start = 0; start < sockets; start += 50) {
    await Promise.all(Array.from({ length: Math.min(50, sockets - start) }, (_, offset) => connect(start + offset)));
  }
  phase = 'warmup';
  await reuseRoom();
  const warmStart = performance.now();
  while (warmReceived < sockets && performance.now() - warmStart < timeout) await new Promise(resolve => setTimeout(resolve, 20));
  if (warmReceived !== sockets || unexpected || closedEarly) throw new Error(`Warmup delivery ${warmReceived}/${sockets}; unexpected=${unexpected}; closed=${closedEarly}`);
  await new Promise(resolve => setTimeout(resolve, 300));
  counts.fill(0);
  phase = 'measure';
  const begin = performance.now();
  for (let ordinal = 0; ordinal < requests; ordinal++) {
    sent[ordinal] = performance.now();
    await reuseRoom();
    requestMs.push(performance.now() - sent[ordinal]);
  }
  const expected = sockets * requests;
  while (received < expected && performance.now() - begin < timeout) await new Promise(resolve => setTimeout(resolve, 20));
  const deliveryEnd = performance.now();
  await new Promise(resolve => setTimeout(resolve, 200));
  latencies.sort((a, b) => a - b);
  if (args['sample-file'] && sample) fs.writeFileSync(args['sample-file'], sample);
  const percentile = (values, fraction) => values.length ? Math.round(values[Math.ceil(values.length * fraction) - 1] * 10) / 10 : null;
  const sortedRequests = [...requestMs].sort((a, b) => a - b);
  const result = {
    sockets, requests, room_id: roomId, members: peerIds.length + 1, expected, received,
    missed: expected - received, unexpected, closed_early: closedEarly,
    elapsed_ms: Math.round(deliveryEnd - begin),
    request_p50_ms: percentile(sortedRequests, .5), request_p95_ms: percentile(sortedRequests, .95),
    event_p50_ms: percentile(latencies, .5), event_p95_ms: percentile(latencies, .95),
    event_bytes: sample ? Buffer.byteLength(sample) : 0,
    request_ms: requestMs.map(value => Math.round(value * 10) / 10),
  };
  console.log(JSON.stringify(result));
  if (result.missed || result.unexpected || result.closed_early) process.exitCode = 1;
} finally {
  shutdown = true;
  for (const socket of clients) socket.destroy();
}
