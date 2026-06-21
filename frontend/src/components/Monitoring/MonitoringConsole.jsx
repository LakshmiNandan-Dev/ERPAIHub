import { useState } from 'react';
import { Activity, ScrollText } from 'lucide-react';
import { MonitoringTab, InteractionsTab } from '../Admin/AdminConsole';
import '../Admin/AdminConsole.css';

// Standalone, top-level Monitoring console (split out of the Admin Console so it
// can grow independently). Each section is a tab; add more metric tabs here.
const VIEWS = [
  { key: 'live', label: 'Live & Quality', icon: Activity },
  { key: 'interactions', label: 'Interactions', icon: ScrollText },
];

export default function MonitoringConsole({ onClose }) {
  const [view, setView] = useState('live');

  return (
    <div className="admin-overlay">
      <div className="admin-container">
        <div className="admin-header">
          <h3>
            <Activity size={18} style={{ verticalAlign: 'middle', marginRight: 8 }} />
            Monitoring Console
          </h3>
          <button className="admin-close" onClick={onClose}>×</button>
        </div>

        <div className="admin-tabs">
          {VIEWS.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              className={`admin-tab ${view === key ? 'active' : ''}`}
              onClick={() => setView(key)}
            >
              <Icon size={14} style={{ marginRight: 6, verticalAlign: 'middle' }} />{label}
            </button>
          ))}
        </div>

        <div className="admin-body">
          {view === 'live' && <MonitoringTab />}
          {view === 'interactions' && <InteractionsTab />}
        </div>
      </div>
    </div>
  );
}
