/**
 * RafflesPage - Manage raffles
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useGuilds } from '../hooks/useGuilds';
import { raffles } from '../lib/api';
import { Card, CardContent, Badge, Button, Modal, useToast } from '../components/ui';
import { CreateRaffleForm } from '../components/forms';

const STATUS_LABELS = {
  active: 'Active',
  drawing: 'Drawing',
  completed: 'Completed',
  cancelled: 'Cancelled',
};

export default function RafflesPage() {
  const { selectedGuildId, selectedGuild } = useGuilds();
  const { addToast } = useToast();
  const [raffleList, setRaffleList] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [statusFilter, setStatusFilter] = useState('');

  const loadRaffles = useCallback(async () => {
    if (!selectedGuildId) return;
    
    setLoading(true);
    setError(null);
    try {
      const params = { guild_id: selectedGuildId };
      if (statusFilter) params.status = statusFilter;
      
      const data = await raffles.list(params);
      setRaffleList(data.raffles || []);
    } catch (err) {
      setError(err.message);
      addToast({ title: 'Failed to load raffles', description: err.message, variant: 'error' });
    } finally {
      setLoading(false);
    }
  }, [selectedGuildId, statusFilter, addToast]);

  useEffect(() => {
    loadRaffles();
  }, [loadRaffles]);

  const handleCreateSuccess = () => {
    setShowCreateModal(false);
    loadRaffles();
    addToast({ title: 'Raffle created', description: 'Your raffle was created successfully.', variant: 'success' });
  };

  const handleRaffleAction = async (raffleId, action) => {
    try {
      if (action === 'draw') {
        if (!confirm('Are you sure you want to draw the winner now?')) return;
        await raffles.draw(raffleId);
      } else if (action === 'cancel') {
        if (!confirm('Are you sure you want to cancel this raffle?')) return;
        await raffles.cancel(raffleId);
      }
      loadRaffles();
    } catch (err) {
      addToast({ title: `Failed to ${action} raffle`, description: err.message, variant: 'error' });
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Raffles</h1>
          <p className="text-gray-400">
            {selectedGuild?.name || 'Select a server'}
          </p>
        </div>
        <Button variant="pink" onClick={() => setShowCreateModal(true)}>
          + Create Raffle
        </Button>
      </div>

      {/* Filters */}
      <div className="flex items-center space-x-4">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-pink-500"
        >
          <option value="">All Statuses</option>
          <option value="active">Active</option>
          <option value="completed">Completed</option>
          <option value="cancelled">Cancelled</option>
        </select>
        <Button onClick={loadRaffles} variant="outline" disabled={loading}>
          {loading ? 'Loading...' : 'Refresh'}
        </Button>
      </div>

      {error && (
        <div className="bg-red-500/20 border border-red-500 rounded-lg p-4 text-red-200">
          {error}
        </div>
      )}

      {/* Raffles List */}
      {raffleList.length === 0 ? (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-gray-400">No raffles found</p>
            <Button 
              onClick={() => setShowCreateModal(true)}
              variant="pink"
              className="mt-4"
            >
              Create your first raffle
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-4">
          {raffleList.map((raffle) => (
            <RaffleCard
              key={raffle.raffle_id}
              raffle={raffle}
              onAction={handleRaffleAction}
            />
          ))}
        </div>
      )}

      {/* Create Modal */}
      <Modal isOpen={showCreateModal} onClose={() => setShowCreateModal(false)}>
        <CreateRaffleForm
          guildId={selectedGuildId}
          onSuccess={handleCreateSuccess}
          onCancel={() => setShowCreateModal(false)}
        />
      </Modal>
    </div>
  );
}

function RaffleCard({ raffle, onAction }) {
  const formatPayment = () => {
    const type = raffle.ticket_payment_type === 'xanax' ? 'Xanax' : 'DVD';
    return `${raffle.ticket_price}x ${type}`;
  };

  const formatDate = (dateStr) => {
    if (!dateStr) return 'N/A';
    const date = new Date(dateStr);
    return date.toLocaleString();
  };

  const timeRemaining = () => {
    if (!raffle.end_time) return null;
    const end = new Date(raffle.end_time);
    const now = new Date();
    const diff = end - now;
    
    if (diff <= 0) return 'Ended';
    
    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
    
    if (hours > 24) {
      return `${Math.floor(hours / 24)}d ${hours % 24}h remaining`;
    }
    return `${hours}h ${minutes}m remaining`;
  };

  return (
    <Card className="border-pink-600/30">
      <CardContent>
        <div className="flex items-start justify-between">
          <div className="flex-1">
            <div className="flex items-center space-x-3 mb-2">
              <span className="text-xl">🎟️</span>
              <span className="text-lg font-semibold text-white">
                {raffle.prize}
              </span>
              <Badge status={raffle.status}>
                {STATUS_LABELS[raffle.status] || raffle.status}
              </Badge>
            </div>
            
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm mt-3">
              <div>
                <span className="text-gray-400">Ticket Price:</span>
                <span className="text-white ml-2">{formatPayment()}</span>
              </div>
              <div>
                <span className="text-gray-400">Total Tickets:</span>
                <span className="text-white ml-2">{raffle.tickets_available}</span>
              </div>
              <div>
                <span className="text-gray-400">Max Per User:</span>
                <span className="text-white ml-2">
                  {raffle.max_tickets_per_user || '∞'}
                </span>
              </div>
              <div>
                <span className="text-gray-400">Ends:</span>
                <span className="text-white ml-2">{formatDate(raffle.end_time)}</span>
              </div>
            </div>

            {raffle.status === 'active' && (
              <p className="text-pink-400 text-sm mt-2">{timeRemaining()}</p>
            )}

            {raffle.winner_discord_id && (
              <p className="text-green-400 text-sm mt-2">
                Winner: <code className="bg-gray-700 px-1 rounded">{raffle.winner_discord_id}</code>
              </p>
            )}

            {raffle.message_url && (
              <a
                href={raffle.message_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-pink-400 hover:text-pink-300 text-sm mt-2 inline-block"
              >
                View in Discord →
              </a>
            )}
          </div>

          {/* Actions */}
          {raffle.status === 'active' && (
            <div className="flex space-x-2 ml-4">
              <Button
                size="sm"
                variant="success"
                onClick={() => onAction(raffle.raffle_id, 'draw')}
              >
                Draw Now
              </Button>
              <Button
                size="sm"
                variant="danger"
                onClick={() => onAction(raffle.raffle_id, 'cancel')}
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
