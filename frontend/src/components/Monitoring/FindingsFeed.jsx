import { useState, useEffect, Fragment } from 'react';
import api from '../../api';
import { AlertTriangle, RefreshCw, Ticket as TicketIcon, ChevronDown, ChevronRight } from 'lucide-react';
import '../Admin/AdminConsole.css';

// New/changed findings from scheduled or on-demand diagnostic scans. Distinct
// from MonitoringConsole.jsx in this same directory (that one is the admin
// telemetry/interactions dashboard — unrelated LLM/chat usage data).

const SEVERITY_BADGE = {
  critical: { label: 'Critical', cls: 'off' },
  warning:  { label: 'Warning', cls: 'pend' },
  info:     { label: 'Info', cls: 'admin' },
};

function ago(ts) {
  if (!ts) return '—';
  const mins = Math.floor((Date.now() - new Date(ts).getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

export default function FindingsFeed({ onClose }) {
  const [environments, setEnvironments] = useState([]);
  const [findings, setFindings] = useState(null);
  const [error, setError] = useState('');
  const [environmentId, setEnvironmentId] = useState('');
  const [severity, setSeverity] = useState('');
  const [statusFilter, setStatusFilter] = useState('open');
  const [source, setSource] = useState('');
  const [area, setArea] = useState('');
  const [q, setQ] = useState('');
  const [expandedId, setExpandedId] = useState(null);
  const [scanning, setScanning] = useState(false);
  const [convertingId, setConvertingId] = useState(null);

  useEffect(() => {
    api.get('/config/environments').then(r => setEnvironments(r.data)).catch(() => {});
  }, []);

  const load = () => {
    setError('');
    const params = new URLSearchParams();
    if (environmentId) params.set('environment_id', environmentId);
    if (source) params.set('source', source);
    if (area) params.set('area', area);
    if (severity) params.set('severity', severity);
    params.set('status_filter', statusFilter);
    if (q) params.set('q', q);
    api.get(`/monitoring/findings?${params.toString()}`)
      .then(r => setFindings(r.data))
      .catch(e => setError(e.response?.data?.detail || e.message));
  };

  // area/q are free-text — applied on Enter or the Refresh button, not
  // per-keystroke; only the dropdown filters auto-fire.
  useEffect(() => { load(); }, [environmentId, source, severity, statusFilter]);

  const scanNow = async () => {
    if (!environmentId) return;
    setScanning(true);
    try {
      await api.post(`/monitoring/scan/${environmentId}`);
      setTimeout(load, 1500);
    } catch (e) {
      alert(e.response?.data?.detail || e.message);
    } finally {
      setScanning(false);
    }
  };

  const convertToTicket = async (findingId) => {
    setConvertingId(findingId);
    try {
      await api.post(`/tickets/from-finding/${findingId}`);
      alert('Ticket created — see it under Admin Console → Tickets.');
    } catch (e) {
      alert(e.response?.data?.detail || e.message);
    } finally {
      setConvertingId(null);
    }
  };

  const rows = findings || [];

  return (
    <div className="admin-overlay">
      <div className="admin-container">
        <div className="admin-header">
          <h3><AlertTriangle size={18} style={{ verticalAlign: 'middle', marginRight: 8 }} />Findings</h3>
          <button className="admin-close" onClick={onClose}>×</button>
        </div>

        <div className="admin-body">
          <p className="admin-muted">
            New and changed issues surfaced by scheduled and on-demand diagnostic scans.
            Convert a finding into a tracked ticket, or trigger a scan now.
          </p>
          {error && <p className="admin-error">{error}</p>}

          <div className="admin-toolbar" style={{ gap: '0.5rem', flexWrap: 'wrap' }}>
            <select value={environmentId} onChange={e => setEnvironmentId(e.target.value)}>
              <option value="">All environments</option>
              {environments.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
            </select>
            <select value={source} onChange={e => setSource(e.target.value)}>
              <option value="">All sources</option>
              <option value="performance">Performance</option>
              <option value="patch_gap">Patch Gap</option>
              <option value="patch_file_gap">File Version Drift</option>
            </select>
            <input placeholder="Area/product (e.g. ap)" value={area}
              onChange={e => setArea(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') load(); }} style={{ width: 140 }} />
            <input placeholder="Search title…" value={q}
              onChange={e => setQ(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') load(); }} style={{ width: 180 }} />
            <select value={severity} onChange={e => setSeverity(e.target.value)}>
              <option value="">All severities</option>
              <option value="critical">Critical</option>
              <option value="warning">Warning</option>
              <option value="info">Info</option>
            </select>
            <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)}>
              <option value="open">Open</option>
              <option value="resolved">Resolved</option>
              <option value="">All</option>
            </select>
            <button className="admin-btn-ghost" onClick={load}><RefreshCw size={13} /> Refresh</button>
            {environmentId && (
              <button className="admin-btn-primary" disabled={scanning} onClick={scanNow}>
                {scanning ? 'Scanning…' : 'Scan now'}
              </button>
            )}
          </div>

          <table className="admin-table">
            <thead><tr>
              <th></th><th>Environment</th><th>Source</th><th>Area</th><th>Severity</th><th>Title</th>
              <th>First seen</th><th>Last seen</th><th>Occurrences</th><th></th>
            </tr></thead>
            <tbody>
              {rows.map(f => (
                <Fragment key={f.id}>
                  <tr onClick={() => setExpandedId(expandedId === f.id ? null : f.id)} style={{ cursor: 'pointer' }}>
                    <td>{expandedId === f.id ? <ChevronDown size={14} /> : <ChevronRight size={14} />}</td>
                    <td>{f.environment_name || '—'}</td>
                    <td className="admin-muted">{f.source}</td>
                    <td className="admin-muted">{f.area}</td>
                    <td>
                      <span className={`admin-pill ${SEVERITY_BADGE[f.severity]?.cls || ''}`}>
                        {SEVERITY_BADGE[f.severity]?.label || f.severity}
                      </span>
                    </td>
                    <td>{f.title}</td>
                    <td className="admin-muted">{ago(f.first_seen_at)}</td>
                    <td className="admin-muted">{ago(f.last_seen_at)}</td>
                    <td>{f.occurrence_count}</td>
                    <td>
                      {f.status === 'open' && (
                        <button className="admin-btn-ghost" disabled={convertingId === f.id}
                          onClick={(e) => { e.stopPropagation(); convertToTicket(f.id); }}>
                          <TicketIcon size={13} /> {convertingId === f.id ? 'Creating…' : 'Ticket'}
                        </button>
                      )}
                    </td>
                  </tr>
                  {expandedId === f.id && (
                    <tr>
                      <td colSpan={10} style={{ padding: '0.5rem 1rem', fontSize: '0.8rem' }}>
                        {Object.entries(f.detail || {}).map(([k, v]) => (
                          <div key={k}><strong>{k}:</strong> {v == null ? '—' : (typeof v === 'object' ? JSON.stringify(v) : String(v))}</div>
                        ))}
                        {(!f.detail || Object.keys(f.detail).length === 0) && <span className="admin-muted">No structured detail.</span>}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
              {findings && rows.length === 0 && (
                <tr><td colSpan={10} className="admin-muted">No findings match these filters.</td></tr>
              )}
              {!findings && !error && (
                <tr><td colSpan={10} className="admin-muted">Loading…</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
