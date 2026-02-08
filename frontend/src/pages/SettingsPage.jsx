/**
 * SettingsPage - Configure guild settings
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useGuilds } from '../hooks/useGuilds';
import { useChannels } from '../hooks/useChannels';
import { useRoles } from '../hooks/useRoles';
import { settings } from '../lib/api';
import { Card, CardContent, CardHeader, CardTitle, Button, useToast } from '../components/ui';

export default function SettingsPage() {
  const { selectedGuildId, selectedGuild } = useGuilds();
  const { addToast } = useToast();
  const { channels, loading: channelsLoading, error: channelsError } = useChannels(selectedGuildId);
  const { roles, loading: rolesLoading, error: rolesError } = useRoles(selectedGuildId);
  
  const [formData, setFormData] = useState({
    host99k_role_id: '',
    insurer_role_id: '',
    admin_role_id: '',
    jump_99k_channel_id: '',
    insurance_channel_id: '',
    raffle_channel_id: '',
    welcome_channel_id: '',
    welcome_message_template: 'Welcome {user} to {guild}! You are member #{member_count}.',
    welcome_enabled: false,
    reservation_timeout_minutes: 5,
    auto_complete_enabled: true,
  });
  
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(false);

  const loadSettings = useCallback(async () => {
    if (!selectedGuildId) return;
    
    setLoading(true);
    setError(null);
    try {
      const data = await settings.get(selectedGuildId);
      setFormData({
        host99k_role_id: data.host99k_role_id || '',
        insurer_role_id: data.insurer_role_id || '',
        admin_role_id: data.admin_role_id || '',
        jump_99k_channel_id: data.jump_99k_channel_id || '',
        insurance_channel_id: data.insurance_channel_id || '',
        raffle_channel_id: data.raffle_channel_id || '',
        welcome_channel_id: data.welcome_channel_id || '',
        welcome_message_template: data.welcome_message_template || 'Welcome {user} to {guild}! You are member #{member_count}.',
        welcome_enabled: data.welcome_enabled ?? false,
        reservation_timeout_minutes: data.reservation_timeout_minutes || 5,
        auto_complete_enabled: data.auto_complete_enabled ?? true,
      });
    } catch (err) {
      setError(err.message);
      addToast({ title: 'Failed to load settings', description: err.message, variant: 'error' });
    } finally {
      setLoading(false);
    }
  }, [selectedGuildId, addToast]);

  useEffect(() => {
    loadSettings();
  }, [loadSettings]);

  const handleChange = (e) => {
    const { name, value, type, checked } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: type === 'checkbox' ? checked : (type === 'number' ? parseInt(value, 10) : value),
    }));
    setSuccess(false);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    setSuccess(false);

    try {
      // Build update payload - only include non-empty values
      const updates = {
        guild_id: parseInt(selectedGuildId, 10),
      };

      if (formData.host99k_role_id) updates.host99k_role_id = parseInt(formData.host99k_role_id, 10);
      if (formData.insurer_role_id) updates.insurer_role_id = parseInt(formData.insurer_role_id, 10);
      if (formData.admin_role_id) updates.admin_role_id = parseInt(formData.admin_role_id, 10);
      if (formData.jump_99k_channel_id) updates.jump_99k_channel_id = parseInt(formData.jump_99k_channel_id, 10);
      if (formData.insurance_channel_id) updates.insurance_channel_id = parseInt(formData.insurance_channel_id, 10);
      if (formData.raffle_channel_id) updates.raffle_channel_id = parseInt(formData.raffle_channel_id, 10);
      if (formData.welcome_channel_id) updates.welcome_channel_id = parseInt(formData.welcome_channel_id, 10);
      updates.welcome_message_template = formData.welcome_message_template;
      updates.welcome_enabled = formData.welcome_enabled;
      updates.reservation_timeout_minutes = formData.reservation_timeout_minutes;
      updates.auto_complete_enabled = formData.auto_complete_enabled;

      await settings.update(updates);
      setSuccess(true);
      addToast({ title: 'Settings saved', description: 'Guild settings updated successfully.', variant: 'success' });
    } catch (err) {
      setError(err.message);
      addToast({ title: 'Failed to save settings', description: err.message, variant: 'error' });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[400px]">
        <p className="text-gray-400">Loading settings...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-2xl font-bold text-white">Settings</h1>
        <p className="text-gray-400">
          Configure Happy Jumper for {selectedGuild?.name || 'your server'}
        </p>
      </div>

      {error && (
        <div className="bg-red-500/20 border border-red-500 rounded-lg p-4 text-red-200">
          {error}
        </div>
      )}

      {success && (
        <div className="bg-green-500/20 border border-green-500 rounded-lg p-4 text-green-200">
          Settings saved successfully!
        </div>
      )}

      {channelsError && (
        <div className="bg-yellow-500/20 border border-yellow-500 rounded-lg p-4 text-yellow-100">
          Failed to load channels from Discord: {channelsError}
        </div>
      )}

      {rolesError && (
        <div className="bg-yellow-500/20 border border-yellow-500 rounded-lg p-4 text-yellow-100">
          Failed to load roles from Discord: {rolesError}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* Channels Section */}
        <Card>
          <CardHeader>
            <CardTitle>📢 Channels</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                99k Jump Announcements
              </label>
              <select
                name="jump_99k_channel_id"
                value={formData.jump_99k_channel_id}
                onChange={handleChange}
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
              <p className="text-xs text-gray-400 mt-1">
                Session announcements will be posted here.
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Raffle Announcements
              </label>
              <select
                name="raffle_channel_id"
                value={formData.raffle_channel_id}
                onChange={handleChange}
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
                Insurance Channel
              </label>
              <select
                name="insurance_channel_id"
                value={formData.insurance_channel_id}
                onChange={handleChange}
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
                Welcome Channel
              </label>
              <select
                name="welcome_channel_id"
                value={formData.welcome_channel_id}
                onChange={handleChange}
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
          </CardContent>
        </Card>

        {/* Roles Section */}
        <Card>
          <CardHeader>
            <CardTitle>👥 Roles</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                99k Host Role
              </label>
              <select
                name="host99k_role_id"
                value={formData.host99k_role_id}
                onChange={handleChange}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
                disabled={rolesLoading}
              >
                <option value="">Select a role...</option>
                {roles.map(role => (
                  <option key={role.id} value={role.id}>
                    @{role.name}
                  </option>
                ))}
              </select>
              <p className="text-xs text-gray-400 mt-1">
                Users with this role can host 99k jump sessions.
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Insurer Role
              </label>
              <select
                name="insurer_role_id"
                value={formData.insurer_role_id}
                onChange={handleChange}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
                disabled={rolesLoading}
              >
                <option value="">Select a role...</option>
                {roles.map(role => (
                  <option key={role.id} value={role.id}>
                    @{role.name}
                  </option>
                ))}
              </select>
              <p className="text-xs text-gray-400 mt-1">
                Users with this role can create insurance policies.
              </p>
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Dashboard Admin Role
              </label>
              <select
                name="admin_role_id"
                value={formData.admin_role_id}
                onChange={handleChange}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
                disabled={rolesLoading}
              >
                <option value="">Administrator only</option>
                {roles.map(role => (
                  <option key={role.id} value={role.id}>
                    @{role.name}
                  </option>
                ))}
              </select>
              <p className="text-xs text-gray-400 mt-1">
                Users with this role OR Administrator permission can access the dashboard.
              </p>
            </div>
          </CardContent>
        </Card>

        {/* General Settings */}
        <Card>
          <CardHeader>
            <CardTitle>⚙️ General Settings</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">
                Reservation Timeout (minutes)
              </label>
              <input
                type="number"
                name="reservation_timeout_minutes"
                value={formData.reservation_timeout_minutes}
                onChange={handleChange}
                min={1}
                max={60}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
              />
              <p className="text-xs text-gray-400 mt-1">
                How long users have to confirm payment after signing up.
              </p>
            </div>

            <div className="flex items-center space-x-3">
              <input
                type="checkbox"
                id="auto_complete"
                name="auto_complete_enabled"
                checked={formData.auto_complete_enabled}
                onChange={handleChange}
                className="w-4 h-4 text-purple-600 bg-gray-700 border-gray-600 rounded focus:ring-purple-500"
              />
              <label htmlFor="auto_complete" className="text-sm text-gray-300">
                Auto-complete sessions when all participants are ready
              </label>
            </div>
          </CardContent>
        </Card>

        {/* Submit */}
        <Card>
          <CardHeader>
            <CardTitle>👋 Welcome Messages</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <label className="flex items-center gap-2 text-gray-300">
              <input
                type="checkbox"
                name="welcome_enabled"
                checked={formData.welcome_enabled}
                onChange={handleChange}
              />
              Enable welcome messages
            </label>
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1">Welcome Template</label>
              <textarea
                name="welcome_message_template"
                value={formData.welcome_message_template}
                onChange={handleChange}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-purple-500"
                rows={4}
              />
              <p className="text-xs text-gray-400 mt-1">Placeholders: {'{user}'} {'{username}'} {'{guild}'} {'{member_count}'}</p>
            </div>
          </CardContent>
        </Card>

        <div className="flex justify-end">
          <Button type="submit" disabled={saving}>
            {saving ? 'Saving...' : 'Save Settings'}
          </Button>
        </div>
      </form>
    </div>
  );
}
