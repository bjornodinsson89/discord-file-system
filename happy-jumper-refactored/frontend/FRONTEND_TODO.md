# Frontend Implementation Guide

The frontend structure is set up with all necessary configuration and build tools. Here's what needs to be implemented:

## ✅ Completed

- Project structure
- Build configuration (Vite + React)
- Styling setup (Tailwind CSS + shadcn/ui theming)
- Main app routing structure
- API proxy configuration

## 📋 To Implement

### 1. Core Components

#### `src/components/DashboardLayout.jsx`
- Sidebar navigation
- User dropdown menu
- Dark/light theme toggle
- Mobile responsive menu

#### `src/components/ui/` (shadcn/ui components)
Install via shadcn/ui CLI:
```bash
npx shadcn-ui@latest init
npx shadcn-ui@latest add button
npx shadcn-ui@latest add card
npx shadcn-ui@latest add dialog
npx shadcn-ui@latest add dropdown-menu
npx shadcn-ui@latest add input
npx shadcn-ui@latest add label
npx shadcn-ui@latest add select
npx shadcn-ui@latest add table
npx shadcn-ui@latest add toast
npx shadcn-ui@latest add tabs
npx shadcn-ui@latest add switch
```

### 2. Pages

#### `src/pages/LoginPage.jsx`
- Discord OAuth login button
- Branding/hero section
- Features overview

#### `src/pages/DashboardPage.jsx`
- KPI cards (active sessions, raffles, policies)
- Recent admin actions
- Quick create buttons

#### `src/pages/SessionsPage.jsx`
- Sessions list with filters (status, host)
- Create session modal/form
- Session actions (lock, cancel, complete)
- Data table with pagination

#### `src/pages/RafflesPage.jsx`
- Raffles list with filters
- Create raffle form
- Raffle actions (draw, cancel, extend)
- Entry management

#### `src/pages/InsurancePage.jsx`
- Provider management
- Policy creation form
- Claims queue
- Approval workflow

#### `src/pages/SettingsPage.jsx`
- Guild settings form
- Channel/role selectors (fetch from Discord)
- Toggle switches for features
- Test tools section

#### `src/pages/AuditLogPage.jsx`
- Searchable audit log table
- Filters (actor, action, date range)
- Export to CSV functionality

### 3. Forms (Based on Spec)

#### Create Session Form (`src/components/forms/CreateSessionForm.jsx`)
```jsx
// Fields per spec 7.1
- Channel dropdown (fetch from API)
- Payment Type: radio (xanax / erotic_dvd)
- Payment Amount: number input (>= 1)
- Spots: number input (1-30)
- Xanax Stack: select (1_xanax, 2_xanax, 3_xanax, full_stack)
- Start Delay: number input (0-72 hours)
```

#### Create Raffle Form (`src/components/forms/CreateRaffleForm.jsx`)
```jsx
// Fields per spec 7.2
- Channel dropdown
- Prize: textarea (freeform)
- Ticket Payment Type: radio (xanax / erotic_dvd)
- Ticket Price: number input (>= 1)
- Tickets Available: number input (>= 10)
- Max Tickets per User: number input (>= 0, 0 = unlimited)
- Duration: number input (1-720 hours)
```

#### Create Policy Form (`src/components/forms/CreatePolicyForm.jsx`)
```jsx
// Fields per spec 7.3 (UPDATED)
- Policy Name: text input
- Description: textarea (coverage instructions)
- Cost Type: radio (xanax / erotic_dvd)
- Cost Amount: number input (>= 1)
- Coverage Type: select (xanax_stack, ecstasy_after_stack, all_drugs)
- Payout Description: textarea (freeform)
- Duration: number input (1-720 hours)
```

### 4. API Client

#### `src/lib/api.js`
```javascript
import axios from 'axios'

const api = axios.create({
  baseURL: '/api',
  withCredentials: true
})

// Sessions
export const sessions = {
  list: (guildId, params) => api.get('/sessions/list', { params: { guild_id: guildId, ...params } }),
  create: (data) => api.post('/sessions/create', data),
  get: (id) => api.get(`/sessions/${id}`),
  lock: (id) => api.post(`/sessions/${id}/lock`),
  cancel: (id, reason) => api.post(`/sessions/${id}/cancel`, { reason })
}

// Raffles
export const raffles = {
  list: (guildId, params) => api.get('/raffles/list', { params: { guild_id: guildId, ...params } }),
  create: (data) => api.post('/raffles/create', data),
  draw: (id) => api.post(`/raffles/${id}/draw`),
  cancel: (id) => api.post(`/raffles/${id}/cancel`)
}

// Insurance
export const insurance = {
  policies: {
    list: (guildId) => api.get('/insurance/policies/list', { params: { guild_id: guildId } }),
    create: (data) => api.post('/insurance/policy/create', data)
  },
  providers: {
    list: (guildId) => api.get('/insurance/providers/list', { params: { guild_id: guildId } }),
    approve: (data) => api.post('/insurance/provider/approve', data)
  }
}

// Settings
export const settings = {
  get: (guildId) => api.get(`/settings/${guildId}`),
  update: (data) => api.post('/settings/update', data)
}

// Audit
export const audit = {
  list: (guildId, params) => api.get('/audit/log', { params: { guild_id: guildId, ...params } })
}

export default api
```

### 5. Hooks

#### `src/hooks/useGuilds.js`
```javascript
// Get user's guilds with admin access
// Filter for guilds where user can manage
```

#### `src/hooks/useChannels.js`
```javascript
// Fetch channels for selected guild
// Need bot endpoint: GET /api/guild/{id}/channels
```

#### `src/hooks/useRoles.js`
```javascript
// Fetch roles for selected guild
// Need bot endpoint: GET /api/guild/{id}/roles
```

### 6. Additional Backend Endpoints Needed

Add these to `admin_api/routes.py`:

```python
# Guild info endpoints (for dropdown populations)
@router.get("/guild/{guild_id}/channels")
async def get_guild_channels(guild_id: int):
    """Get list of text channels in guild."""
    guild = bot.get_guild(guild_id)
    channels = [
        {"id": c.id, "name": c.name}
        for c in guild.text_channels
    ]
    return {"channels": channels}

@router.get("/guild/{guild_id}/roles")
async def get_guild_roles(guild_id: int):
    """Get list of roles in guild."""
    guild = bot.get_guild(guild_id)
    roles = [
        {"id": r.id, "name": r.name}
        for r in guild.roles
        if r.name != "@everyone"
    ]
    return {"roles": roles}
```

## 🎨 Design System

### Colors (from spec)
- Primary: Purple `#9B59B6` - buttons, links, highlights
- Success: Green `#2ECC71` - confirmations, success states
- Error: Red `#E74C3C` - errors, destructive actions
- Warning: Orange `#F39C12` - warnings, attention needed
- Info: Blue `#3498DB` - information, neutral states

### Typography
- Headings: Inter or System UI
- Body: Inter or System UI
- Monospace: JetBrains Mono (for IDs, codes)

### Components
- Cards: Rounded corners, subtle shadows
- Forms: Clear labels, helpful error messages
- Tables: Sticky headers, zebra striping, hover states
- Modals: Centered, overlay backdrop
- Toasts: Bottom-right, auto-dismiss

## 🔨 Build Commands

```bash
# Development with hot reload
npm run dev

# Production build
npm run build

# Preview production build
npm run preview

# Lint
npm run lint
```

## 📦 Installation Steps

```bash
cd frontend

# Install dependencies
npm install

# Install shadcn/ui components
npx shadcn-ui@latest init
# Select: React, Tailwind CSS, Default style

# Add all needed components
npx shadcn-ui@latest add button card dialog dropdown-menu input label select table toast tabs switch

# Start development
npm run dev
```

## 🧪 Testing Checklist

Once implemented, test:

- [ ] Login flow works
- [ ] User sees their guilds
- [ ] Can select guild
- [ ] Create session posts to Discord
- [ ] Create raffle posts to Discord
- [ ] Create policy saves correctly
- [ ] Session list shows data
- [ ] Filters work
- [ ] Pagination works
- [ ] Actions (lock, cancel) work
- [ ] Settings save correctly
- [ ] Audit log displays events
- [ ] Mobile responsive
- [ ] Dark mode consistent
- [ ] Forms validate correctly
- [ ] Error handling works
- [ ] Loading states show

## 📚 Resources

- [shadcn/ui Documentation](https://ui.shadcn.com/)
- [Tailwind CSS Docs](https://tailwindcss.com/docs)
- [React Router Docs](https://reactrouter.com/)
- [Axios Documentation](https://axios-http.com/)

## 🚀 Quick Start Implementation Order

1. **First**: Install shadcn/ui components
2. **Then**: Create DashboardLayout
3. **Next**: Create LoginPage
4. **After**: Create API client
5. **Then**: Create SessionsPage with list
6. **Next**: Create CreateSessionForm
7. **Continue**: Implement other pages similarly
8. **Finally**: Polish and test

## 💡 Tips

- Use shadcn/ui components for consistency
- Follow existing patterns from other pages
- Test forms with validation
- Handle loading and error states
- Make mobile-friendly from start
- Add helpful empty states
- Include loading skeletons
- Show success/error toasts

---

**The backend is complete and functional. Focus on implementing these frontend components to complete the dashboard!**
