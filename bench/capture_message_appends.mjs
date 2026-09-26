// Capture every signed RoomMessagesChannel append during a mixed read/write trial.
import net from 'node:net';
import crypto from 'node:crypto';
import fs from 'node:fs';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) => {
  if (index % 2 === 0) pairs.push([value.slice(2), all[index + 1]]);
  return pairs;
}, []));
const base = new URL(args.base);
const cookie = args.cookie;
const socketCount = Number(args.sockets ?? 1);
const messageCount = Number(args.messages ?? 1);
const roomId = Number(args.room ?? 1);
const clientPrefix = args['client-prefix'] ?? 'mixed';
const firstId = Number(args['first-id'] ?? 41);
const timeoutMs = Number(args.timeout ?? 60000);
const browserChannels = args['browser-channels'] === '1';
if (!cookie || base.protocol !== 'http:' || !Number.isSafeInteger(socketCount) || socketCount < 1 || !Number.isSafeInteger(messageCount) || messageCount < 1 || !Number.isSafeInteger(roomId) || roomId < 1 || !Number.isSafeInteger(firstId) || firstId < 0 || !/^[A-Za-z0-9-]+$/.test(clientPrefix)) {
  throw new Error('Use --base http://host:port --cookie name=value --sockets N --messages N [--room N --client-prefix PREFIX --first-id N]');
}

const response = await fetch(new URL(`/rooms/${roomId}`, base), { headers: { Cookie: cookie } });
if (!response.ok) throw new Error(`Room page returned ${response.status}`);
const page = await response.text();
const source = page.match(/<turbo-cable-stream-source\b[^>]*>/gi)?.find(tag => /\bchannel=(['"])RoomMessagesChannel\1/i.test(tag));
const signedName = source?.match(/\bsigned-stream-name=(['"])(.*?)\1/i)?.[2];
if (!signedName) throw new Error('Signed room stream is missing');
const identifier = JSON.stringify({ channel: 'RoomMessagesChannel', signed_stream_name: signedName });
const readIdentifier = JSON.stringify({ channel: 'ReadRoomsChannel' });
const unreadIdentifier = JSON.stringify({ channel: 'UnreadRoomsChannel' });
let browserIdentifiers = [];
if (browserChannels) {
  const sidebarResponse = await fetch(new URL('/users/me/sidebar', base), { headers: { Cookie: cookie } });
  if (!sidebarResponse.ok) throw new Error(`Sidebar returned ${sidebarResponse.status}`);
  const sidebar = await sidebarResponse.text();
  const names = [...sidebar.matchAll(/<turbo-cable-stream-source\b[^>]*channel=(['"])Turbo::StreamsChannel\1[^>]*signed-stream-name=(['"])(.*?)\2/gi)].map(match => match[3]);
  if (names.length !== 2) throw new Error(`Expected two signed sidebar streams, found ${names.length}`);
  browserIdentifiers = [
    identifier,
    JSON.stringify({ channel: 'TypingNotificationsChannel', room_id: roomId }),
    JSON.stringify({ channel: 'PresenceChannel', room_id: roomId }),
    unreadIdentifier,
    JSON.stringify({ channel: 'HeartbeatChannel' }),
    ...names.map(name => JSON.stringify({ channel: 'Turbo::StreamsChannel', signed_stream_name: name })),
  ];
}
const streamPattern = new RegExp(`<turbo-stream\\b[^>]*\\baction=["']append["'][^>]*\\btarget=["']messages_rooms_open_${roomId}["']`);
const clientPattern = new RegExp(`\\bid=["']message_${clientPrefix}-(\\d+)["']`);
const sockets = [];
const seen = Array.from({ length: socketCount }, () => new Set());
const unreadSeen = Array(socketCount).fill(0);
const readSeen = Array(socketCount).fill(0);
const events = args['events-file'] ? Array(messageCount).fill(null) : null;
let received = 0;
let unexpected = 0;
let closedEarly = 0;
let sampled = false;
let unreadReceived = 0;
let readReceived = 0;

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
    const confirmed = new Set();
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
        if (event.type === 'welcome') socket.write(frame(JSON.stringify({ command: 'subscribe', identifier: browserChannels ? readIdentifier : identifier })));
        else if (event.type === 'confirm_subscription' && (event.identifier === identifier || browserChannels && (event.identifier === readIdentifier || browserIdentifiers.includes(event.identifier)))) {
          confirmed.add(event.identifier);
          if (browserChannels && event.identifier === readIdentifier) {
            for (const next of browserIdentifiers) socket.write(frame(JSON.stringify({ command: 'subscribe', identifier: next })));
          }
          if (!ready && confirmed.size === (browserChannels ? browserIdentifiers.length + 1 : 1)) {
            ready = true;
            clearTimeout(timer);
            resolve();
          }
        } else if (event.type === 'reject_subscription') fail(new Error(`Socket ${index} rejected ${event.identifier}`));
        else if (browserChannels && event.identifier === readIdentifier && event.message?.room_id === roomId) {
          readSeen[index]++;
          readReceived++;
        } else if (browserChannels && event.identifier === unreadIdentifier && event.message?.roomId === roomId) {
          unreadSeen[index]++;
          unreadReceived++;
          if (unreadSeen[index] > messageCount) unexpected++;
        }
        else if (event.identifier === identifier && typeof event.message === 'string') {
          const html = event.message;
          const stream = streamPattern.test(html);
          const clientId = html.match(clientPattern)?.[1];
          const messageId = html.match(/\bdata-message-id=["'](\d+)["']/)?.[1];
          const number = Number(clientId);
          if (!stream || !clientId || !Number.isSafeInteger(number) || number < 1 || number > messageCount || !Number.isSafeInteger(Number(messageId)) || Number(messageId) < 1 || (firstId && Number(messageId) !== firstId + number - 1) || seen[index].has(number)) {
            unexpected++;
          } else {
            seen[index].add(number);
            received++;
            if (index === 0 && number === 1 && args['sample-file'] && !sampled) {
              fs.writeFileSync(args['sample-file'], html);
              sampled = true;
            }
            if (index === 0 && events) events[number - 1] = html;
          }
        }
      }
    });
  });
}

try {
  for (let start = 0; start < socketCount; start += browserChannels ? 1 : 50) {
    await Promise.all(Array.from({ length: Math.min(browserChannels ? 1 : 50, socketCount - start) }, (_, index) => connect(start + index)));
  }
  console.log('READY');
  const started = Date.now();
  const expected = socketCount * messageCount;
  const expectedRead = browserChannels ? socketCount * (socketCount + 1) / 2 : 0;
  while ((received < expected || browserChannels && unreadReceived < expected) && Date.now() - started < timeoutMs) await new Promise(resolve => setTimeout(resolve, 20));
  if (received === expected && (!browserChannels || unreadReceived === expected)) await new Promise(resolve => setTimeout(resolve, 100));
  if (events) fs.writeFileSync(args['events-file'], JSON.stringify(events));
  console.log(JSON.stringify({ sockets: socketCount, messages: messageCount, expected, received, missed: expected - received, unexpected, closed_early: closedEarly, sampled, browser_channels: browserChannels, unread_received: unreadReceived, read_expected: expectedRead, read_received: readReceived, elapsed_ms: Date.now() - started }));
  if (received !== expected || browserChannels && (unreadReceived !== expected || readReceived !== expectedRead || readSeen.some(count => count < 1)) || unexpected || closedEarly || (args['sample-file'] && !sampled) || (events && events.some(event => event === null))) process.exitCode = 1;
} finally {
  for (const socket of sockets) socket.destroy();
}
