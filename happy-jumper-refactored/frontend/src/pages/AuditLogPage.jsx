/**
 * AuditLogPage - View audit log entries
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useGuilds } from '../hooks/useGuilds';
import { audit } from '../lib/api';
import { Card, CardContent, Badge, Button } from '../components/ui';

const ACTION_LABELS = {
  api_key_registered: 'API Key Registered',
  api_key_removed: 'API Key Removed',
  session_created: 'Session Created',
  session_created_dashboard: 'Session Created (Dashboard)',
  session_locked: 'Session Locked',
  session_completed: 'Session Completed',
  session_cancelled: 'Session Cancelled',
  jump_signup: 'Jump Signup',
  jump_leave: 'Left Jump',
  raffle_created: 'Raffle Created',
  raffle_created_dashboard: 'Raffle Created (Dashboard)',
  raffle_drawn: 'Raffle Drawn',
  raffle_auto_drawn: 'Raffle Auto-Drawn',
  raffle_cancelled: 'Raffle Cancelled',
  policy_created: 'Policy Created',
  policy_created_dashboard: 'Policy Created (Dashboard)',
  claim_created: 'Claim Created',
  claim_approved: 'Claim Approved',
  claim_rejected: 'Claim Rejected',
  provider_approved: 'Provider Approved',
  provider_rejected: 'Provider Rejected',
  settings_updated: 'Settings Updated',
};

const ACTION_COLORS = {
  api_key_registered: 'info',
  api_key_removed: 'warning',
  session_created: 'success',
  session_created_dashboard: 'success',
  session_locked: 'warning',
  session_completed: 'success',
  session_cancelled: 'error',
  raffle_created: 'success',
  raffle_created_dashboard: 'success',
  raffle_drawn: 'info',
  raffle_cancelled: 'error',
  claim_approved: 'success',
  claim_rejected: 'error',
  provider_approved: 'success',
  provider_rejected: 'error',
  settings_updated: 'info',
};

export default function AuditLogPage() {
  const { selectedGuildId, selectedGuild } = useGuilds();
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [actionFilter, setActionFilter] = useState('');
  
  const perPage = 25;

  const loadAuditLog = useCallback(async () => {
    if (!selectedGuildId) return;
    
    setLoading(true);
    setError(null);
    try {
      const params = {
        guild_id: selectedGuildId,
        page,
        per_page: perPage,
      };
      if (actionFilter) params.action = actionFilter;
      
      const data = await audit.list(params);
      setEntries(data.entries || []);
      setTotal(data.total || 0);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [selectedGuildId, page, actionFilter]);

  useEffect(() => {
    loadAuditLog();
  }, [loadAuditLog]);

  useEffect(() => {
    setPage(1);
  }, [actionFilter]);

  const totalPages = Math.ceil(total / perPage);

  const formatDate = (dateStr) => {
    if (!dateStr) return 'N/A';
    const date = new Date(dateStr);
    return date.toLocaleString();
  };

  const formatPayload = (payload) => {
    if (!payload || Object.keys(payload).length === 0) return null;
    return JSON.stringify(payload, null, 2);
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Audit Log</h1>
          <p className="text-gray-400">
            Activity history for {selectedGuild?.name || 'your server'}
          </p>
        </div>
        <Button onClick={loadAuditLog} variant="outline" disabled={loading}>
          {loading ? 'Loading...' : 'Refresh'}
        </Button>
      </div>

      {/* Filters */}
      <div className="flex items-center space-x-4">
        <select
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value)}
          className="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
        >
          <option value="">All Actions</option>
          {Object.entries(ACTION_LABELS).map(([key, label]) => (
            <option key={key} value={key}>{label}</option>
          ))}
        </select>
        <span className="text-gray-400 text-sm">
          {total} total entries
        </span>
      </div>

      {error && (
        <div className="bg-red-500/20 border border-red-500 rounded-lg p-4 text-red-200">
          {error}
        </div>
      )}

      {/* Entries List */}
      {entries.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-gray-400">No audit log entries found</p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-2">
          {entries.map((entry) => (
            <Card key={entry.id} className="hover:border-gray-600 transition-colors">
              <CardContent className="py-4">
                <div className="flex items-start justify-between">
                  <div className="flex-1">
                    <div className="flex items-center space-x-3 mb-1">
                      <Badge variant={ACTION_COLORS[entry.action] || 'default'}>
                        {ACTION_LABELS[entry.action] || entry.action}
                      </Badge>
                      {entry.source && entry.source !== 'discord' && (
                        <Badge variant="info" size="sm">
                          {entry.source}
                        </Badge>
                      )}
                    </div>
                    
                    <div className="flex items-center space-x-4 text-sm text-gray-400 mt-2">
                      {entry.actor_discord_id && (
                        <span>
                          Actor: <code className="bg-gray-700 px-1 rounded text-xs">
                            {entry.actor_discord_id}
                          </code>
                        </span>
                      )}
                      {entry.target_type && (
                        <span>
                          Target: {entry.target_type} #{entry.target_id}
                        </span>
                      )}
                    </div>

                    {entry.payload && Object.keys(entry.payload).length > 0 && (
                      <details className="mt-2">
                        <summary className="text-xs text-gray-500 cursor-pointer hover:text-gray-400">
                          Show details
                        </summary>
                        <pre className="mt-2 text-xs bg-gray-900 p-2 rounded overflow-x-auto text-gray-400">
                          {formatPayload(entry.payload)}
                        </pre>
                      </details>
                    )}
                  </div>
                  
                  <span className="text-sm text-gray-500 whitespace-nowrap ml-4">
                    {formatDate(entry.created_at)}
                  </span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center space-x-4">
          <Button
            variant="outline"
            onClick={() => setPage(p => Math.max(1, p - 1))}
            disabled={page === 1 || loading}
          >
            Previous
          </Button>
          <span className="text-gray-400">
            Page {page} of {totalPages}
          </span>
          <Button
            variant="outline"
            onClick={() => setPage(p => Math.min(totalPages, p + 1))}
            disabled={page === totalPages || loading}
          >
            Next
          </Button>
        </div>
      )}
    </div>
  );
}
