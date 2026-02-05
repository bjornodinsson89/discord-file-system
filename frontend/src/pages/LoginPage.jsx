/**
 * LoginPage - Beautiful animated login with Discord OAuth
 */

import React, { useEffect, useState } from 'react'

export default function LoginPage() {
  const [mousePosition, setMousePosition] = useState({ x: 0, y: 0 })

  useEffect(() => {
    const handleMouseMove = (e) => {
      setMousePosition({
        x: (e.clientX / window.innerWidth) * 100,
        y: (e.clientY / window.innerHeight) * 100,
      })
    }
    window.addEventListener('mousemove', handleMouseMove)
    return () => window.removeEventListener('mousemove', handleMouseMove)
  }, [])

  const handleLogin = () => {
    window.location.href = '/auth/login'
  }

  return (
    <div className="min-h-screen flex items-center justify-center overflow-hidden relative">
      {/* Animated Background */}
      <div className="absolute inset-0 bg-gray-950">
        {/* Gradient orbs */}
        <div
          className="absolute w-[600px] h-[600px] rounded-full opacity-30 blur-[120px] transition-transform duration-1000"
          style={{
            background: 'radial-gradient(circle, #7c3aed 0%, transparent 70%)',
            left: `${mousePosition.x - 20}%`,
            top: `${mousePosition.y - 20}%`,
            transform: 'translate(-50%, -50%)',
          }}
        />
        <div
          className="absolute w-[500px] h-[500px] rounded-full opacity-20 blur-[100px]"
          style={{
            background: 'radial-gradient(circle, #ec4899 0%, transparent 70%)',
            right: '10%',
            top: '20%',
            animation: 'float 8s ease-in-out infinite',
          }}
        />
        <div
          className="absolute w-[400px] h-[400px] rounded-full opacity-20 blur-[80px]"
          style={{
            background: 'radial-gradient(circle, #06b6d4 0%, transparent 70%)',
            left: '20%',
            bottom: '10%',
            animation: 'float 10s ease-in-out infinite reverse',
          }}
        />

        {/* Grid overlay */}
        <div
          className="absolute inset-0 opacity-10"
          style={{
            backgroundImage: `
              linear-gradient(rgba(124, 58, 237, 0.3) 1px, transparent 1px),
              linear-gradient(90deg, rgba(124, 58, 237, 0.3) 1px, transparent 1px)
            `,
            backgroundSize: '50px 50px',
          }}
        />

        {/* Floating particles */}
        {[...Array(20)].map((_, i) => (
          <div
            key={i}
            className="absolute w-1 h-1 bg-purple-500 rounded-full opacity-40"
            style={{
              left: `${Math.random() * 100}%`,
              top: `${Math.random() * 100}%`,
              animation: `twinkle ${3 + Math.random() * 4}s ease-in-out infinite`,
              animationDelay: `${Math.random() * 2}s`,
            }}
          />
        ))}
      </div>

      {/* Login Card */}
      <div className="relative z-10 w-full max-w-md px-4">
        <div className="backdrop-blur-xl bg-gray-900/70 rounded-2xl border border-gray-700/50 shadow-2xl overflow-hidden">
          {/* Glowing border effect */}
          <div className="absolute inset-0 rounded-2xl bg-gradient-to-r from-purple-600/20 via-pink-600/20 to-cyan-600/20 blur-xl -z-10" />

          <div className="p-8 md:p-10">
            {/* Logo & Header */}
            <div className="text-center mb-8">
              <div className="inline-flex items-center justify-center w-20 h-20 rounded-2xl bg-gradient-to-br from-purple-600 to-pink-600 mb-6 shadow-lg shadow-purple-500/30">
                <span className="text-4xl">HJ</span>
              </div>
              <h1 className="text-3xl font-bold text-white mb-2">
                Happy Jumper
              </h1>
              <p className="text-gray-400">
                Admin Dashboard & Control Center
              </p>
            </div>

            {/* Features */}
            <div className="space-y-3 mb-8">
              <Feature icon="HJ" text="Manage 99k Jump Sessions" color="purple" />
              <Feature icon="RS" text="Create & Run Raffles" color="pink" />
              <Feature icon="IS" text="Insurance Claims & Policies" color="cyan" />
              <Feature icon="AU" text="Full Audit Logging" color="yellow" />
            </div>

            {/* Login Button */}
            <button
              onClick={handleLogin}
              className="w-full relative group"
            >
              <div className="absolute inset-0 bg-gradient-to-r from-[#5865F2] to-[#7289DA] rounded-xl blur-sm opacity-70 group-hover:opacity-100 transition-opacity" />
              <div className="relative flex items-center justify-center gap-3 px-6 py-4 bg-[#5865F2] hover:bg-[#4752C4] rounded-xl transition-colors">
                <DiscordIcon className="w-6 h-6" />
                <span className="font-semibold text-white text-lg">
                  Continue with Discord
                </span>
              </div>
            </button>

            {/* Security note */}
            <p className="text-center text-xs text-gray-500 mt-6">
              <span className="inline-flex items-center gap-1">
                <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">
                  <path fillRule="evenodd" d="M5 9V7a5 5 0 0110 0v2a2 2 0 012 2v5a2 2 0 01-2 2H5a2 2 0 01-2-2v-5a2 2 0 012-2zm8-2v2H7V7a3 3 0 016 0z" clipRule="evenodd" />
                </svg>
                Secure OAuth2 authentication via Discord
              </span>
            </p>
          </div>
        </div>

        {/* Version */}
        <p className="text-center text-gray-600 text-xs mt-6">
          Happy Jumper Dashboard v2.0
        </p>
      </div>

      <style jsx>{`
        @keyframes float {
          0%, 100% { transform: translateY(0) rotate(0deg); }
          50% { transform: translateY(-20px) rotate(5deg); }
        }
        @keyframes twinkle {
          0%, 100% { opacity: 0.2; transform: scale(1); }
          50% { opacity: 0.8; transform: scale(1.5); }
        }
      `}</style>
    </div>
  )
}

function Feature({ icon, text, color }) {
  const colors = {
    purple: 'from-purple-600 to-purple-800 text-purple-300',
    pink: 'from-pink-600 to-pink-800 text-pink-300',
    cyan: 'from-cyan-600 to-cyan-800 text-cyan-300',
    yellow: 'from-yellow-600 to-yellow-800 text-yellow-300',
  }

  return (
    <div className="flex items-center gap-3 px-4 py-3 rounded-lg bg-gray-800/50 border border-gray-700/50">
      <div className={`w-8 h-8 rounded-lg bg-gradient-to-br ${colors[color]} flex items-center justify-center text-xs font-bold`}>
        {icon}
      </div>
      <span className="text-gray-300 text-sm">{text}</span>
    </div>
  )
}

function DiscordIcon({ className }) {
  return (
    <svg className={className} fill="currentColor" viewBox="0 0 24 24">
      <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515a.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0a12.64 12.64 0 0 0-.617-1.25a.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057a19.9 19.9 0 0 0 5.993 3.03a.078.078 0 0 0 .084-.028a14.09 14.09 0 0 0 1.226-1.994a.076.076 0 0 0-.041-.106a13.107 13.107 0 0 1-1.872-.892a.077.077 0 0 1-.008-.128a10.2 10.2 0 0 0 .372-.292a.074.074 0 0 1 .077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127a12.299 12.299 0 0 1-1.873.892a.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028a19.839 19.839 0 0 0 6.002-3.03a.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.03z"/>
    </svg>
  )
}
