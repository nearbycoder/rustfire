// Subscribe to a room and verify every banned-content Turbo remove event.
import net from 'node:net';
import crypto from 'node:crypto';
import fs from 'node:fs';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) => {
  if (index % 2 === 0) pairs.push([value.slice(2), all[index + 1]]);
  return pairs;
}, []));
const base = new URL(args.base);
const cookie = args.cookie;
const sockets = Number(args.sockets ?? 1);
const messages = Number(args.messages ?? 55);
const deadlineMs = Number(args.timeout ?? 30000);
if (!cookie || base.protocol !== 'http:' || !Number.isSafeInteger(sockets) || sockets < 1 || !Number.isSafeInteger(messages) || messages < 1) {
  throw new Error('Use --base http://host:port --cookie name=value --sockets 1 --messages 55');
}
const roomResponse = await fetch(new URL('/rooms/1', base), { headers: { Cookie: cookie } });
if (!roomResponse.ok) throw new Error(`Room page returned ${roomResponse.status}`);
const roomPage = await roomResponse.text();
const source = roomPage.match(/<turbo-cable-stream-source\b[^>]*>/gi)?.find(tag => /\bchannel=(['"])RoomMessagesChannel\1/i.test(tag));
const signedName = source?.match(/\bsigned-stream-name=(['"])(.*?)\1/i)?.[2];
if (!signedName) throw new Error('Signed room stream is missing');
const identifier = JSON.stringify({ channel: 'RoomMessagesChannel', signed_stream_name: signedName });
const clients = [];
const seen = Array.from({ length: sockets }, () => new Set());
let received = 0;
let unexpected = 0;
let sample;

function frame(value) {
  const data = Buffer.from(value);
  const offset = data.length < 126 ? 2 : 4;
  const packet = Buffer.alloc(offset + 4 + data.length);
  packet[0] = 0x81;
  packet[1] = 0x80 | (offset === 2 ? data.length : 126);
  if (offset === 4) packet.writeUInt16BE(data.length, 2);
  const mask = crypto.randomBytes(4);
  mask.copy(packet, offset);
  for (let index = 0; index < data.length; index++) packet[offset + 4 + index] = data[index] ^ mask[index % 4];
  return packet;
}

function connect(index) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection({ host: base.hostname, port: Number(base.port || 80) });
    clients.push(socket);
    let data = Buffer.alloc(0);
    let handshake = false;
    let ready = false;
    const timer = setTimeout(() => reject(new Error(`Socket ${index} timed out during subscription`)), deadlineMs);
    socket.once('error', reject);
    socket.once('close', () => { if (!ready) reject(new Error(`Socket ${index} closed before subscription`)); });
    socket.once('connect', () => {
      const key = crypto.randomBytes(16).toString('base64');
      socket.write(`GET /cable HTTP/1.1\r\nHost: ${base.host}\r\nOrigin: ${base.origin}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Protocol: actioncable-v1-json\r\nCookie: ${cookie}\r\n\r\n`);
    });
    socket.on('data', chunk => {
      data = Buffer.concat([data, chunk]);
      if (!handshake) {
        const end = data.indexOf('\r\n\r\n');
        if (end < 0) return;
        const head = data.subarray(0, end).toString();
        if (!head.startsWith('HTTP/1.1 101')) { reject(new Error(head.split('\r\n')[0])); return; }
        data = data.subarray(end + 4);
        handshake = true;
      }
      while (data.length >= 2) {
        let size = data[1] & 127;
        let offset = 2;
        if (size === 126) { if (data.length < 4) break; size = data.readUInt16BE(2); offset = 4; }
        if (size === 127) { if (data.length < 10) break; size = Number(data.readBigUInt64BE(2)); offset = 10; }
        if (data.length < offset + size) break;
        const opcode = data[0] & 15;
        const body = data.subarray(offset, offset + size);
        data = data.subarray(offset + size);
        if (opcode !== 1) continue;
        let event;
        try { event = JSON.parse(body.toString()); } catch { unexpected++; continue; }
        if (event.type === 'welcome') socket.write(frame(JSON.stringify({ command: 'subscribe', identifier })));
        else if (event.type === 'confirm_subscription') { ready = true; clearTimeout(timer); resolve(); }
        else if (typeof event.message === 'string' && event.identifier === identifier) {
          const match = event.message.match(/^<turbo-stream action="remove" target="message_banned-(\d+)"><\/turbo-stream>$/);
          if (!match || Number(match[1]) < 1 || Number(match[1]) > messages || seen[index].has(Number(match[1]))) {
            unexpected++;
          } else {
            seen[index].add(Number(match[1]));
            received++;
            sample ??= event.message;
          }
        }
      }
    });
  });
}

try {
  for (let start = 0; start < sockets; start += 50) {
    await Promise.all(Array.from({ length: Math.min(50, sockets - start) }, (_, index) => connect(start + index)));
  }
  console.log('READY');
  const expected = sockets * messages;
  const begin = Date.now();
  while (received < expected && Date.now() - begin < deadlineMs) await new Promise(resolve => setTimeout(resolve, 20));
  if (args['sample-file'] && sample) fs.writeFileSync(args['sample-file'], sample);
  console.log(JSON.stringify({ sockets, messages, expected, received, missed: expected - received, unexpected, elapsed_ms: Date.now() - begin }));
  if (received !== expected || unexpected) process.exitCode = 1;
} finally {
  for (const socket of clients) socket.destroy();
}
