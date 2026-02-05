/**
 * DashboardPage - Main dashboard with KPIs, charts, and activity feed
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useGuilds } from '../hooks/useGuilds';
import { stats, audit, sessions, raffles } from '../lib/api';
import { Card, CardHeader, CardTitle, CardContent, Badge, Button } from '../components/ui';
import { Link } from 'react-router-dom';

export default function DashboardPage() {
  const { selectedGuildId, selectedGuild, hasGuilds } = useGuilds();
  const [dashboardStats, setDashboardStats] = useState(null);
  const [recentActivity, setRecentActivity] = useState([]);
  const [recentSessions, setRecentSessions] = useState([]);
  const [recentRaffles, setRecentRaffles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const loadData = useCallback(async () => {
    if (!selectedGuildId) return;

    setLoading(true);
    setError(null);

    try {
      const [statsData, activityData, sessionsData, rafflesData] = await Promise.all([
        stats.get(selectedGuildId),
        audit.list({ guild_id: selectedGuildId, per_page: 10 }),
        sessions.list({ guild_id: selectedGuildId, per_page: 5 }),
        raffles.list({ guild_id: selectedGuildId, per_page: 5 }),
      ]);

      setDashboardStats(statsData);
      setRecentActivity(activityData.entries || []);
      setRecentSessions(sessionsData.sessions || []);
      setRecentRaffles(rafflesData.raffles || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [selectedGuildId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  if (!hasGuilds) {
    return (
      <div className="flex items-center justify-center min-h-[500px]">
        <Card className="max-w-md text-center bg-gray-800/50 backdrop-blur">
          <CardContent className="py-12">
            <div className="w-16 h-16 mx-auto mb-6 rounded-full bg-purple-600/20 flex items-center justify-center">
              <svg className="w-8 h-8 text-purple-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
              </svg>
            </div>
            <h2 className="text-xl font-bold text-white mb-2">No Server Access</h2>
            <p className="text-gray-400">
              You don't have admin access to any servers with Happy Jumper installed.
              Make sure you have Administrator permission or the configured admin role.
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-4">
        <div>
          <h1 className="text-3xl font-bold text-white">Dashboard</h1>
          <p className="text-gray-400 mt-1">
            Welcome back! Here's what's happening with {selectedGuild?.name || 'your server'}.
          </p>
        </div>
        <div className="flex gap-3">
          <Button onClick={loadData} variant="outline" disabled={loading}>
            <RefreshIcon className="w-4 h-4 mr-2" />
            {loading ? 'Refreshing...' : 'Refresh'}
          </Button>
          <Link to="/sessions">
            <Button>
              <PlusIcon className="w-4 h-4 mr-2" />
              New Session
            </Button>
          </Link>
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/50 rounded-xl p-4 text-red-300 flex items-center gap-3">
          <svg className="w-5 h-5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
            <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.707 7.293a1 1 0 00-1.414 1.414L8.586 10l-1.293 1.293a1 1 0 101.414 1.414L10 11.414l1.293 1.293a1 1 0 001.414-1.414L11.414 10l1.293-1.293a1 1 0 00-1.414-1.414L10 8.586 8.707 7.293z" clipRule="evenodd" />
          </svg>
          {error}
        </div>
      )}

      {/* Stats Grid */}
      {dashboardStats && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            title="Active Sessions"
            value={dashboardStats.active_sessions || 0}
            icon={<SessionIcon />}
            trend={dashboardStats.sessions_today}
            trendLabel="today"
            color="purple"
            link="/sessions"
          />
          <StatCard
            title="Active Raffles"
            value={dashboardStats.active_raffles || 0}
            icon={<RaffleIcon />}
            trend={dashboardStats.raffles_this_week}
            trendLabel="this week"
            color="pink"
            link="/raffles"
          />
          <StatCard
            title="Active Policies"
            value={dashboardStats.active_policies || 0}
            icon={<InsuranceIcon />}
            trend={dashboardStats.coverage_active}
            trendLabel="users covered"
            color="cyan"
            link="/insurance"
          />
          <StatCard
            title="Pending Claims"
            value={dashboardStats.pending_claims || 0}
            icon={<ClaimIcon />}
            alert={dashboardStats.pending_claims > 0}
            color="yellow"
            link="/insurance"
          />
        </div>
      )}

      {/* Secondary Stats */}
      {dashboardStats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <MiniStat label="Total Sessions" value={dashboardStats.total_sessions || 0} />
          <MiniStat label="Total Raffles" value={dashboardStats.total_raffles || 0} />
          <MiniStat label="Registered Users" value={dashboardStats.registered_users || 0} />
          <MiniStat label="Total Signups" value={dashboardStats.total_signups || 0} />
        </div>
      )}

      {/* Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Activity Feed - Takes 2 columns */}
        <div className="lg:col-span-2">
          <Card className="h-full">
            <CardHeader className="flex flex-row items-center justify-between">
              <CardTitle className="flex items-center gap-2">
                <ActivityIcon className="w-5 h-5 text-purple-400" />
                Recent Activity
              </CardTitle>
              <Link to="/audit" className="text-sm text-purple-400 hover:text-purple-300">
                View all
              </Link>
            </CardHeader>
            <CardContent>
              {recentActivity.length === 0 ? (
                <div className="text-center py-8 text-gray-500">
                  No recent activity
                </div>
              ) : (
                <div className="space-y-1">
                  {recentActivity.map((entry, index) => (
                    <ActivityItem key={entry.id || index} entry={entry} />
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Quick Actions */}
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2">
                <BoltIcon className="w-5 h-5 text-yellow-400" />
                Quick Actions
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <QuickAction
                to="/sessions"
                icon={<SessionIcon />}
                title="Create 99k Session"
                description="Host a new jump"
                color="purple"
              />
              <QuickAction
                to="/raffles"
                icon={<RaffleIcon />}
                title="Create Raffle"
                description="Start a prize raffle"
                color="pink"
              />
              <QuickAction
                to="/insurance"
                icon={<InsuranceIcon />}
                title="Manage Insurance"
                description="Policies & claims"
                color="cyan"
              />
              <QuickAction
                to="/settings"
                icon={<SettingsIcon />}
                title="Settings"
                description="Configure channels"
                color="gray"
              />
            </CardContent>
          </Card>

          {/* Active Sessions Preview */}
          {recentSessions.length > 0 && (
            <Card>
              <CardHeader className="flex flex-row items-center justify-between">
                <CardTitle className="text-sm">Active Sessions</CardTitle>
                <Link to="/sessions" className="text-xs text-purple-400 hover:text-purple-300">
                  View all
                </Link>
              </CardHeader>
              <CardContent className="space-y-2">
                {recentSessions.slice(0, 3).filter(s => s.status === 'open' || s.status === 'locked').map((session) => (
                  <div key={session.id} className="flex items-center justify-between p-2 rounded-lg bg-gray-700/30">
                    <div>
                      <span className="text-white text-sm">Session #{session.id}</span>
                      <p className="text-xs text-gray-400">{session.max_spots} spots</p>
                    </div>
                    <Badge status={session.status} size="sm">
                      {session.status}
                    </Badge>
                  </div>
                ))}
                {recentSessions.filter(s => s.status === 'open' || s.status === 'locked').length === 0 && (
                  <p className="text-gray-500 text-sm text-center py-2">No active sessions</p>
                )}
              </CardContent>
            </Card>
          )}
        </div>
      </div>

      {/* Active Raffles Preview */}
      {recentRaffles.filter(r => r.status === 'active').length > 0 && (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <RaffleIcon className="w-5 h-5 text-pink-400" />
              Active Raffles
            </CardTitle>
            <Link to="/raffles" className="text-sm text-pink-400 hover:text-pink-300">
              View all
            </Link>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {recentRaffles.filter(r => r.status === 'active').slice(0, 3).map((raffle) => (
                <RafflePreviewCard key={raffle.raffle_id} raffle={raffle} />
              ))}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

// Components

function StatCard({ title, value, icon, trend, trendLabel, color, link, alert }) {
  const colorClasses = {
    purple: 'from-purple-600/20 to-purple-600/5 border-purple-600/30 hover:border-purple-500/50',
    pink: 'from-pink-600/20 to-pink-600/5 border-pink-600/30 hover:border-pink-500/50',
    cyan: 'from-cyan-600/20 to-cyan-600/5 border-cyan-600/30 hover:border-cyan-500/50',
    yellow: 'from-yellow-600/20 to-yellow-600/5 border-yellow-600/30 hover:border-yellow-500/50',
  };

  const iconColors = {
    purple: 'text-purple-400',
    pink: 'text-pink-400',
    cyan: 'text-cyan-400',
    yellow: 'text-yellow-400',
  };

  const content = (
    <div className={`relative overflow-hidden rounded-xl border bg-gradient-to-br p-6 transition-all ${colorClasses[color]}`}>
      {alert && (
        <div className="absolute top-3 right-3">
          <span className="flex h-3 w-3">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-yellow-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-3 w-3 bg-yellow-500"></span>
          </span>
        </div>
      )}
      <div className="flex items-start justify-between">
        <div>
          <p className="text-sm text-gray-400">{title}</p>
          <p className="text-4xl font-bold text-white mt-2">{value}</p>
          {trend !== undefined && (
            <p className="text-xs text-gray-500 mt-2">
              <span className="text-green-400">+{trend}</span> {trendLabel}
            </p>
          )}
        </div>
        <div className={`p-3 rounded-xl bg-gray-800/50 ${iconColors[color]}`}>
          {icon}
        </div>
      </div>
    </div>
  );

  return link ? <Link to={link} className="block">{content}</Link> : content;
}

function MiniStat({ label, value }) {
  return (
    <div className="bg-gray-800/50 rounded-lg p-4 border border-gray-700/50">
      <p className="text-xs text-gray-500 uppercase tracking-wide">{label}</p>
      <p className="text-2xl font-semibold text-white mt-1">{value.toLocaleString()}</p>
    </div>
  );
}

function ActivityItem({ entry }) {
  const ACTION_ICONS = {
    session_created: '📅',
    session_created_dashboard: '📅',
    session_locked: '🔒',
    session_completed: '✅',
    session_cancelled: '❌',
    raffle_created: '🎟️',
    raffle_created_dashboard: '🎟️',
    raffle_drawn: '🏆',
    raffle_auto_drawn: '🏆',
    claim_created: '📋',
    claim_approved: '✅',
    claim_rejected: '❌',
    settings_updated: '⚙️',
    api_key_registered: '🔑',
    default: '📝',
  };

  const ACTION_LABELS = {
    session_created: 'Session created',
    session_created_dashboard: 'Session created via dashboard',
    session_locked: 'Session locked',
    session_completed: 'Session completed',
    session_cancelled: 'Session cancelled',
    raffle_created: 'Raffle created',
    raffle_created_dashboard: 'Raffle created via dashboard',
    raffle_drawn: 'Raffle winner drawn',
    raffle_auto_drawn: 'Raffle auto-drawn',
    claim_created: 'Claim submitted',
    claim_approved: 'Claim approved',
    claim_rejected: 'Claim rejected',
    settings_updated: 'Settings updated',
    api_key_registered: 'API key registered',
  };

  const formatTime = (dateStr) => {
    if (!dateStr) return '';
    const date = new Date(dateStr);
    const now = new Date();
    const diff = now - date;

    if (diff < 60000) return 'Just now';
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h ago`;
    return date.toLocaleDateString();
  };

  return (
    <div className="flex items-center gap-3 p-3 rounded-lg hover:bg-gray-700/30 transition-colors">
      <span className="text-lg">{ACTION_ICONS[entry.action] || ACTION_ICONS.default}</span>
      <div className="flex-1 min-w-0">
        <p className="text-sm text-white truncate">
          {ACTION_LABELS[entry.action] || entry.action}
        </p>
        {entry.target_type && (
          <p className="text-xs text-gray-500">
            {entry.target_type} #{entry.target_id}
          </p>
        )}
      </div>
      <span className="text-xs text-gray-500 whitespace-nowrap">
        {formatTime(entry.created_at)}
      </span>
    </div>
  );
}

function QuickAction({ to, icon, title, description, color }) {
  const colors = {
    purple: 'group-hover:border-purple-500 group-hover:bg-purple-600/10',
    pink: 'group-hover:border-pink-500 group-hover:bg-pink-600/10',
    cyan: 'group-hover:border-cyan-500 group-hover:bg-cyan-600/10',
    gray: 'group-hover:border-gray-500 group-hover:bg-gray-600/10',
  };

  return (
    <Link
      to={to}
      className={`group flex items-center gap-3 p-3 rounded-lg border border-gray-700 transition-all ${colors[color]}`}
    >
      <div className="p-2 rounded-lg bg-gray-700/50 text-gray-400 group-hover:text-white transition-colors">
        {icon}
      </div>
      <div>
        <h4 className="font-medium text-white text-sm">{title}</h4>
        <p className="text-xs text-gray-500">{description}</p>
      </div>
    </Link>
  );
}

function RafflePreviewCard({ raffle }) {
  const timeRemaining = () => {
    if (!raffle.end_time) return null;
    const end = new Date(raffle.end_time);
    const now = new Date();
    const diff = end - now;

    if (diff <= 0) return 'Ended';

    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));

    if (hours > 24) return `${Math.floor(hours / 24)}d ${hours % 24}h`;
    return `${hours}h ${minutes}m`;
  };

  return (
    <div className="p-4 rounded-lg bg-gradient-to-br from-pink-600/10 to-transparent border border-pink-600/30">
      <div className="flex items-start justify-between mb-2">
        <span className="text-2xl">🎟️</span>
        <Badge status="active" size="sm">Active</Badge>
      </div>
      <h4 className="font-medium text-white truncate">{raffle.prize}</h4>
      <div className="flex items-center justify-between mt-2 text-xs text-gray-400">
        <span>{raffle.tickets_sold || 0}/{raffle.tickets_available} sold</span>
        <span className="text-pink-400">{timeRemaining()}</span>
      </div>
    </div>
  );
}

// Icons

function RefreshIcon({ className }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
    </svg>
  );
}

function PlusIcon({ className }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
    </svg>
  );
}

function SessionIcon({ className = "w-6 h-6" }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
    </svg>
  );
}

function RaffleIcon({ className = "w-6 h-6" }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 5v2m0 4v2m0 4v2M5 5a2 2 0 00-2 2v3a2 2 0 110 4v3a2 2 0 002 2h14a2 2 0 002-2v-3a2 2 0 110-4V7a2 2 0 00-2-2H5z" />
    </svg>
  );
}

function InsuranceIcon({ className = "w-6 h-6" }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
    </svg>
  );
}

function ClaimIcon({ className = "w-6 h-6" }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
    </svg>
  );
}

function ActivityIcon({ className }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
    </svg>
  );
}

function BoltIcon({ className }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
    </svg>
  );
}

function SettingsIcon({ className = "w-6 h-6" }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
    </svg>
  );
}
