/**
 * CreateRaffleForm - Form for creating raffles
 */

import React, { useState } from 'react';
import { useChannels } from '../../hooks/useChannels';
import { raffles } from '../../lib/api';

const PAYMENT_TYPE_OPTIONS = [
  { value: 'xanax', label: 'Xanax' },
  { value: 'erotic_dvd', label: 'Erotic DVD' },
];

export default function CreateRaffleForm({ guildId, onSuccess, onCancel }) {
  const { channels, loading: channelsLoading } = useChannels(guildId);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  
  const [formData, setFormData] = useState({
    channel_id: '',
    prize: '',
    ticket_payment_type: 'xanax',
    ticket_price: 1,
    tickets_available: 100,
    max_tickets_per_user: 10,
    duration_hours: 24,
  });

  const handleChange = (e) => {
    const { name, value, type } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: type === 'number' ? parseInt(value, 10) : value,
    }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      const data = {
        guild_id: parseInt(guildId, 10),
        channel_id: parseInt(formData.channel_id, 10),
        prize: formData.prize,
        ticket_payment_type: formData.ticket_payment_type,
        ticket_price: formData.ticket_price,
        tickets_available: formData.tickets_available,
        max_tickets_per_user: formData.max_tickets_per_user,
        duration_hours: formData.duration_hours,
      };

      const result = await raffles.create(data);
      onSuccess?.(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <h3 className="text-lg font-semibold text-white mb-4">Create Raffle</h3>
      
      {error && (
        <div className="bg-red-500/20 border border-red-500 rounded p-3 text-red-200 text-sm">
          {error}
        </div>
      )}

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">
          Announcement Channel
        </label>
        <select
          name="channel_id"
          value={formData.channel_id}
          onChange={handleChange}
          required
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
          disabled={channelsLoading}
        >
          <option value="">Select a channel...</option>
          {channels.map(channel => (
            <option key={channel.id} value={channel.id}>
              #{channel.name}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">
          Prize Description
        </label>
        <input
          type="text"
          name="prize"
          value={formData.prize}
          onChange={handleChange}
          maxLength={500}
          required
          placeholder="e.g., 100 Xanax, Donator Pack, $10M Cash"
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">
            Ticket Payment Type
          </label>
          <select
            name="ticket_payment_type"
            value={formData.ticket_payment_type}
            onChange={handleChange}
            className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
          >
            {PAYMENT_TYPE_OPTIONS.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">
            Ticket Price
          </label>
          <input
            type="number"
            name="ticket_price"
            value={formData.ticket_price}
            onChange={handleChange}
            min={1}
            max={100}
            required
            className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">
            Total Tickets Available
          </label>
          <input
            type="number"
            name="tickets_available"
            value={formData.tickets_available}
            onChange={handleChange}
            min={10}
            max={10000}
            required
            className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">
            Max Tickets Per User
          </label>
          <input
            type="number"
            name="max_tickets_per_user"
            value={formData.max_tickets_per_user}
            onChange={handleChange}
            min={0}
            max={1000}
            className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
          />
          <p className="text-xs text-gray-400 mt-1">
            Set to 0 for unlimited tickets per user.
          </p>
        </div>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">
          Duration (Hours)
        </label>
        <input
          type="number"
          name="duration_hours"
          value={formData.duration_hours}
          onChange={handleChange}
          min={1}
          max={720}
          required
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
        />
        <p className="text-xs text-gray-400 mt-1">
          The raffle will automatically draw a winner after this duration.
        </p>
      </div>

      <div className="flex justify-end space-x-3 pt-4">
        <button
          type="button"
          onClick={onCancel}
          className="px-4 py-2 text-gray-300 hover:text-white transition-colors"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={loading || !formData.channel_id || !formData.prize}
          className="px-6 py-2 bg-pink-600 hover:bg-pink-700 disabled:bg-gray-600 disabled:cursor-not-allowed rounded-lg text-white font-medium transition-colors"
        >
          {loading ? 'Creating...' : 'Create Raffle'}
        </button>
      </div>
    </form>
  );
}
