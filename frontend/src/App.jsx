import React from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'

// Pages
import LoginPage from './pages/LoginPage'
import DashboardPage from './pages/DashboardPage'
import SessionsPage from './pages/SessionsPage'
import RafflesPage from './pages/RafflesPage'
import InsurancePage from './pages/InsurancePage'
import SettingsPage from './pages/SettingsPage'
import AuditLogPage from './pages/AuditLogPage'
import MembersPage from './pages/MembersPage'
import BlacklistPage from './pages/BlacklistPage'

// Layout
import DashboardLayout from './components/DashboardLayout'

// Context
import { AuthProvider, useAuth } from './hooks/useAuth'

function AppRoutes() {
  const { user, loading, isAuthenticated } = useAuth()

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-950">
        <div className="text-center">
          <div className="relative">
            <div className="w-16 h-16 rounded-xl bg-gradient-to-br from-purple-600 to-pink-600 animate-pulse" />
            <div className="absolute inset-0 w-16 h-16 rounded-xl bg-gradient-to-br from-purple-600 to-pink-600 animate-ping opacity-30" />
          </div>
          <p className="text-gray-400 mt-6 font-medium">Loading Dashboard...</p>
        </div>
      </div>
    )
  }

  return (
    <Routes>
      {/* Public routes */}
      <Route path="/login" element={
        isAuthenticated ? <Navigate to="/" replace /> : <LoginPage />
      } />

      {/* Protected routes */}
      {isAuthenticated ? (
        <Route element={<DashboardLayout user={user} />}>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/sessions" element={<SessionsPage />} />
          <Route path="/raffles" element={<RafflesPage />} />
          <Route path="/insurance" element={<InsurancePage />} />
          <Route path="/members" element={<MembersPage />} />
          <Route path="/blacklist" element={<BlacklistPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/audit" element={<AuditLogPage />} />
        </Route>
      ) : (
        <Route path="*" element={<Navigate to="/login" replace />} />
      )}
    </Routes>
  )
}

function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  )
}

export default App
