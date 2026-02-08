import assert from 'node:assert/strict';

global.document = {
  cookie: '',
};

let capturedConfig = null;
global.fetch = async (_url, config) => {
  capturedConfig = config;
  return {
    ok: true,
    async json() {
      return { ok: true };
    },
  };
};

const api = await import('../src/lib/api.js');
api.setCsrfToken('session-token-123');

await api.settings.update({ guild_id: 1, key: 'value' });

assert.equal(capturedConfig.credentials, 'include');
assert.equal(capturedConfig.headers['X-CSRF-Token'], 'session-token-123');
assert.equal(capturedConfig.method, 'POST');

console.log('CSRF header + credentials check passed');
