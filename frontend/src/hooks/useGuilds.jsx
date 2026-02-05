/**
 * useGuilds Hook
 * Manages guild selection state for the dashboard.
 */

import { useState, useEffect, createContext, useContext, useCallback } from 'react';
import { useAuth } from './useAuth';

const GuildContext = createContext(null);

export function GuildProvider({ children }) {
  const { user, getAdminGuilds } = useAuth();
  const [selectedGuildId, setSelectedGuildId] = useState(null);
  const [guilds, setGuilds] = useState([]);

  useEffect(() => {
    if (user) {
      const adminGuilds = getAdminGuilds();
      setGuilds(adminGuilds);
      
      // Auto-select first guild if none selected
      if (!selectedGuildId && adminGuilds.length > 0) {
        const savedGuild = localStorage.getItem('selectedGuildId');
        if (savedGuild && adminGuilds.find(g => g.id === savedGuild)) {
          setSelectedGuildId(savedGuild);
        } else {
          setSelectedGuildId(adminGuilds[0].id);
        }
      }
    }
  }, [user, getAdminGuilds]);

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
