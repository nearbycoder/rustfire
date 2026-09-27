// Capture one signed RoomMessagesChannel presentation replacement.
import net from 'node:net';
import crypto from 'node:crypto';

const entries = process.argv.slice(2);
const args = Object.fromEntries(entries.reduce((pairs, value, index) => {
  if (index % 2 === 0) pairs.push([value.slice(2), entries[index + 1]]);
  return pairs;
}, []));
const base = new URL(args.base);
const cookie = args.cookie;
const room = Number(args.room ?? 1);
const target = args.target;
if (base.protocol !== 'http:' || !cookie || !Number.isSafeInteger(room) || room < 1 || !/^presentation_message_[\w-]+$/.test(target ?? '')) {
  throw new Error('Use --base http://host:port --cookie name=value --room 1 --target presentation_message_client-id');
}
const response = await fetch(new URL(`/rooms/${room}`, base), { headers: { Cookie: cookie } });
if (!response.ok) throw new Error(`Room page returned ${response.status}`);
const page = await response.text();
const source = page.match(/<turbo-cable-stream-source\b[^>]*>/gi)?.find(tag => /\bchannel=(['"])RoomMessagesChannel\1/i.test(tag));
const signedName = source?.match(/\bsigned-stream-name=(['"])(.*?)\1/i)?.[2];
if (!signedName) throw new Error('Signed room stream is missing');
const identifier = JSON.stringify({ channel: 'RoomMessagesChannel', signed_stream_name: signedName });
const socket = net.createConnection({ host: base.hostname, port: Number(base.port) });
let buffer = Buffer.alloc(0);
let handshake = false;
let ready = false;
let captured = false;
const timeout = setTimeout(() => { console.error('Timed out waiting for message replacement'); process.exit(1); }, 30000);

function frame(value) {
  const payload = Buffer.from(value);
  const header = payload.length < 126 ? 2 : 4;
  const packet = Buffer.alloc(header + 4 + payload.length);
  packet[0] = 0x81;
  packet[1] = 0x80 | (header === 2 ? payload.length : 126);
  if (header === 4) packet.writeUInt16BE(payload.length, 2);
  const mask = crypto.randomBytes(4);
  mask.copy(packet, header);
  for (let index = 0; index < payload.length; index++) packet[header + 4 + index] = payload[index] ^ mask[index % 4];
  return packet;
}

socket.on('connect', () => {
  const key = crypto.randomBytes(16).toString('base64');
  socket.write(`GET /cable HTTP/1.1\r\nHost: ${base.host}\r\nOrigin: ${base.origin}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Protocol: actioncable-v1-json\r\nCookie: ${cookie}\r\n\r\n`);
});
socket.on('error', error => { console.error(error); process.exit(1); });
socket.on('close', () => { if (!captured) process.exitCode = 1; });
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
    if (event.type === 'welcome') socket.write(frame(JSON.stringify({ command: 'subscribe', identifier })));
    else if (event.type === 'confirm_subscription' && event.identifier === identifier && !ready) {
      ready = true;
      console.log('READY');
    } else if (event.type === 'reject_subscription' && event.identifier === identifier) {
      throw new Error('Room subscription rejected');
    } else if (ready && event.identifier === identifier && typeof event.message === 'string') {
      if (!event.message.includes(`target="${target}"`)) throw new Error(`Unexpected room event: ${event.message.slice(0, 150)}`);
      captured = true;
      console.log(JSON.stringify(event.message));
      clearTimeout(timeout);
      socket.destroy();
    }
  }
});
