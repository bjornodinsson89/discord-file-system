/**
 * useAuth Hook
 * Manages authentication state and provides auth methods.
 */

import { useState, useEffect, useCallback, createContext, useContext } from 'react';
import { auth as authApi, guilds as guildsApi, setCsrfToken } from '../lib/api';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [adminGuilds, setAdminGuilds] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const loadAdminGuilds = useCallback(async () => {
    try {
      const data = await guildsApi.getAdminGuilds();
      setAdminGuilds(data.guilds || []);
    } catch (err) {
      console.error('Failed to fetch admin guilds:', err);
      setAdminGuilds([]);
    }
  }, []);

  const checkAuth = useCallback(async () => {
    try {
      const response = await authApi.getStatus();
      setCsrfToken(response?.csrf_token || null);
      if (response.authenticated) {
        setUser(response.user);
        setAdminGuilds([]);
        await loadAdminGuilds();
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
  }, [loadAdminGuilds]);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  const login = () => {
    authApi.login();
  };

  const logout = () => {
    setUser(null);
    setAdminGuilds([]);
    setCsrfToken(null);
    authApi.logout();
  };

  const getAdminGuilds = () => adminGuilds;

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
