/**
 * useAuth Hook
 * Manages authentication state and provides auth methods.
 */

import { useState, useEffect, createContext, useContext } from 'react';
import { auth as authApi } from '../lib/api';

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    checkAuth();
  }, []);

  const checkAuth = async () => {
    try {
      const response = await authApi.getStatus();
      if (response.authenticated) {
        setUser(response.user);
      }
    } catch (err) {
      console.error('Auth check failed:', err);
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const login = () => {
    authApi.login();
  };

  const logout = () => {
    setUser(null);
    authApi.logout();
  };

  const getAdminGuilds = () => {
    if (!user) return [];
    
    // Administrator permission bit
    const ADMIN_BIT = 0x8;
    
    return (user.guilds || []).filter(guild => {
      const perms = parseInt(guild.permissions || '0', 10);
      return guild.owner || ((perms & ADMIN_BIT) === ADMIN_BIT);
    });
  };

  const value = {
    user,
    loading,
    error,
    login,
    logout,
    checkAuth,
    getAdminGuilds,
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
