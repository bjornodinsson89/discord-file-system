import React from 'react'
import { Outlet, Link, useLocation } from 'react-router-dom'
import { GuildProvider, useGuilds } from '../hooks/useGuilds'

function DashboardLayoutContent({ user }) {
  const location = useLocation()
  const { guilds, selectedGuildId, selectGuild, selectedGuild } = useGuilds()
  
  const handleLogout = () => {
    window.location.href = '/auth/logout'
  }
  
  const navigation = [
    { name: 'Overview', href: '/', icon: '📊' },
    { name: 'Sessions', href: '/sessions', icon: '🪂' },
    { name: 'Raffles', href: '/raffles', icon: '🎟️' },
    { name: 'Insurance', href: '/insurance', icon: '🛡️' },
    { name: 'Settings', href: '/settings', icon: '⚙️' },
    { name: 'Audit Log', href: '/audit', icon: '📋' },
  ]

  const isActive = (href) => {
    if (href === '/') return location.pathname === '/'
    return location.pathname.startsWith(href)
  }
  
  return (
    <div className="min-h-screen bg-gray-950 flex">
      {/* Sidebar */}
      <div className="w-64 bg-gray-900 border-r border-gray-800 flex flex-col">
        {/* Header */}
        <div className="p-6 border-b border-gray-800">
          <h1 className="text-xl font-bold text-white">Happy Jumper</h1>
          <p className="text-sm text-gray-400">Dashboard</p>
        </div>
        
        {/* Guild Selector */}
        <div className="p-4 border-b border-gray-800">
          <label className="block text-xs text-gray-400 mb-1">Server</label>
          <select
            value={selectedGuildId || ''}
            onChange={(e) => selectGuild(e.target.value)}
            className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white text-sm focus:outline-none focus:border-purple-500"
          >
            {guilds.map(guild => (
              <option key={guild.id} value={guild.id}>
                {guild.name}
              </option>
            ))}
          </select>
        </div>
        
        {/* Navigation */}
        <nav className="flex-1 p-4 space-y-1">
          {navigation.map((item) => (
            <Link
              key={item.name}
              to={item.href}
              className={`flex items-center space-x-3 px-4 py-3 rounded-lg transition-colors ${
                isActive(item.href)
                  ? 'bg-purple-600/20 text-purple-300 border-l-2 border-purple-500'
                  : 'text-gray-300 hover:bg-gray-800 hover:text-white'
              }`}
            >
              <span className="text-xl">{item.icon}</span>
              <span>{item.name}</span>
            </Link>
          ))}
        </nav>
        
        {/* User Section */}
        <div className="p-4 border-t border-gray-800">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3">
              {user?.avatar ? (
                <img
                  src={`https://cdn.discordapp.com/avatars/${user.id}/${user.avatar}.png`}
                  alt={user.username}
                  className="w-8 h-8 rounded-full"
                />
              ) : (
                <div className="w-8 h-8 rounded-full bg-purple-600 flex items-center justify-center">
                  <span className="text-white text-sm">{user?.username?.[0]}</span>
                </div>
              )}
              <div className="text-sm">
                <p className="text-white">{user?.username}</p>
                <p className="text-gray-400 text-xs">
                  {user?.discriminator !== '0' ? `#${user?.discriminator}` : ''}
                </p>
              </div>
            </div>
            <button
              onClick={handleLogout}
              className="text-gray-400 hover:text-white transition-colors"
              title="Logout"
            >
              🚪
            </button>
          </div>
        </div>
      </div>
      
      {/* Main content */}
      <div className="flex-1 overflow-y-auto">
        <div className="p-8">
          <Outlet />
        </div>
      </div>
    </div>
  )
}

export default function DashboardLayout({ user }) {
  return (
    <GuildProvider>
      <DashboardLayoutContent user={user} />
    </GuildProvider>
  )
}
