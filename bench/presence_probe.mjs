// Run one authenticated Action Cable presence sequence and report saved state.
import net from 'node:net';
import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';

const args = Object.fromEntries(process.argv.slice(2).reduce((items, value, index, all) => {
  if (index % 2 === 0) items.push([value.slice(2), all[index + 1]]);
  return items;
}, []));
const base = new URL(args.base);
const cookie = args.cookie;
const database = args.database;
if (base.protocol !== 'http:' || !cookie || !database) throw new Error('Use --base URL --cookie COOKIE --database PATH');

function sql(statement, parameters = []) {
  const program = 'import json,sqlite3,sys;db=sqlite3.connect(sys.argv[1]);statement=sys.argv[2];parameters=json.loads(sys.argv[3]);cursor=db.execute(statement,parameters);row=cursor.fetchone() if statement.lower().startswith("select") else None;db.commit();print(json.dumps(row))';
  return JSON.parse(execFileSync('python', ['-c', program, database, statement, JSON.stringify(parameters)], { encoding: 'utf8' }));
}
const state = () => sql('select connections,connected_at,unread_at from memberships where room_id=1 and user_id=1');
async function until(check, description) {
  for (let index = 0; index < 100; index++) {
    if (check()) return;
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  throw new Error(`Timed out waiting for ${description}: ${JSON.stringify(state())}`);
}
function maskFrame(value) {
  const payload = Buffer.from(JSON.stringify(value));
  const header = payload.length < 126 ? 2 : 4;
  const frame = Buffer.alloc(header + 4 + payload.length);
  frame[0] = 0x81;
  frame[1] = 0x80 | (header === 2 ? payload.length : 126);
  if (header === 4) frame.writeUInt16BE(payload.length, 2);
  const mask = crypto.randomBytes(4);
  mask.copy(frame, header);
  for (let index = 0; index < payload.length; index++) frame[header + 4 + index] = payload[index] ^ mask[index % 4];
  return frame;
}

const socket = net.createConnection({ host: base.hostname, port: Number(base.port) });
const frames = [];
const waiters = [];
let buffer = Buffer.alloc(0);
let handshake = false;
let failed;
function deliver(value) {
  if (value.type === 'ping') return;
  const index = waiters.findIndex(waiter => waiter.predicate(value));
  if (index >= 0) {
    const [waiter] = waiters.splice(index, 1);
    waiter.resolve(value);
  } else frames.push(value);
}
function waitFor(predicate, description) {
  const index = frames.findIndex(predicate);
  if (index >= 0) return Promise.resolve(frames.splice(index, 1)[0]);
  return new Promise((resolve, reject) => {
    const waiter = { predicate, resolve, reject };
    waiters.push(waiter);
    const timer = setTimeout(() => {
      const position = waiters.indexOf(waiter);
      if (position >= 0) waiters.splice(position, 1);
      reject(new Error(`Timed out waiting for ${description}; frames=${JSON.stringify(frames)}`));
    }, 5000);
    waiter.resolve = value => { clearTimeout(timer); resolve(value); };
  });
}
socket.on('error', error => { failed = error; for (const waiter of waiters.splice(0)) waiter.reject(error); });
socket.on('data', chunk => {
  buffer = Buffer.concat([buffer, chunk]);
  if (!handshake) {
    const end = buffer.indexOf('\r\n\r\n');
    if (end < 0) return;
    const head = buffer.subarray(0, end).toString();
    if (!head.startsWith('HTTP/1.1 101')) {
      failed = new Error(head.split('\r\n')[0]);
      for (const waiter of waiters.splice(0)) waiter.reject(failed);
      return;
    }
    buffer = buffer.subarray(end + 4);
    handshake = true;
  }
  while (buffer.length >= 2) {
    const opcode = buffer[0] & 15;
    let size = buffer[1] & 127;
    let offset = 2;
    if (size === 126) { if (buffer.length < 4) break; size = buffer.readUInt16BE(2); offset = 4; }
    if (size === 127) { if (buffer.length < 10) break; size = Number(buffer.readBigUInt64BE(2)); offset = 10; }
    if (buffer.length < offset + size) break;
    const payload = buffer.subarray(offset, offset + size);
    buffer = buffer.subarray(offset + size);
    if (opcode === 1) deliver(JSON.parse(payload.toString()));
  }
});
socket.on('connect', () => {
  const key = crypto.randomBytes(16).toString('base64');
  socket.write(`GET /cable HTTP/1.1\r\nHost: ${base.host}\r\nOrigin: ${base.origin}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Protocol: actioncable-v1-json\r\nCookie: ${cookie}\r\n\r\n`);
});
const send = command => { if (failed) throw failed; socket.write(maskFrame(command)); };
const read = JSON.stringify({ channel: 'ReadRoomsChannel' });
const first = JSON.stringify({ channel: 'PresenceChannel', room_id: 1 });
const second = JSON.stringify({ channel: 'PresenceChannel', room_id: 1, tab: 'second' });
const confirm = identifier => waitFor(frame => frame.identifier === identifier && frame.type === 'confirm_subscription', `confirmation for ${identifier}`);
const readEvent = () => waitFor(frame => frame.identifier === read && frame.message?.room_id === 1, 'room read event');

try {
  await waitFor(frame => frame.type === 'welcome', 'Action Cable welcome');
  send({ command: 'subscribe', identifier: read });
  await confirm(read);
  send({ command: 'subscribe', identifier: first });
  await confirm(first);
  await readEvent();
  await until(() => state()[0] === 1, 'first subscription');
  send({ command: 'message', identifier: second, data: JSON.stringify({ action: 'absent' }) });
  send({ command: 'subscribe', identifier: second });
  await confirm(second);
  await readEvent();
  await until(() => state()[0] === 2, 'second subscription');

  const marker = '2025-01-01 00:00:00';
  sql('update memberships set unread_at=? where room_id=1 and user_id=1', [marker]);
  const before = state()[1];
  send({ command: 'message', identifier: first, data: JSON.stringify({ action: 'refresh' }) });
  await until(() => state()[1] !== before, 'refreshed timestamp');
  const refreshed = state();
  if (refreshed[0] !== 2 || refreshed[2] !== marker) throw new Error(`Refresh changed count or unread marker: ${JSON.stringify(refreshed)}`);
  await new Promise(resolve => setTimeout(resolve, 250));
  if (frames.some(frame => frame.identifier === read)) throw new Error('Presence refresh broadcast a read event');

  send({ command: 'unsubscribe', identifier: second });
  await until(() => state()[0] === 1, 'second unsubscribe');
  send({ command: 'unsubscribe', identifier: first });
  await until(() => state()[0] === 0, 'first unsubscribe');
  const ended = state();
  if (ended[1] !== null || ended[2] !== marker) throw new Error(`Unsubscribe changed unread marker or remained connected: ${JSON.stringify(ended)}`);
  console.log(JSON.stringify({ connections: [1, 2, 2, 1, 0], read_events: 2, refresh_preserved_unread: true, refresh_read_event: false, final_unread: marker }));
} finally {
  socket.destroy();
}
