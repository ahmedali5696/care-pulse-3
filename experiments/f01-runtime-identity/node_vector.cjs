// Synthetic signer in a separate language/runtime. No application key material.
const { createHash, createHmac, randomBytes } = require('node:crypto');
const fields = [
  'carepulse-request-v1', 'http-a', 'web', 'POST', '/proof', '',
  createHash('sha256').update('').digest('hex'), 'A', 'doctor-a', 'sa',
  'DOCTOR_OWNER', '1', String(Date.now()), randomBytes(24).toString('base64url'),
  'NOTE_READ', 'p1',
];
const payload = Buffer.from(JSON.stringify(fields), 'utf8');
const input = Buffer.concat([Buffer.from('carepulse-db-context-v1\0db-a\0'), payload]);
const mac = createHmac('sha256', Buffer.alloc(32, 'A')).update(input).digest('hex');
process.stdout.write(JSON.stringify({ payload: payload.toString('base64'), mac }));
