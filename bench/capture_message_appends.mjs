// Capture every signed RoomMessagesChannel append during a mixed read/write trial.
import net from 'node:net';
import crypto from 'node:crypto';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) => {
  if (index % 2 === 0) pairs.push([value.slice(2), all[index + 1]]);
  return pairs;
}, []));
const base = new URL(args.base);
const cookie = args.cookie;
const socketCount = Number(args.sockets ?? 1);
const messageCount = Number(args.messages ?? 1);
const timeoutMs = Number(args.timeout ?? 60000);
if (!cookie || base.protocol !== 'http:' || !Number.isSafeInteger(socketCount) || socketCount < 1 || !Number.isSafeInteger(messageCount) || messageCount < 1) {
  throw new Error('Use --base http://host:port --cookie name=value --sockets N --messages N');
}

const response = await fetch(new URL('/rooms/1', base), { headers: { Cookie: cookie } });
if (!response.ok) throw new Error(`Room page returned ${response.status}`);
const page = await response.text();
const source = page.match(/<turbo-cable-stream-source\b[^>]*>/gi)?.find(tag => /\bchannel=(['"])RoomMessagesChannel\1/i.test(tag));
const signedName = source?.match(/\bsigned-stream-name=(['"])(.*?)\1/i)?.[2];
if (!signedName) throw new Error('Signed room stream is missing');
const identifier = JSON.stringify({ channel: 'RoomMessagesChannel', signed_stream_name: signedName });
const sockets = [];
const seen = Array.from({ length: socketCount }, () => new Set());
let received = 0;
let unexpected = 0;
let closedEarly = 0;

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
    sockets.push(socket);
    let buffer = Buffer.alloc(0);
    let handshake = false;
    let ready = false;
    const timer = setTimeout(() => reject(new Error(`Socket ${index} timed out`)), timeoutMs);
    const fail = error => {
      if (!ready) { clearTimeout(timer); reject(error); }
      else closedEarly++;
    };
    socket.on('error', fail);
    socket.on('close', () => { if (!ready) fail(new Error(`Socket ${index} closed before subscription`)); else if (seen[index].size < messageCount) closedEarly++; });
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
        if (!head.startsWith('HTTP/1.1 101')) { fail(new Error(head.split('\r\n')[0])); return; }
        buffer = buffer.subarray(end + 4);
        handshake = true;
      }
      while (buffer.length >= 2) {
        let size = buffer[1] & 127;
        let offset = 2;
        if (size === 126) { if (buffer.length < 4) break; size = buffer.readUInt16BE(2); offset = 4; }
        if (size === 127) { if (buffer.length < 10) break; size = Number(buffer.readBigUInt64BE(2)); offset = 10; }
        if (buffer.length < offset + size) break;
        const opcode = buffer[0] & 15;
        const body = buffer.subarray(offset, offset + size);
        buffer = buffer.subarray(offset + size);
        if (opcode !== 1) continue;
        let event;
        try { event = JSON.parse(body.toString()); } catch { unexpected++; continue; }
        if (event.type === 'welcome') socket.write(frame(JSON.stringify({ command: 'subscribe', identifier })));
        else if (event.type === 'confirm_subscription' && event.identifier === identifier) {
          ready = true;
          clearTimeout(timer);
          resolve();
        } else if (event.type === 'reject_subscription' && event.identifier === identifier) fail(new Error(`Socket ${index} rejected`));
        else if (event.identifier === identifier && typeof event.message === 'string') {
          const html = event.message;
          const stream = /<turbo-stream\b[^>]*\baction=["']append["'][^>]*\btarget=["']messages_rooms_open_1["']/.test(html);
          const clientId = html.match(/\bid=["']message_mixed-(\d+)["']/)?.[1];
          const messageId = html.match(/\bdata-message-id=["'](\d+)["']/)?.[1];
          const number = Number(clientId);
          if (!stream || !clientId || !Number.isSafeInteger(number) || number < 1 || number > messageCount || Number(messageId) !== 40 + number || seen[index].has(number)) {
            unexpected++;
          } else {
            seen[index].add(number);
            received++;
          }
        }
      }
    });
  });
}

try {
  for (let start = 0; start < socketCount; start += 50) {
    await Promise.all(Array.from({ length: Math.min(50, socketCount - start) }, (_, index) => connect(start + index)));
  }
  console.log('READY');
  const started = Date.now();
  const expected = socketCount * messageCount;
  while (received < expected && Date.now() - started < timeoutMs) await new Promise(resolve => setTimeout(resolve, 20));
  console.log(JSON.stringify({ sockets: socketCount, messages: messageCount, expected, received, missed: expected - received, unexpected, closed_early: closedEarly, elapsed_ms: Date.now() - started }));
  if (received !== expected || unexpected || closedEarly) process.exitCode = 1;
} finally {
  for (const socket of sockets) socket.destroy();
}
