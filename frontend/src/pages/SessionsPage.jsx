/**
 * SessionsPage - Manage 99k jump sessions
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useGuilds } from '../hooks/useGuilds';
import { sessions } from '../lib/api';
import { Card, CardContent, Badge, Button, Modal, useToast } from '../components/ui';
import { CreateSessionForm } from '../components/forms';

const STATUS_LABELS = {
  open: 'Open',
  locked: 'Locked',
  completed: 'Completed',
  cancelled: 'Cancelled',
};

export default function SessionsPage() {
  const { selectedGuildId, selectedGuild } = useGuilds();
  const { addToast } = useToast();
  const [sessionList, setSessionList] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [statusFilter, setStatusFilter] = useState('');

  const loadSessions = useCallback(async () => {
    if (!selectedGuildId) return;
    
    setLoading(true);
    setError(null);
    try {
      const params = { guild_id: selectedGuildId };
      if (statusFilter) params.status = statusFilter;
      
      const data = await sessions.list(params);
      setSessionList(data.sessions || []);
    } catch (err) {
      setError(err.message);
      addToast({ title: 'Failed to load sessions', description: err.message, variant: 'error' });
    } finally {
      setLoading(false);
    }
  }, [selectedGuildId, statusFilter, addToast]);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  const handleCreateSuccess = (newSession) => {
    setShowCreateModal(false);
    loadSessions();
    addToast({ title: 'Session created', description: `Session #${newSession?.id ?? ''} created successfully.`, variant: 'success' });
  };

  const handleSessionAction = async (sessionId, action) => {
    try {
      if (action === 'lock') await sessions.lock(sessionId);
      else if (action === 'cancel') await sessions.cancel(sessionId);
      else if (action === 'complete') await sessions.complete(sessionId);
      loadSessions();
    } catch (err) {
      addToast({ title: `Failed to ${action} session`, description: err.message, variant: 'error' });
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">99k Jump Sessions</h1>
          <p className="text-gray-400">
            {selectedGuild?.name || 'Select a server'}
          </p>
        </div>
        <Button onClick={() => setShowCreateModal(true)}>
          + Create Session
        </Button>
      </div>

      {/* Filters */}
      <div className="flex items-center space-x-4">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
        >
          <option value="">All Statuses</option>
          <option value="open">Open</option>
          <option value="locked">Locked</option>
          <option value="completed">Completed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <Button onClick={loadSessions} variant="outline" disabled={loading}>
          {loading ? 'Loading...' : 'Refresh'}
        </Button>
      </div>

      {error && (
        <div className="bg-red-500/20 border border-red-500 rounded-lg p-4 text-red-200">
          {error}
        </div>
      )}

      {/* Sessions List */}
      {sessionList.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-gray-400">No sessions found</p>
            <Button 
              onClick={() => setShowCreateModal(true)}
              variant="outline"
              className="mt-4"
            >
              Create your first session
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {sessionList.map((session) => (
            <SessionCard
              key={session.id}
              session={session}
              onAction={handleSessionAction}
            />
          ))}
        </div>
      )}

      {/* Create Modal */}
      <Modal isOpen={showCreateModal} onClose={() => setShowCreateModal(false)}>
        <CreateSessionForm
          guildId={selectedGuildId}
          onSuccess={handleCreateSuccess}
          onCancel={() => setShowCreateModal(false)}
        />
      </Modal>
    </div>
  );
}

function SessionCard({ session, onAction }) {
  const formatPayment = () => {
    const type = session.payment_type === 'xanax' ? 'Xanax' : 'DVD';
    return `${session.payment_amount}x ${type}`;
  };

  const formatStack = () => {
    const map = {
      '1_xanax': '1 Xanax',
      '2_xanax': '2 Xanax',
      '3_xanax': '3 Xanax',
      'full_stack': 'Full Stack',
    };
    return map[session.xanax_stack] || session.xanax_stack;
  };

  return (
    <Card>
      <CardContent>
        <div className="flex items-start justify-between">
          <div className="flex-1">
            <div className="flex items-center space-x-3 mb-2">
              <span className="text-lg font-semibold text-white">
                Session #{session.id}
              </span>
              <Badge status={session.status}>
                {STATUS_LABELS[session.status] || session.status}
              </Badge>
              {session.created_by_dashboard && (
                <Badge variant="info">Dashboard</Badge>
              )}
            </div>
            
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
              <div>
                <span className="text-gray-400">Payment:</span>
                <span className="text-white ml-2">{formatPayment()}</span>
              </div>
              <div>
                <span className="text-gray-400">Spots:</span>
                <span className="text-white ml-2">{session.max_spots}</span>
              </div>
              <div>
                <span className="text-gray-400">Stack:</span>
                <span className="text-white ml-2">{formatStack()}</span>
              </div>
              <div>
                <span className="text-gray-400">Host:</span>
                <span className="text-white ml-2">
                  <code className="text-xs bg-gray-700 px-1 rounded">
                    {session.host_torn_id}
                  </code>
                </span>
              </div>
            </div>

            {session.message_url && (
              <a
                href={session.message_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-purple-400 hover:text-purple-300 text-sm mt-2 inline-block"
              >
                View in Discord →
              </a>
            )}
          </div>

          {/* Actions */}
          {session.status === 'open' && (
            <div className="flex space-x-2 ml-4">
              <Button
                size="sm"
                variant="warning"
                onClick={() => onAction(session.id, 'lock')}
              >
                Lock
              </Button>
              <Button
                size="sm"
                variant="danger"
                onClick={() => onAction(session.id, 'cancel')}
              >
                Cancel
              </Button>
            </div>
          )}
          {session.status === 'locked' && (
            <div className="flex space-x-2 ml-4">
              <Button
                size="sm"
                variant="success"
                onClick={() => onAction(session.id, 'complete')}
              >
                Complete
              </Button>
              <Button
                size="sm"
                variant="danger"
                onClick={() => onAction(session.id, 'cancel')}
              >
                Cancel
              </Button>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
