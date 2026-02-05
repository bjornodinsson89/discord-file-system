/**
 * CreateSessionForm - Form for creating 99k jump sessions
 */

import React, { useState } from 'react';
import { useChannels } from '../../hooks/useChannels';
import { sessions } from '../../lib/api';

const XANAX_STACK_OPTIONS = [
  { value: '1_xanax', label: '1 Xanax' },
  { value: '2_xanax', label: '2 Xanax' },
  { value: '3_xanax', label: '3 Xanax' },
  { value: 'full_stack', label: 'Full Stack' },
];

const PAYMENT_TYPE_OPTIONS = [
  { value: 'xanax', label: 'Xanax' },
  { value: 'erotic_dvd', label: 'Erotic DVD' },
];

export default function CreateSessionForm({ guildId, onSuccess, onCancel }) {
  const { channels, loading: channelsLoading } = useChannels(guildId);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  
  const [formData, setFormData] = useState({
    channel_id: '',
    payment_type: 'xanax',
    payment_amount: 1,
    spots: 10,
    xanax_stack: '1_xanax',
    start_delay_hours: 0,
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
        payment_type: formData.payment_type,
        payment_amount: formData.payment_amount,
        spots: formData.spots,
        xanax_stack: formData.xanax_stack,
        start_delay_hours: formData.start_delay_hours,
      };

      const result = await sessions.create(data);
      onSuccess?.(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <h3 className="text-lg font-semibold text-white mb-4">Create 99k Jump Session</h3>
      
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

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">
            Payment Type
          </label>
          <select
            name="payment_type"
            value={formData.payment_type}
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
            Payment Amount
          </label>
          <input
            type="number"
            name="payment_amount"
            value={formData.payment_amount}
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
            Number of Spots
          </label>
          <input
            type="number"
            name="spots"
            value={formData.spots}
            onChange={handleChange}
            min={1}
            max={30}
            required
            className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">
            Xanax Stack
          </label>
          <select
            name="xanax_stack"
            value={formData.xanax_stack}
            onChange={handleChange}
            className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
          >
            {XANAX_STACK_OPTIONS.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">
          Start Delay (Hours)
        </label>
        <input
          type="number"
          name="start_delay_hours"
          value={formData.start_delay_hours}
          onChange={handleChange}
          min={0}
          max={72}
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
        />
        <p className="text-xs text-gray-400 mt-1">
          Set to 0 to start immediately, or set hours to delay the jump.
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
          disabled={loading || !formData.channel_id}
          className="px-6 py-2 bg-purple-600 hover:bg-purple-700 disabled:bg-gray-600 disabled:cursor-not-allowed rounded-lg text-white font-medium transition-colors"
        >
          {loading ? 'Creating...' : 'Create Session'}
        </button>
      </div>
    </form>
  );
}
