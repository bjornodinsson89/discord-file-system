/**
 * Happy Jumper Dashboard - API Client
 * All endpoints for communicating with FastAPI backend.
 */

const API_BASE = '/api';
const AUTH_BASE = '/auth';

function extractErrorMessage(payload, fallback) {
  if (!payload) return fallback;
  if (typeof payload === 'string') return payload;
  if (typeof payload.detail === 'string') return payload.detail;
  if (payload.detail) {
    try { return JSON.stringify(payload.detail); } catch (_) {}
  }
  if (typeof payload.message === 'string') return payload.message;
  if (payload.error && typeof payload.error === 'string') return payload.error;
  if (payload.error && typeof payload.error.message === 'string') return payload.error.message;
  try { return JSON.stringify(payload); } catch (_) { return String(payload); }
}

// ============================================================================
// HTTP Client Wrapper
// ============================================================================

async function request(url, options = {}) {
  const config = {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  };

  if (options.body && typeof options.body === 'object') {
    config.body = JSON.stringify(options.body);
  }

  const response = await fetch(url, config);

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const message = extractErrorMessage(payload, `HTTP ${response.status}`);
    const error = new Error(message);
    error.status = response.status;
    error.code = payload?.code || 'http_error';
    error.detail = payload?.detail || message;
    throw error;
  }

  return response.json();
}

export const auth = {
  getStatus: () => request(`${AUTH_BASE}/status`),
  getMe: () => request(`${AUTH_BASE}/me`),
  logout: () => window.location.href = `${AUTH_BASE}/logout`,
  login: () => window.location.href = `${AUTH_BASE}/login`,
};

export const guilds = {
  getChannels: (guildId) => request(`${API_BASE}/guilds/${guildId}/channels`),
  getRoles: (guildId) => request(`${API_BASE}/guilds/${guildId}/roles`),
  getInfo: (guildId) => request(`${API_BASE}/guilds/${guildId}`),
  getAdminGuilds: () => request(`${API_BASE}/guilds/admin`),
};

export const sessions = {
  list: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.guild_id) searchParams.append('guild_id', params.guild_id);
    if (params.status) searchParams.append('status', params.status);
    if (params.page) searchParams.append('page', params.page);
    if (params.per_page) searchParams.append('per_page', params.per_page);
    return request(`${API_BASE}/sessions/list?${searchParams}`);
  },
  get: (sessionId) => request(`${API_BASE}/sessions/${sessionId}`),
  create: (data) => request(`${API_BASE}/sessions/create`, { method: 'POST', body: data }),
  lock: (sessionId) => request(`${API_BASE}/sessions/${sessionId}/lock`, { method: 'POST' }),
  cancel: (sessionId) => request(`${API_BASE}/sessions/${sessionId}/cancel`, { method: 'POST' }),
  complete: (sessionId) => request(`${API_BASE}/sessions/${sessionId}/complete`, { method: 'POST' }),
};

export const raffles = {
  list: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.guild_id) searchParams.append('guild_id', params.guild_id);
    if (params.status) searchParams.append('status', params.status);
    if (params.page) searchParams.append('page', params.page);
    if (params.per_page) searchParams.append('per_page', params.per_page);
    return request(`${API_BASE}/raffles/list?${searchParams}`);
  },
  get: (raffleId) => request(`${API_BASE}/raffles/${raffleId}`),
  create: (data) => request(`${API_BASE}/raffles/create`, { method: 'POST', body: data }),
  draw: (raffleId) => request(`${API_BASE}/raffles/${raffleId}/draw`, { method: 'POST' }),
  cancel: (raffleId) => request(`${API_BASE}/raffles/${raffleId}/cancel`, { method: 'POST' }),
};

export const insurance = {
  listPolicies: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.guild_id) searchParams.append('guild_id', params.guild_id);
    if (params.provider_id) searchParams.append('provider_id', params.provider_id);
    return request(`${API_BASE}/insurance/policies/list?${searchParams}`);
  },
  createPolicy: (data) => request(`${API_BASE}/insurance/policy/create`, { method: 'POST', body: data }),
  listProviders: (status = null) => {
    const searchParams = new URLSearchParams();
    if (status) searchParams.append('approval_status', status);
    return request(`${API_BASE}/insurance/providers/list?${searchParams}`);
  },
  approveProvider: (providerId, status) => request(`${API_BASE}/insurance/provider/approve`, {
    method: 'POST',
    body: { provider_id: providerId, status },
  }),
  listClaims: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.status) searchParams.append('status', params.status);
    if (params.provider_id) searchParams.append('provider_id', params.provider_id);
    return request(`${API_BASE}/insurance/claims/list?${searchParams}`);
  },
  approveClaim: (claimId) => request(`${API_BASE}/insurance/claims/${claimId}/approve`, { method: 'POST' }),
  rejectClaim: (claimId, notes = '') => request(`${API_BASE}/insurance/claims/${claimId}/reject?notes=${encodeURIComponent(notes)}`, { method: 'POST' }),
};

export const settings = {
  get: (guildId) => request(`${API_BASE}/settings/${guildId}`),
  update: (data) => request(`${API_BASE}/settings/update`, { method: 'POST', body: data }),
};

export const audit = {
  list: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.guild_id) searchParams.append('guild_id', params.guild_id);
    if (params.action) searchParams.append('action', params.action);
    if (params.actor_id) searchParams.append('actor_id', params.actor_id);
    if (params.search) searchParams.append('search', params.search);
    if (params.page) searchParams.append('page', params.page);
    if (params.per_page) searchParams.append('per_page', params.per_page);
    return request(`${API_BASE}/audit/log?${searchParams}`);
  },
};

export const stats = {
  get: (guildId) => request(`${API_BASE}/stats/${guildId}`),
};

export const members = {
  list: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.guild_id) searchParams.append('guild_id', params.guild_id);
    if (params.search) searchParams.append('search', params.search);
    if (params.filter) searchParams.append('filter', params.filter);
    if (params.page) searchParams.append('page', params.page);
    if (params.per_page) searchParams.append('per_page', params.per_page);
    return request(`${API_BASE}/members/list?${searchParams}`);
  },
};

export const blacklist = {
  list: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.guild_id) searchParams.append('guild_id', params.guild_id);
    if (params.search) searchParams.append('search', params.search);
    return request(`${API_BASE}/blacklist/list?${searchParams}`);
  },
  add: (data) => request(`${API_BASE}/blacklist/add`, { method: 'POST', body: data }),
  remove: (guildId, discordId) => request(`${API_BASE}/blacklist/${guildId}/${discordId}/remove`, { method: 'POST' }),
};

export default {
  auth,
  guilds,
  sessions,
  raffles,
  insurance,
  settings,
  audit,
  stats,
  members,
  blacklist,
};
