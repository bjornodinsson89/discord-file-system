/**
 * BlacklistPage - Manage blacklisted users
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useGuilds } from '../hooks/useGuilds';
import { Card, CardContent, CardHeader, CardTitle, Badge, Button, Modal } from '../components/ui';

const API_BASE = '/api';

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
    const error = await response.json().catch(() => ({ detail: 'Request failed' }));
    throw new Error(error.detail || error.error || `HTTP ${response.status}`);
  }

  return response.json();
}

export default function BlacklistPage() {
  const { selectedGuildId, selectedGuild } = useGuilds();
  const [blacklist, setBlacklist] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showAddModal, setShowAddModal] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  const loadBlacklist = useCallback(async () => {
    if (!selectedGuildId) return;

    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({ guild_id: selectedGuildId });
      if (searchQuery) params.append('search', searchQuery);

      const data = await request(`${API_BASE}/blacklist/list?${params}`);
      setBlacklist(data.entries || []);
    } catch (err) {
      if (err.message.includes('404') || err.message.includes('Not Found')) {
        setBlacklist([]);
      } else {
        setError(err.message);
      }
    } finally {
      setLoading(false);
    }
  }, [selectedGuildId, searchQuery]);

  useEffect(() => {
    loadBlacklist();
  }, [loadBlacklist]);

  const handleRemove = async (entryId) => {
    if (!confirm('Are you sure you want to remove this user from the blacklist?')) return;

    try {
      await request(`${API_BASE}/blacklist/${entryId}/remove`, { method: 'POST' });
      loadBlacklist();
    } catch (err) {
      alert(`Failed to remove: ${err.message}`);
    }
  };

  const filteredBlacklist = blacklist.filter(entry => {
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      entry.discord_id?.toString().includes(query) ||
      entry.torn_user_id?.toString().includes(query) ||
      entry.reason?.toLowerCase().includes(query)
    );
  });

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-white">Blacklist</h1>
          <p className="text-gray-400 mt-1">
            Manage banned users in {selectedGuild?.name || 'your server'}
          </p>
        </div>
        <div className="flex gap-3">
          <Button onClick={loadBlacklist} variant="outline" disabled={loading}>
            {loading ? 'Loading...' : 'Refresh'}
          </Button>
          <Button onClick={() => setShowAddModal(true)} variant="danger">
            <PlusIcon className="w-4 h-4 mr-2" />
            Add to Blacklist
          </Button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="rounded-xl border bg-gradient-to-br from-red-600/20 to-red-600/5 border-red-600/30 p-6">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-gray-400">Total Blacklisted</p>
              <p className="text-3xl font-bold text-white mt-1">{blacklist.length}</p>
            </div>
            <span className="text-3xl">🚫</span>
          </div>
        </div>
        <div className="rounded-xl border bg-gradient-to-br from-yellow-600/20 to-yellow-600/5 border-yellow-600/30 p-6">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-gray-400">Temporary Bans</p>
              <p className="text-3xl font-bold text-white mt-1">
                {blacklist.filter(e => e.expires_at).length}
              </p>
            </div>
            <span className="text-3xl">⏰</span>
          </div>
        </div>
        <div className="rounded-xl border bg-gradient-to-br from-gray-600/20 to-gray-600/5 border-gray-600/30 p-6">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-gray-400">Permanent Bans</p>
              <p className="text-3xl font-bold text-white mt-1">
                {blacklist.filter(e => !e.expires_at).length}
              </p>
            </div>
            <span className="text-3xl">❌</span>
          </div>
        </div>
      </div>

      {/* Search */}
      <Card>
        <CardContent className="py-4">
          <input
            type="text"
            placeholder="Search by Discord ID, Torn ID, or reason..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white placeholder-gray-400 focus:outline-none focus:border-red-500"
          />
        </CardContent>
      </Card>

      {error && (
        <div className="bg-red-500/10 border border-red-500/50 rounded-xl p-4 text-red-300">
          {error}
        </div>
      )}

      {/* Blacklist Table */}
      {filteredBlacklist.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12">
            <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-green-600/20 flex items-center justify-center">
              <span className="text-3xl">✅</span>
            </div>
            <h3 className="text-lg font-medium text-white mb-2">
              {searchQuery ? 'No Results Found' : 'No Blacklisted Users'}
            </h3>
            <p className="text-gray-400">
              {searchQuery
                ? 'No blacklist entries match your search.'
                : 'Your blacklist is empty. Users can be blacklisted for violating rules.'}
            </p>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-gray-700">
                    <th className="text-left py-4 px-6 text-sm font-medium text-gray-400">User</th>
                    <th className="text-left py-4 px-6 text-sm font-medium text-gray-400">Reason</th>
                    <th className="text-left py-4 px-6 text-sm font-medium text-gray-400">Banned By</th>
                    <th className="text-left py-4 px-6 text-sm font-medium text-gray-400">Expires</th>
                    <th className="text-left py-4 px-6 text-sm font-medium text-gray-400">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredBlacklist.map((entry) => (
                    <BlacklistRow
                      key={entry.id || entry.discord_id}
                      entry={entry}
                      onRemove={() => handleRemove(entry.id)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Add Modal */}
      <Modal isOpen={showAddModal} onClose={() => setShowAddModal(false)} maxWidth="max-w-lg">
        <AddBlacklistForm
          guildId={selectedGuildId}
          onSuccess={() => {
            setShowAddModal(false);
            loadBlacklist();
          }}
          onCancel={() => setShowAddModal(false)}
        />
      </Modal>
    </div>
  );
}

function BlacklistRow({ entry, onRemove }) {
  const isExpired = entry.expires_at && new Date(entry.expires_at) < new Date();
  const formatDate = (dateStr) => {
    if (!dateStr) return 'Never';
    const date = new Date(dateStr);
    return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  return (
    <tr className={`border-b border-gray-700/50 hover:bg-gray-800/30 transition-colors ${isExpired ? 'opacity-50' : ''}`}>
      <td className="py-4 px-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-red-600/20 flex items-center justify-center">
            <span className="text-red-400">🚫</span>
          </div>
          <div>
            <code className="text-white text-sm">{entry.discord_id}</code>
            {entry.torn_user_id && (
              <p className="text-xs text-gray-500">
                Torn:{' '}
                <a
                  href={`https://www.torn.com/profiles.php?XID=${entry.torn_user_id}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-purple-400 hover:text-purple-300"
                >
                  {entry.torn_user_id}
                </a>
              </p>
            )}
          </div>
        </div>
      </td>
      <td className="py-4 px-6">
        <p className="text-gray-300 text-sm max-w-xs truncate" title={entry.reason}>
          {entry.reason || 'No reason provided'}
        </p>
      </td>
      <td className="py-4 px-6">
        <code className="text-gray-400 text-xs">{entry.banned_by || 'System'}</code>
      </td>
      <td className="py-4 px-6">
        {entry.expires_at ? (
          <div>
            <Badge variant={isExpired ? 'default' : 'warning'} size="sm">
              {isExpired ? 'Expired' : 'Temporary'}
            </Badge>
            <p className="text-xs text-gray-500 mt-1">{formatDate(entry.expires_at)}</p>
          </div>
        ) : (
          <Badge variant="error" size="sm">Permanent</Badge>
        )}
      </td>
      <td className="py-4 px-6">
        <Button size="sm" variant="ghost" onClick={onRemove}>
          Remove
        </Button>
      </td>
    </tr>
  );
}

function AddBlacklistForm({ guildId, onSuccess, onCancel }) {
  const [formData, setFormData] = useState({
    discord_id: '',
    reason: '',
    duration: '',
    duration_unit: 'days',
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      const payload = {
        guild_id: parseInt(guildId, 10),
        discord_id: formData.discord_id,
        reason: formData.reason || 'No reason provided',
      };

      if (formData.duration) {
        const multipliers = { hours: 1, days: 24, weeks: 168 };
        const hours = parseInt(formData.duration, 10) * multipliers[formData.duration_unit];
        const expires = new Date();
        expires.setHours(expires.getHours() + hours);
        payload.expires_at = expires.toISOString();
      }

      await request(`${API_BASE}/blacklist/add`, {
        method: 'POST',
        body: payload,
      });

      onSuccess();
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      <div>
        <h2 className="text-xl font-bold text-white mb-2">Add to Blacklist</h2>
        <p className="text-gray-400 text-sm">
          Blacklisted users cannot participate in sessions, raffles, or purchase insurance.
        </p>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/50 rounded-lg p-3 text-red-300 text-sm">
          {error}
        </div>
      )}

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-2">
          Discord User ID *
        </label>
        <input
          type="text"
          value={formData.discord_id}
          onChange={(e) => setFormData({ ...formData, discord_id: e.target.value })}
          placeholder="123456789012345678"
          required
          className="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white placeholder-gray-400 focus:outline-none focus:border-red-500"
        />
        <p className="text-xs text-gray-500 mt-1">
          Right-click the user in Discord and select "Copy User ID"
        </p>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-2">
          Reason
        </label>
        <textarea
          value={formData.reason}
          onChange={(e) => setFormData({ ...formData, reason: e.target.value })}
          placeholder="Reason for blacklisting..."
          rows={3}
          className="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white placeholder-gray-400 focus:outline-none focus:border-red-500 resize-none"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-2">
          Duration (leave empty for permanent)
        </label>
        <div className="flex gap-3">
          <input
            type="number"
            value={formData.duration}
            onChange={(e) => setFormData({ ...formData, duration: e.target.value })}
            placeholder="Duration"
            min="1"
            className="flex-1 px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white placeholder-gray-400 focus:outline-none focus:border-red-500"
          />
          <select
            value={formData.duration_unit}
            onChange={(e) => setFormData({ ...formData, duration_unit: e.target.value })}
            className="px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-red-500"
          >
            <option value="hours">Hours</option>
            <option value="days">Days</option>
            <option value="weeks">Weeks</option>
          </select>
        </div>
      </div>

      <div className="flex justify-end gap-3 pt-4 border-t border-gray-700">
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
        <Button type="submit" variant="danger" disabled={submitting}>
          {submitting ? 'Adding...' : 'Add to Blacklist'}
        </Button>
      </div>
    </form>
  );
}

function PlusIcon({ className }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
    </svg>
  );
}
