/**
 * MembersPage - View registered users and their API keys
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useGuilds } from '../hooks/useGuilds';
import { Card, CardContent, Badge, Button, useToast } from '../components/ui';
import { members as membersApi } from '../lib/api';

export default function MembersPage() {
  const { selectedGuildId, selectedGuild } = useGuilds();
  const { addToast } = useToast();
  const [membersList, setMembersList] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [filterType, setFilterType] = useState('all');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);

  const perPage = 20;

  const loadMembers = useCallback(async () => {
    if (!selectedGuildId) return;

    setLoading(true);
    setError(null);

    try {
      const data = await membersApi.list({
        guild_id: selectedGuildId,
        page,
        per_page: perPage,
        search: searchQuery || undefined,
        filter: filterType !== 'all' ? filterType : undefined,
      });
      setMembersList(data.members || []);
      setTotal(data.total || 0);
    } catch (err) {
      setError(err.message);
      addToast({ title: 'Failed to load members', description: err.message, variant: 'error' });
    } finally {
      setLoading(false);
    }
  }, [selectedGuildId, page, searchQuery, filterType, addToast]);

  useEffect(() => {
    loadMembers();
  }, [loadMembers]);

  const totalPages = Math.ceil(total / perPage);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-white">Members</h1>
          <p className="text-gray-400 mt-1">
            Registered users in {selectedGuild?.name || 'your server'}
          </p>
        </div>
        <Button onClick={loadMembers} variant="outline" disabled={loading}>
          {loading ? 'Loading...' : 'Refresh'}
        </Button>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <StatCard
          title="Total Registered"
          value={total}
          icon="👥"
          color="purple"
        />
        <StatCard
          title="With API Keys"
          value={membersList.filter(m => m.has_api_key).length || 0}
          icon="🔑"
          color="green"
        />
        <StatCard
          title="Active Hosts"
          value={membersList.filter(m => m.is_host).length || 0}
          icon="⭐"
          color="yellow"
        />
      </div>

      {/* Filters */}
      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col sm:flex-row gap-4">
            <div className="flex-1">
              <input
                type="text"
                placeholder="Search by Discord ID or Torn ID..."
                value={searchQuery}
                onChange={(e) => {
                  setSearchQuery(e.target.value);
                  setPage(1);
                }}
                className="w-full px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white placeholder-gray-400 focus:outline-none focus:border-purple-500"
              />
            </div>
            <select
              value={filterType}
              onChange={(e) => {
                setFilterType(e.target.value);
                setPage(1);
              }}
              className="px-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
            >
              <option value="all">All Members</option>
              <option value="with_key">With API Key</option>
              <option value="hosts">Hosts</option>
              <option value="insurers">Insurance Providers</option>
            </select>
          </div>
        </CardContent>
      </Card>

      {error && (
        <div className="bg-red-500/10 border border-red-500/50 rounded-xl p-4 text-red-300">
          {error}
        </div>
      )}

      {/* Members List */}
      {membersList.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12">
            <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-gray-700 flex items-center justify-center">
              <span className="text-3xl">👥</span>
            </div>
            <h3 className="text-lg font-medium text-white mb-2">No Members Found</h3>
            <p className="text-gray-400">
              {searchQuery
                ? 'No members match your search criteria.'
                : 'No registered members yet. Users can register by using the /set_api_key command.'}
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
                    <th className="text-left py-4 px-6 text-sm font-medium text-gray-400">Torn ID</th>
                    <th className="text-left py-4 px-6 text-sm font-medium text-gray-400">Status</th>
                    <th className="text-left py-4 px-6 text-sm font-medium text-gray-400">Stats</th>
                    <th className="text-left py-4 px-6 text-sm font-medium text-gray-400">Registered</th>
                  </tr>
                </thead>
                <tbody>
                  {membersList.map((member) => (
                    <MemberRow key={member.discord_id} member={member} />
                  ))}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-center gap-4">
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

function StatCard({ title, value, icon, color }) {
  const colors = {
    purple: 'from-purple-600/20 to-purple-600/5 border-purple-600/30',
    green: 'from-green-600/20 to-green-600/5 border-green-600/30',
    yellow: 'from-yellow-600/20 to-yellow-600/5 border-yellow-600/30',
  };

  return (
    <div className={`rounded-xl border bg-gradient-to-br p-6 ${colors[color]}`}>
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-gray-400">{title}</p>
          <p className="text-3xl font-bold text-white mt-1">{value}</p>
        </div>
        <span className="text-3xl">{icon}</span>
      </div>
    </div>
  );
}

function MemberRow({ member }) {
  return (
    <tr className="border-b border-gray-700/50 hover:bg-gray-800/30 transition-colors">
      <td className="py-4 px-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-purple-600/20 flex items-center justify-center">
            <span className="text-purple-400 font-medium">
              {member.username?.[0]?.toUpperCase() || '?'}
            </span>
          </div>
          <div>
            <p className="text-white font-medium">{member.username || 'Unknown'}</p>
            <code className="text-xs text-gray-500">{member.discord_id}</code>
          </div>
        </div>
      </td>
      <td className="py-4 px-6">
        {member.torn_user_id ? (
          <a
            href={`https://www.torn.com/profiles.php?XID=${member.torn_user_id}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-purple-400 hover:text-purple-300"
          >
            {member.torn_user_id}
          </a>
        ) : (
          <span className="text-gray-500">-</span>
        )}
      </td>
      <td className="py-4 px-6">
        <div className="flex flex-wrap gap-1">
          {member.has_api_key && (
            <Badge variant="success" size="sm">API Key</Badge>
          )}
          {member.is_host && (
            <Badge variant="purple" size="sm">Host</Badge>
          )}
          {member.is_insurer && (
            <Badge variant="teal" size="sm">Insurer</Badge>
          )}
          {!member.has_api_key && !member.is_host && !member.is_insurer && (
            <Badge variant="default" size="sm">Member</Badge>
          )}
        </div>
      </td>
      <td className="py-4 px-6">
        <div className="text-sm">
          <span className="text-gray-400">Sessions: </span>
          <span className="text-white">{member.sessions_joined || 0}</span>
          {member.sessions_hosted > 0 && (
            <>
              <span className="text-gray-400 ml-2">Hosted: </span>
              <span className="text-white">{member.sessions_hosted}</span>
            </>
          )}
        </div>
      </td>
      <td className="py-4 px-6">
        <span className="text-gray-400 text-sm">
          {member.created_at ? new Date(member.created_at).toLocaleDateString() : '-'}
        </span>
      </td>
    </tr>
  );
}
