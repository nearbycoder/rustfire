// Subscribe with a previously captured signed room stream on a fresh socket.
import net from 'node:net';
import crypto from 'node:crypto';

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, value, index, all) => {
  if (index % 2 === 0) pairs.push([value.slice(2), all[index + 1]]);
  return pairs;
}, []));
const base = new URL(args.base);
if (base.protocol !== 'http:' || !args.cookie || !args['signed-name']) {
  throw new Error('Use --base http://host:port --cookie name=value --signed-name token');
}
const subscriptions = [
  { channel: 'HeartbeatChannel' },
  { channel: 'RoomMessagesChannel', signed_stream_name: args['signed-name'] },
  { channel: 'Turbo::StreamsChannel', signed_stream_name: args['signed-name'] },
  { channel: 'RoomMessagesChannel', room_id: 2 },
];
const identifiers = subscriptions.map(value => JSON.stringify(value));

function frame(value) {
  const payload = Buffer.from(value);
  const offset = payload.length < 126 ? 2 : 4;
  const packet = Buffer.alloc(offset + 4 + payload.length);
  packet[0] = 0x81;
  packet[1] = 0x80 | (offset === 2 ? payload.length : 126);
  if (offset === 4) packet.writeUInt16BE(payload.length, 2);
  const mask = crypto.randomBytes(4);
  mask.copy(packet, offset);
  for (let index = 0; index < payload.length; index++) packet[offset + 4 + index] = payload[index] ^ mask[index % 4];
  return packet;
}

const decisions = await new Promise((resolve, reject) => {
  const socket = net.createConnection({ host: base.hostname, port: Number(base.port) });
  let buffer = Buffer.alloc(0);
  let handshake = false;
  const answers = new Map();
  const timer = setTimeout(() => fail(new Error('Timed out waiting for four subscription decisions')), 10000);
  function fail(error) { clearTimeout(timer); socket.destroy(); reject(error); }
  socket.on('error', fail);
  socket.on('close', () => { if (answers.size !== identifiers.length) fail(new Error('Socket closed before all subscription decisions')); });
  socket.on('connect', () => {
    const key = crypto.randomBytes(16).toString('base64');
    socket.write(`GET /cable HTTP/1.1\r\nHost: ${base.host}\r\nOrigin: ${base.origin}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Protocol: actioncable-v1-json\r\nCookie: ${args.cookie}\r\n\r\n`);
  });
  socket.on('data', chunk => {
    buffer = Buffer.concat([buffer, chunk]);
    if (!handshake) {
      const end = buffer.indexOf('\r\n\r\n');
      if (end < 0) return;
      const status = buffer.subarray(0, end).toString().split('\r\n')[0];
      if (!status.startsWith('HTTP/1.1 101')) return fail(new Error(status));
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
      const event = JSON.parse(payload.toString());
      if (event.type === 'welcome') {
        for (const identifier of identifiers) socket.write(frame(JSON.stringify({ command: 'subscribe', identifier })));
      } else if (['confirm_subscription', 'reject_subscription'].includes(event.type) && identifiers.includes(event.identifier)) {
        answers.set(event.identifier, event.type);
        if (answers.size === identifiers.length) {
          clearTimeout(timer);
          socket.destroy();
          resolve(Object.fromEntries(identifiers.map((identifier, index) => [
            index === 3 ? 'UnsignedRoomMessagesChannel' : subscriptions[index].channel,
            answers.get(identifier),
          ])));
        }
      }
    }
  });
});
console.log(JSON.stringify(decisions));
