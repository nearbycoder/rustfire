// WebSocket fanout probe for Rustfire JSON, Rustfire Turbo, and Campfire Turbo streams. Use a valid browser session cookie.
// node bench/fanout.mjs --app rustfire --base http://127.0.0.1:3000 --cookie session_token=... --sockets 100 --messages 20
import net from 'node:net';
import crypto from 'node:crypto';
import { performance } from 'node:perf_hooks';
import fs from 'node:fs';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, item, i, all) => {
  if (i % 2 === 0) pairs.push([item.slice(2), all[i + 1]]);
  return pairs;
}, []));
const base = new URL(args.base ?? 'http://127.0.0.1:3000');
const app = args.app ?? 'rustfire';
const cookie = args.cookie;
const room = Number(args.room ?? 1);
const messageId = Number(args['message-id'] ?? 1);
const operation = args.operation ?? 'messages';
const socketCount = Number(args.sockets ?? 100);
const messageCount = Number(args.messages ?? 20);
const timeoutMs = Number(args.timeout ?? 30000);
if (!cookie || !['rustfire','rustfire-turbo','campfire'].includes(app) || !['messages','boosts'].includes(operation) || !Number.isSafeInteger(room) || room < 1 || !Number.isSafeInteger(messageId) || messageId < 1 || !Number.isSafeInteger(socketCount) || socketCount < 1 || !Number.isSafeInteger(messageCount) || messageCount < 1 || base.protocol !== 'http:') {
  console.error('Use --app rustfire|rustfire-turbo|campfire --base http://host:port --cookie name=value [--operation messages|boosts --message-id 1 --room 1 --sockets 100 --messages 20]');
  process.exit(2);
}
let identifier = JSON.stringify({ channel: 'RoomMessagesChannel', room_id: room });
const expectedTarget = operation === 'boosts' ? 'boosts_message_boost-fixture' : `messages_rooms_open_${room}`;
let csrf = args.csrf;
if (app !== 'rustfire' || !csrf) {
  const roomResponse = await fetch(new URL(`/rooms/${room}`, base), { headers: { Cookie: cookie } });
  if (!roomResponse.ok) throw new Error(`Room page returned ${roomResponse.status}`);
  const html = await roomResponse.text();
  csrf ??= html.match(/<meta name=['"]csrf-token['"] content=['"]([^'"]+)/i)?.[1];
  if (app !== 'rustfire') {
    if (!html.includes(`id='${expectedTarget}'`) && !html.includes(`id="${expectedTarget}"`)) throw new Error(`Room page lacks Turbo target ${expectedTarget}`);
    const source = html.match(/<turbo-cable-stream-source\b[^>]*>/gi)?.find(tag => /\bchannel=(['"])RoomMessagesChannel\1/i.test(tag));
    const signedName = source?.match(/\bsigned-stream-name=(['"])(.*?)\1/i)?.[2];
    if (!signedName) throw new Error('Room stream name was not found');
    identifier = JSON.stringify({ channel: 'RoomMessagesChannel', signed_stream_name: signedName });
  }
}
const samples = [];
const sent = new Map();
let received = 0;
let receivedBytes = 0;
let sampleMessage;
let unexpected = 0;
let closedEarly = 0;
const clients = [];

function maskedTextFrame(value) {
  const data = Buffer.from(value);
  const header = data.length < 126 ? 2 : data.length < 65536 ? 4 : 10;
  const packet = Buffer.alloc(header + 4 + data.length);
  packet[0] = 0x81;
  packet[1] = 0x80 | (header === 2 ? data.length : header === 4 ? 126 : 127);
  if (header === 4) packet.writeUInt16BE(data.length, 2);
  if (header === 10) packet.writeBigUInt64BE(BigInt(data.length), 2);
  const mask = crypto.randomBytes(4);
  mask.copy(packet, header);
  for (let i = 0; i < data.length; i++) packet[header + 4 + i] = data[i] ^ mask[i % 4];
  return packet;
}

function connect(index) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: base.hostname, port: Number(base.port || 80) });
    clients.push(socket);
    let buffer = Buffer.alloc(0);
    let handshake = false;
    let ready = false;
    const timer = setTimeout(() => reject(new Error(`socket ${index} timed out`)), timeoutMs);
    const fail = error => { if (!ready) { clearTimeout(timer); reject(error); } else { closedEarly++; } };
    socket.on('error', fail);
    socket.on('close', () => { if (!ready) fail(new Error(`socket ${index} closed`)); else closedEarly++; });
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
        if (!head.startsWith('HTTP/1.1 101')) return fail(new Error(`socket ${index} handshake: ${head.split('\r\n')[0]}`));
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
        const payload = buffer.subarray(offset, offset + length);
        buffer = buffer.subarray(offset + length);
        if (opcode !== 1) continue;
        let event;
        try { event = JSON.parse(payload.toString()); } catch { unexpected++; continue; }
        if (event.type === 'welcome') socket.write(maskedTextFrame(JSON.stringify({ command: 'subscribe', identifier })));
        else if (event.type === 'confirm_subscription') { ready = true; clearTimeout(timer); resolve(socket); }
        else if (operation === 'boosts' ? (event.message?.type === 'boost' || typeof event.message === 'string' && /<turbo-stream\b[^>]*action="append"[^>]*target="boosts[_-]/.test(event.message)) : (event.message?.type === 'message' || typeof event.message === 'string' && /<turbo-stream\b[^>]*action="append"/.test(event.message))) {
          if (typeof event.message === 'string' && !event.message.includes(`target="${expectedTarget}"`)) { unexpected++; continue; }
          const body = operation === 'boosts' ? (app === 'rustfire' ? event.message.content : event.message) : (app === 'rustfire' ? event.message.message?.body?.plain_text : event.message);
          const id = operation === 'boosts' ? (typeof body === 'string' ? body.match(/z[0-9a-f]{8}/)?.[0] : undefined)
            : app === 'rustfire' && typeof body === 'string' && body.startsWith('fanout ') ? body.slice(7)
            : typeof body === 'string' ? body.match(/fanout ([\w-]+)/)?.[1] : undefined;
          const start = sent.get(id);
          if (start === undefined) unexpected++;
          else { received++; receivedBytes += Buffer.byteLength(typeof event.message === 'string' ? event.message : JSON.stringify(event.message)); sampleMessage ??= event.message; samples.push(performance.now() - start); }
        }
      }
    });
  });
}

try {
  // Batches avoid a connect storm dominating the delivery measurement.
  for (let start = 0; start < socketCount; start += 50) {
    await Promise.all(Array.from({ length: Math.min(50, socketCount - start) }, (_, i) => connect(start + i)));
  }
  const runId = crypto.randomUUID();
  const begin = performance.now();
  for (let i = 0; i < messageCount; i++) {
    const id = operation === 'boosts' ? `z${crypto.randomBytes(4).toString('hex')}` : `${runId}-${i}`;
    sent.set(id, performance.now());
    const response = await fetch(new URL(operation === 'boosts' ? `/messages/${messageId}/boosts` : `/rooms/${room}/messages`, base), {
      method: 'POST',
      redirect: 'manual',
      headers: { Cookie: cookie, Accept: app === 'rustfire' ? 'application/json' : 'text/vnd.turbo-stream.html, text/html', 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ ...(operation === 'boosts' ? { 'boost[content]': id } : { 'message[body]': `fanout ${id}`, 'message[client_message_id]': id }), ...(csrf ? { authenticity_token: csrf } : {}) }),
    });
    if (!(operation === 'boosts' ? [302,303].includes(response.status) : response.ok)) throw new Error(`POST ${i} returned ${response.status}`);
  }
  const expected = socketCount * messageCount;
  while (received < expected && performance.now() - begin < timeoutMs) await new Promise(resolve => setTimeout(resolve, 20));
  samples.sort((a, b) => a - b);
  const percentile = fraction => samples.length ? samples[Math.floor((samples.length - 1) * fraction)].toFixed(2) : 'n/a';
  const elapsed = ((performance.now() - begin) / 1000).toFixed(2);
  if (args['sample-file'] && sampleMessage !== undefined) fs.writeFileSync(args['sample-file'], typeof sampleMessage === 'string' ? sampleMessage : JSON.stringify(sampleMessage, null, 2));
  console.log(`operation=${operation} sockets=${socketCount} messages=${messageCount} expected_deliveries=${expected} received=${received} missed=${expected - received} unexpected=${unexpected} avg_message_bytes=${received ? Math.round(receivedBytes / received) : 0} elapsed_s=${elapsed} p50_ms=${percentile(.50)} p95_ms=${percentile(.95)} p99_ms=${percentile(.99)}`);
  if (received !== expected || closedEarly) process.exitCode = 1;
} finally {
  for (const socket of clients) socket.destroy();
}
