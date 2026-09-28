// Observe Action Cable frames and the WebSocket close after administrator revocation.
import net from 'node:net';
import crypto from 'node:crypto';

const entries = process.argv.slice(2);
const args = Object.fromEntries(entries.reduce((pairs, value, index) => {
  if (index % 2 === 0) pairs.push([value.slice(2), entries[index + 1]]);
  return pairs;
}, []));
const base = new URL(args.base);
const cookie = args.cookie;
const unauthorized = args.unauthorized === 'true';
if (base.protocol !== 'http:' || !cookie) throw new Error('Use --base http://host:port --cookie name=value');
const identifier = JSON.stringify({ channel: 'HeartbeatChannel' });
const socket = net.createConnection({ host: base.hostname, port: Number(base.port) });
const messages = [];
let buffer = Buffer.alloc(0);
let handshake = false;
let ready = false;
let httpStatus = null;
let closeCode = null;
let closeReason = '';
let tcpEnded = false;
const timeout = setTimeout(() => {
  console.error('Timed out waiting for revocation close');
  process.exitCode = 2;
  socket.destroy();
}, 15000);

function frame(value, opcode = 1) {
  const payload = Buffer.isBuffer(value) ? value : Buffer.from(value);
  const mask = crypto.randomBytes(4);
  const head = payload.length < 126 ? 2 : 4;
  const packet = Buffer.alloc(head + 4 + payload.length);
  packet[0] = 0x80 | opcode;
  packet[1] = 0x80 | (head === 2 ? payload.length : 126);
  if (head === 4) packet.writeUInt16BE(payload.length, 2);
  mask.copy(packet, head);
  for (let index = 0; index < payload.length; index++) packet[head + 4 + index] = payload[index] ^ mask[index % 4];
  return packet;
}

socket.on('connect', () => {
  const key = crypto.randomBytes(16).toString('base64');
  socket.write(`GET /cable HTTP/1.1\r\nHost: ${base.host}\r\nOrigin: ${base.origin}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Protocol: actioncable-v1-json\r\nCookie: ${cookie}\r\n\r\n`);
});
socket.on('error', error => { console.error(error); process.exit(1); });
socket.on('end', () => { tcpEnded = true; });
socket.on('close', () => {
  clearTimeout(timeout);
  console.log(JSON.stringify({ ready, http_status: httpStatus, messages, close_code: closeCode, close_reason: closeReason, tcp_ended: tcpEnded }));
  if (!ready && !unauthorized) process.exitCode = 1;
});
socket.on('data', chunk => {
  buffer = Buffer.concat([buffer, chunk]);
  if (!handshake) {
    const end = buffer.indexOf('\r\n\r\n');
    if (end < 0) return;
    const head = buffer.subarray(0, end).toString();
    httpStatus = Number(head.match(/^HTTP\/1\.1 (\d+)/)?.[1]);
    if (httpStatus !== 101) {
      if (!unauthorized) throw new Error(head.split('\r\n')[0]);
      socket.destroy();
      return;
    }
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
    if (opcode === 8) {
      closeCode = body.length >= 2 ? body.readUInt16BE(0) : null;
      closeReason = body.subarray(2).toString();
      socket.end(frame(body, 8));
    } else if (opcode === 1) {
      const event = JSON.parse(body.toString());
      if (event.type === 'welcome' && !unauthorized) socket.write(frame(JSON.stringify({ command: 'subscribe', identifier })));
      else if (event.type === 'confirm_subscription' && event.identifier === identifier) {
        ready = true;
        console.log('READY');
      } else if (event.type !== 'ping') messages.push(event);
    }
  }
});
