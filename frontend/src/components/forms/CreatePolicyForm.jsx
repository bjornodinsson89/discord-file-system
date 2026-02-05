/**
 * CreatePolicyForm - Form for creating insurance policies
 */

import React, { useState } from 'react';
import { insurance } from '../../lib/api';

const COST_TYPE_OPTIONS = [
  { value: 'xanax', label: 'Xanax' },
  { value: 'erotic_dvd', label: 'Erotic DVD' },
];

const COVERAGE_TYPE_OPTIONS = [
  { value: 'xanax_stack', label: 'Xanax Stack Only' },
  { value: 'ecstasy_after_stack', label: 'Ecstasy After Stack' },
  { value: 'all_drugs', label: 'All Drug-Related Losses' },
];

export default function CreatePolicyForm({ guildId, onSuccess, onCancel }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  
  const [formData, setFormData] = useState({
    policy_name: '',
    description: '',
    cost_type: 'xanax',
    cost_amount: 1,
    coverage_type: 'xanax_stack',
    payout_description: '',
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
        ...formData,
      };

      const result = await insurance.createPolicy(data);
      onSuccess?.(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <h3 className="text-lg font-semibold text-white mb-4">Create Insurance Policy</h3>
      
      {error && (
        <div className="bg-red-500/20 border border-red-500 rounded p-3 text-red-200 text-sm">
          {error}
        </div>
      )}

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">
          Policy Name
        </label>
        <input
          type="text"
          name="policy_name"
          value={formData.policy_name}
          onChange={handleChange}
          maxLength={100}
          required
          placeholder="e.g., Basic Xanax Coverage"
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-teal-500"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">
          Coverage Description
        </label>
        <textarea
          name="description"
          value={formData.description}
          onChange={handleChange}
          required
          rows={3}
          placeholder="Describe what this policy covers and any conditions..."
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-teal-500 resize-none"
        />
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">
            Premium Payment Type
          </label>
          <select
            name="cost_type"
            value={formData.cost_type}
            onChange={handleChange}
            className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-teal-500"
          >
            {COST_TYPE_OPTIONS.map(opt => (
              <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-gray-300 mb-1">
            Premium Amount
          </label>
          <input
            type="number"
            name="cost_amount"
            value={formData.cost_amount}
            onChange={handleChange}
            min={1}
            max={1000}
            required
            className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-teal-500"
          />
        </div>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">
          Coverage Type
        </label>
        <select
          name="coverage_type"
          value={formData.coverage_type}
          onChange={handleChange}
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-teal-500"
        >
          {COVERAGE_TYPE_OPTIONS.map(opt => (
            <option key={opt.value} value={opt.value}>{opt.label}</option>
          ))}
        </select>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">
          Payout Terms
        </label>
        <textarea
          name="payout_description"
          value={formData.payout_description}
          onChange={handleChange}
          required
          rows={2}
          placeholder="e.g., Full xanax replacement + 10% bonus"
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-teal-500 resize-none"
        />
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-300 mb-1">
          Coverage Duration (Hours)
        </label>
        <input
          type="number"
          name="duration_hours"
          value={formData.duration_hours}
          onChange={handleChange}
          min={1}
          max={720}
          required
          className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-teal-500"
        />
        <p className="text-xs text-gray-400 mt-1">
          How long the coverage lasts after purchase (max 30 days).
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
          disabled={loading || !formData.policy_name || !formData.description}
          className="px-6 py-2 bg-teal-600 hover:bg-teal-700 disabled:bg-gray-600 disabled:cursor-not-allowed rounded-lg text-white font-medium transition-colors"
        >
          {loading ? 'Creating...' : 'Create Policy'}
        </button>
      </div>
    </form>
  );
}
