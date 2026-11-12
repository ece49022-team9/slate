import { useState } from 'react'
import App from './App'
import Dashboard from './Dashboard'
import DeviceLab from './lab/DeviceLab'

type Tab = 'dashboard' | 'mic' | 'lab'

const tabs: Array<{ id: Tab; label: string }> = [
  { id: 'dashboard', label: 'Dashboard' },
  { id: 'mic', label: 'Mic test' },
  { id: 'lab', label: 'Device lab' },
]

export default function Root() {
  const [tab, setTab] = useState<Tab>('dashboard')

  return (
    <>
      <nav className="tabs" aria-label="Pages">
        {tabs.map((t) => (
          <button
            key={t.id}
            className={tab === t.id ? '' : 'secondary'}
            aria-current={tab === t.id ? 'page' : undefined}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      {tab === 'dashboard' ? (
        <main>
          <p className="eyebrow">Dashboard</p>
          <h1>Slate</h1>
          <Dashboard />
        </main>
      ) : tab === 'lab' ? (
        <DeviceLab />
      ) : (
        <App />
      )}
    </>
  )
}