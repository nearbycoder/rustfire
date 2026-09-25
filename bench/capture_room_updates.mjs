// Capture open-room and private-room sidebar replacements on signed streams.
import net from 'node:net';
import crypto from 'node:crypto';

const entries = process.argv.slice(2);
const args = Object.fromEntries(entries.reduce((pairs, value, index) => {
  if (index % 2 === 0) pairs.push([value.slice(2), entries[index + 1]]);
  return pairs;
}, []));
const base = new URL(args.base);
const cookie = args.cookie;
const count = Number(args.count ?? 1);
if (base.protocol !== 'http:' || !cookie || !Number.isSafeInteger(count) || count < 1) throw new Error('Use --base http://host:port --cookie name=value --count 2');
const response = await fetch(new URL('/users/me/sidebar', base), { headers: { Cookie: cookie } });
if (!response.ok) throw new Error(`Sidebar returned ${response.status}`);
const sidebar = await response.text();
const sources = [...sidebar.matchAll(/<turbo-cable-stream-source\b[^>]*>/gi)].map(match => match[0]);
function identifier(fragment) {
  const source = sources.find(tag => /channel=(['"])Turbo::StreamsChannel\1/.test(tag) && tag.includes(fragment));
  const name = source?.match(/signed-stream-name=(['"])(.*?)\1/)?.[2];
  if (!name) throw new Error(`Signed ${fragment} stream is missing`);
  return JSON.stringify({ channel: 'Turbo::StreamsChannel', signed_stream_name: name });
}
const globalId = identifier('InJvb21zIg==--');
const userId = identifier('IloybGtPaTh2');
const socket = net.createConnection({ host: base.hostname, port: Number(base.port) });
let buffer = Buffer.alloc(0);
let handshake = false;
let ready = false;
const confirmed = new Set();
const captured = { global: [], user: [] };
const timeout = setTimeout(() => { console.error('Timed out waiting for room updates'); process.exit(1); }, 30000);

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

socket.on('connect', () => {
  const key = crypto.randomBytes(16).toString('base64');
  socket.write(`GET /cable HTTP/1.1\r\nHost: ${base.host}\r\nOrigin: ${base.origin}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Protocol: actioncable-v1-json\r\nCookie: ${cookie}\r\n\r\n`);
});
socket.on('error', error => { console.error(error); process.exit(1); });
socket.on('close', () => { if (captured.global.length !== count || captured.user.length !== count) process.exitCode = 1; });
socket.on('data', chunk => {
  buffer = Buffer.concat([buffer, chunk]);
  if (!handshake) {
    const end = buffer.indexOf('\r\n\r\n');
    if (end < 0) return;
    const head = buffer.subarray(0, end).toString();
    if (!head.startsWith('HTTP/1.1 101')) throw new Error(head.split('\r\n')[0]);
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
    try { event = JSON.parse(body.toString()); } catch { continue; }
    if (event.type === 'welcome') {
      socket.write(frame(JSON.stringify({ command: 'subscribe', identifier: globalId })));
      socket.write(frame(JSON.stringify({ command: 'subscribe', identifier: userId })));
    } else if (event.type === 'confirm_subscription' && [globalId, userId].includes(event.identifier)) {
      confirmed.add(event.identifier);
      if (confirmed.size === 2 && !ready) { ready = true; console.log('READY'); }
    } else if (event.type === 'reject_subscription' && [globalId, userId].includes(event.identifier)) {
      throw new Error('Room-list subscription rejected');
    } else if (typeof event.message === 'string' && [globalId, userId].includes(event.identifier)) {
      const stream = event.identifier === globalId ? 'global' : 'user';
      if (captured[stream].length >= count) throw new Error(`Unexpected extra ${stream} event`);
      captured[stream].push(event.message);
      if (captured.global.length === count && captured.user.length === count) {
        console.log(JSON.stringify(captured));
        clearTimeout(timeout);
        socket.destroy();
      }
    }
  }
});
