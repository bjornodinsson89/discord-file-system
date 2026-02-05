/**
 * Happy Jumper Dashboard - API Client
 * All endpoints for communicating with FastAPI backend.
 */

const API_BASE = '/api';
const AUTH_BASE = '/auth';

// ============================================================================
// HTTP Client Wrapper
// ============================================================================

async function request(url, options = {}) {
  const config = {
    credentials: 'include',  // Include cookies for session auth
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
    const error = await response.json().catch(() => ({ detail: 'Request failed' }));
    throw new Error(error.detail || error.error || `HTTP ${response.status}`);
  }
  
  return response.json();
}

// ============================================================================
// Authentication
// ============================================================================

export const auth = {
  getStatus: () => request(`${AUTH_BASE}/status`),
  getMe: () => request(`${AUTH_BASE}/me`),
  logout: () => window.location.href = `${AUTH_BASE}/logout`,
  login: () => window.location.href = `${AUTH_BASE}/login`,
};

// ============================================================================
// Guilds
// ============================================================================

export const guilds = {
  getChannels: (guildId) => request(`${API_BASE}/guilds/${guildId}/channels`),
  getRoles: (guildId) => request(`${API_BASE}/guilds/${guildId}/roles`),
  getInfo: (guildId) => request(`${API_BASE}/guilds/${guildId}`),
};

// ============================================================================
// Sessions (99k Jumps)
// ============================================================================

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
  
  create: (data) => request(`${API_BASE}/sessions/create`, {
    method: 'POST',
    body: data,
  }),
  
  lock: (sessionId) => request(`${API_BASE}/sessions/${sessionId}/lock`, {
    method: 'POST',
  }),
  
  cancel: (sessionId) => request(`${API_BASE}/sessions/${sessionId}/cancel`, {
    method: 'POST',
  }),
  
  complete: (sessionId) => request(`${API_BASE}/sessions/${sessionId}/complete`, {
    method: 'POST',
  }),
};

// ============================================================================
// Raffles
// ============================================================================

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
  
  create: (data) => request(`${API_BASE}/raffles/create`, {
    method: 'POST',
    body: data,
  }),
  
  draw: (raffleId) => request(`${API_BASE}/raffles/${raffleId}/draw`, {
    method: 'POST',
  }),
  
  cancel: (raffleId) => request(`${API_BASE}/raffles/${raffleId}/cancel`, {
    method: 'POST',
  }),
};

// ============================================================================
// Insurance
// ============================================================================

export const insurance = {
  // Policies
  listPolicies: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.guild_id) searchParams.append('guild_id', params.guild_id);
    if (params.provider_id) searchParams.append('provider_id', params.provider_id);
    return request(`${API_BASE}/insurance/policies/list?${searchParams}`);
  },
  
  createPolicy: (data) => request(`${API_BASE}/insurance/policy/create`, {
    method: 'POST',
    body: data,
  }),
  
  // Providers
  listProviders: (status = null) => {
    const searchParams = new URLSearchParams();
    if (status) searchParams.append('approval_status', status);
    return request(`${API_BASE}/insurance/providers/list?${searchParams}`);
  },
  
  approveProvider: (providerId, status) => request(`${API_BASE}/insurance/provider/approve`, {
    method: 'POST',
    body: { provider_id: providerId, status },
  }),
  
  // Claims
  listClaims: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.status) searchParams.append('status', params.status);
    if (params.provider_id) searchParams.append('provider_id', params.provider_id);
    return request(`${API_BASE}/insurance/claims/list?${searchParams}`);
  },
  
  approveClaim: (claimId) => request(`${API_BASE}/insurance/claims/${claimId}/approve`, {
    method: 'POST',
  }),
  
  rejectClaim: (claimId, notes = '') => request(`${API_BASE}/insurance/claims/${claimId}/reject?notes=${encodeURIComponent(notes)}`, {
    method: 'POST',
  }),
};

// ============================================================================
// Settings
// ============================================================================

export const settings = {
  get: (guildId) => request(`${API_BASE}/settings/${guildId}`),
  
  update: (data) => request(`${API_BASE}/settings/update`, {
    method: 'POST',
    body: data,
  }),
};

// ============================================================================
// Audit Log
// ============================================================================

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

// ============================================================================
// Statistics
// ============================================================================

export const stats = {
  get: (guildId) => request(`${API_BASE}/stats/${guildId}`),
};

// ============================================================================
// Members
// ============================================================================

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

// ============================================================================
// Blacklist
// ============================================================================

export const blacklist = {
  list: (params = {}) => {
    const searchParams = new URLSearchParams();
    if (params.guild_id) searchParams.append('guild_id', params.guild_id);
    if (params.search) searchParams.append('search', params.search);
    return request(`${API_BASE}/blacklist/list?${searchParams}`);
  },
  add: (data) => request(`${API_BASE}/blacklist/add`, {
    method: 'POST',
    body: data,
  }),
  remove: (guildId, discordId) => request(`${API_BASE}/blacklist/${guildId}/${discordId}/remove`, {
    method: 'POST',
  }),
};

// ============================================================================
// Default Export
// ============================================================================

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
