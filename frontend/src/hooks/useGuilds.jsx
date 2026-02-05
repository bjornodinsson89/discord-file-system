/**
 * useGuilds Hook
 * Manages guild selection state for the dashboard.
 */

import { useState, useEffect, createContext, useContext, useCallback } from 'react';
import { useAuth } from './useAuth';

const GuildContext = createContext(null);

export function GuildProvider({ children }) {
  const { user, getAdminGuilds, adminGuilds } = useAuth();
  const [selectedGuildId, setSelectedGuildId] = useState(null);
  const [guilds, setGuilds] = useState([]);

  useEffect(() => {
    if (user) {
      const resolvedGuilds = adminGuilds.length > 0 ? adminGuilds : getAdminGuilds();
      setGuilds(resolvedGuilds);
      
      // Auto-select first guild if none selected
      if (!selectedGuildId && resolvedGuilds.length > 0) {
        const savedGuild = localStorage.getItem('selectedGuildId');
        if (savedGuild && resolvedGuilds.find(g => g.id === savedGuild)) {
          setSelectedGuildId(savedGuild);
        } else {
          setSelectedGuildId(resolvedGuilds[0].id);
        }
      }
    }
  }, [user, getAdminGuilds, adminGuilds, selectedGuildId]);

  const selectGuild = useCallback((guildId) => {
    setSelectedGuildId(guildId);
    localStorage.setItem('selectedGuildId', guildId);
  }, []);

  const selectedGuild = guilds.find(g => g.id === selectedGuildId);

  const value = {
    guilds,
    selectedGuildId,
    selectedGuild,
    selectGuild,
    hasGuilds: guilds.length > 0,
  };

  return (
    <GuildContext.Provider value={value}>
      {children}
    </GuildContext.Provider>
  );
}

export function useGuilds() {
  const context = useContext(GuildContext);
  if (!context) {
    throw new Error('useGuilds must be used within a GuildProvider');
  }
  return context;
}

export default useGuilds;
