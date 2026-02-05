/**
 * DashboardPage - Main dashboard with KPIs and quick actions
 */

import React, { useState, useEffect } from 'react';
import { useGuilds } from '../hooks/useGuilds';
import { stats } from '../lib/api';
import { Card, CardHeader, CardTitle, CardContent } from '../components/ui/Card';
import { Button } from '../components/ui';
import { Link } from 'react-router-dom';

export default function DashboardPage() {
  const { selectedGuildId, selectedGuild, hasGuilds } = useGuilds();
  const [dashboardStats, setDashboardStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (selectedGuildId) {
      loadStats();
    }
  }, [selectedGuildId]);

  const loadStats = async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await stats.get(selectedGuildId);
      setDashboardStats(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  if (!hasGuilds) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <Card className="max-w-md text-center">
          <CardContent>
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
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Dashboard</h1>
          <p className="text-gray-400">
            {selectedGuild ? selectedGuild.name : 'Select a server'}
          </p>
        </div>
        <Button onClick={loadStats} variant="outline" disabled={loading}>
          {loading ? 'Refreshing...' : 'Refresh Stats'}
        </Button>
      </div>

      {error && (
        <div className="bg-red-500/20 border border-red-500 rounded-lg p-4 text-red-200">
          {error}
        </div>
      )}

      {/* Stats Grid */}
      {dashboardStats && (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          <StatCard
            title="Active Sessions"
            value={dashboardStats.active_sessions || 0}
            icon="🪂"
            color="purple"
            link="/sessions"
          />
          <StatCard
            title="Active Raffles"
            value={dashboardStats.active_raffles || 0}
            icon="🎟️"
            color="pink"
            link="/raffles"
          />
          <StatCard
            title="Active Policies"
            value={dashboardStats.active_policies || 0}
            icon="🛡️"
            color="teal"
            link="/insurance"
          />
          <StatCard
            title="Pending Claims"
            value={dashboardStats.pending_claims || 0}
            icon="⚠️"
            color="yellow"
            link="/insurance"
          />
        </div>
      )}

      {/* Quick Actions */}
      <Card>
        <CardHeader>
          <CardTitle>Quick Actions</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <Link to="/sessions">
              <QuickActionCard
                title="Create 99k Session"
                description="Host a new happy jump session"
                icon="🪂"
                color="purple"
              />
            </Link>
            <Link to="/raffles">
              <QuickActionCard
                title="Create Raffle"
                description="Start a new prize raffle"
                icon="🎟️"
                color="pink"
              />
            </Link>
            <Link to="/settings">
              <QuickActionCard
                title="Configure Settings"
                description="Set up channels and roles"
                icon="⚙️"
                color="gray"
              />
            </Link>
          </div>
        </CardContent>
      </Card>

      {/* Recent Activity placeholder */}
      <Card>
        <CardHeader>
          <CardTitle>Recent Activity</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-gray-400 text-center py-8">
            Activity feed coming soon...
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function StatCard({ title, value, icon, color, link }) {
  const colorClasses = {
    purple: 'bg-purple-600/20 border-purple-600/50',
    pink: 'bg-pink-600/20 border-pink-600/50',
    teal: 'bg-teal-600/20 border-teal-600/50',
    yellow: 'bg-yellow-600/20 border-yellow-600/50',
    green: 'bg-green-600/20 border-green-600/50',
  };

  return (
    <Link to={link}>
      <Card 
        className={`${colorClasses[color]} hover:opacity-80 transition-opacity`}
        hover
      >
        <CardContent>
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-gray-400">{title}</p>
              <p className="text-3xl font-bold text-white mt-1">{value}</p>
            </div>
            <span className="text-3xl">{icon}</span>
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}

function QuickActionCard({ title, description, icon, color }) {
  const colorClasses = {
    purple: 'hover:border-purple-500',
    pink: 'hover:border-pink-500',
    teal: 'hover:border-teal-500',
    gray: 'hover:border-gray-500',
  };

  return (
    <div className={`bg-gray-700/50 rounded-lg p-4 border border-gray-600 ${colorClasses[color]} transition-colors cursor-pointer`}>
      <div className="flex items-center space-x-3">
        <span className="text-2xl">{icon}</span>
        <div>
          <h4 className="font-medium text-white">{title}</h4>
          <p className="text-sm text-gray-400">{description}</p>
        </div>
      </div>
    </div>
  );
}
