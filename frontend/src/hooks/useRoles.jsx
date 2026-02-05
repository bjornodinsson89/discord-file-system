/**
 * useRoles Hook
 * Fetches and caches guild roles from the bot.
 */

import { useState, useEffect, useCallback } from 'react';
import { guilds as guildsApi } from '../lib/api';

export function useRoles(guildId) {
  const [roles, setRoles] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const fetchRoles = useCallback(async () => {
    if (!guildId) {
      setRoles([]);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const data = await guildsApi.getRoles(guildId);
      setRoles(data.roles || []);
    } catch (err) {
      console.error('Failed to fetch roles:', err);
      setError(err.message);
      setRoles([]);
    } finally {
      setLoading(false);
    }
  }, [guildId]);

  useEffect(() => {
    fetchRoles();
  }, [fetchRoles]);

  return {
    roles,
    loading,
    error,
    refetch: fetchRoles,
  };
}

export default useRoles;
