/**
 * useChannels Hook
 * Fetches and caches guild text channels from the bot.
 */

import { useState, useEffect, useCallback } from 'react';
import { guilds as guildsApi } from '../lib/api';

export function useChannels(guildId) {
  const [channels, setChannels] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchChannels = useCallback(async () => {
    if (!guildId) {
      setChannels([]);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const data = await guildsApi.getChannels(guildId);
      setChannels(data.channels || []);
    } catch (err) {
      console.error('Failed to fetch channels:', err);
      setError(err.message);
      setChannels([]);
    } finally {
      setLoading(false);
    }
  }, [guildId]);

  useEffect(() => {
    fetchChannels();
  }, [fetchChannels]);

  return {
    channels,
    loading,
    error,
    refetch: fetchChannels,
  };
}

export default useChannels;
