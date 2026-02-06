/**
 * useAuth Hook
 * Manages authentication state and provides auth methods.
 */

import { useState, useEffect, useCallback, createContext, useContext } from 'react';
import { auth as authApi, guilds as guildsApi } from '../lib/api';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [adminGuilds, setAdminGuilds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const getAdminGuildsFromUser = useCallback((targetUser) => {
    if (!targetUser) return [];

    // Administrator permission bit
    const ADMIN_BIT = 0x8;

    return (targetUser.guilds || []).filter(guild => {
      const perms = parseInt(guild.permissions || '0', 10);
      return guild.owner || ((perms & ADMIN_BIT) === ADMIN_BIT);
    });
  }, []);

  const loadAdminGuilds = useCallback(async (fallbackGuilds = []) => {
    try {
      const data = await guildsApi.getAdminGuilds();
      setAdminGuilds(data.guilds || fallbackGuilds);
    } catch (err) {
      console.error('Failed to fetch admin guilds:', err);
      setAdminGuilds(fallbackGuilds);
    }
  }, []);

  const checkAuth = useCallback(async () => {
    try {
      const response = await authApi.getStatus();
      if (response.authenticated) {
        setUser(response.user);
        const fallbackGuilds = getAdminGuildsFromUser(response.user);
        setAdminGuilds(fallbackGuilds);
        await loadAdminGuilds(fallbackGuilds);
      } else {
        setUser(null);
        setAdminGuilds([]);
      }
    } catch (err) {
      console.error('Auth check failed:', err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [getAdminGuildsFromUser, loadAdminGuilds]);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  const login = () => {
    authApi.login();
  };

  const logout = () => {
    setUser(null);
    setAdminGuilds([]);
    authApi.logout();
  };

  const getAdminGuilds = () => {
    if (adminGuilds.length > 0) return adminGuilds;
    return getAdminGuildsFromUser(user);
  };

  const value = {
    user,
    loading,
    error,
    login,
    logout,
    checkAuth,
    getAdminGuilds,
    adminGuilds,
    isAuthenticated: !!user,
  };

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}

export default useAuth;
