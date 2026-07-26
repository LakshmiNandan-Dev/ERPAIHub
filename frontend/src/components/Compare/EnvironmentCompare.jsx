import { useState, useEffect } from 'react';
import api from '../../api';
import ReactMarkdown from 'react-markdown';
import { GitCompare, ArrowLeftRight, Loader2 } from 'lucide-react';
import '../Admin/AdminConsole.css';

const ALL_AREAS = ['wait_events', 'top_sql', 'memory', 'locks', 'tablespace', 'concurrent_manager', 'statistics'];
const CONFIG_CATEGORIES = [
  { key: 'db_parameters', label: 'DB Init Parameters' },
  { key: 'profile_options', label: 'Profile Options (Site)' },
  { key: 'responsibilities', label: 'Responsibilities' },
];

function getLLMHeaders() {
  try {
    const llm = JSON.parse(localStorage.getItem('llm_config') || '{}');
    return {
      'X-LLM-Provider': llm.provider || 'ollama',
      'X-LLM-Model': llm.model || '',
      'X-LLM-Base-Url': llm.base_url || '',
    };
  } catch { return { 'X-LLM-Provider': 'ollama' }; }
}

async function readSseStream(resp, onEvent) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) {
      if (!line.startsWith('data: ')) continue;
      const raw = line.slice(6).trim();
      if (raw === '[DONE]') return;
      try { onEvent(JSON.parse(raw)); } catch { /* malformed */ }
    }
  }
}

export default function EnvironmentCompare({ onClose }) {
  const [view, setView] = useState('performance');   // 'performance' | 'patches' | 'config'
  const [environments, setEnvironments] = useState([]);
  const [envAId, setEnvAId] = useState('');
  const [envBId, setEnvBId] = useState('');

  useEffect(() => {
    api.get('/config/environments').then(r => setEnvironments(r.data)).catch(() => {});
  }, []);

  const swap = () => { const a = envAId; setEnvAId(envBId); setEnvBId(a); };
  const envName = (id) => environments.find(e => String(e.id) === String(id))?.name || '';

  return (
    <div className="admin-overlay">
      <div className="admin-container">
        <div className="admin-header">
          <h3><GitCompare size={18} style={{ verticalAlign: 'middle', marginRight: 8 }} />Environment Compare</h3>
          <button className="admin-close" onClick={onClose}>×</button>
        </div>

        <div className="admin-toolbar" style={{ gap: '0.5rem' }}>
          <select value={envAId} onChange={e => setEnvAId(e.target.value)}>
            <option value="">Environment A…</option>
            {environments.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
          </select>
          <button className="admin-btn-ghost" onClick={swap} title="Swap A/B" disabled={!envAId && !envBId}>
            <ArrowLeftRight size={14} />
          </button>
          <select value={envBId} onChange={e => setEnvBId(e.target.value)}>
            <option value="">Environment B…</option>
            {environments.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
          </select>
        </div>

        <div className="admin-tabs">
          <button className={`admin-tab ${view === 'performance' ? 'active' : ''}`} onClick={() => setView('performance')}>Performance</button>
          <button className={`admin-tab ${view === 'patches' ? 'active' : ''}`} onClick={() => setView('patches')}>Patches</button>
          <button className={`admin-tab ${view === 'config' ? 'active' : ''}`} onClick={() => setView('config')}>Config</button>
        </div>

        <div className="admin-body">
          {!envAId || !envBId ? (
            <p className="admin-muted">Select two environments above to compare.</p>
          ) : envAId === envBId ? (
            <p className="admin-error">Environment A and B must be different.</p>
          ) : view === 'performance' ? (
            <PerformanceCompareView envAId={envAId} envBId={envBId} envAName={envName(envAId)} envBName={envName(envBId)} />
          ) : view === 'patches' ? (
            <PatchesCompareView envAId={envAId} envBId={envBId} envAName={envName(envAId)} envBName={envName(envBId)} />
          ) : (
            <ConfigCompareView envAId={envAId} envBId={envBId} envAName={envName(envAId)} envBName={envName(envBId)} />
          )}
        </div>
      </div>
    </div>
  );
}

function PerformanceCompareView({ envAId, envBId, envAName, envBName }) {
  const [selectedAreas, setSelectedAreas] = useState(new Set(ALL_AREAS));
  const [running, setRunning] = useState(false);
  const [phase, setPhase] = useState('');
  const [dataA, setDataA] = useState(null);
  const [dataB, setDataB] = useState(null);
  const [analysisText, setAnalysisText] = useState('');
  const [error, setError] = useState('');

  const toggleArea = (a) => setSelectedAreas(s => {
    const next = new Set(s);
    next.has(a) ? next.delete(a) : next.add(a);
    return next;
  });

  const run = async () => {
    if (running || selectedAreas.size === 0) return;
    setRunning(true); setPhase(''); setDataA(null); setDataB(null); setAnalysisText(''); setError('');
    const token = localStorage.getItem('session_token');
    try {
      const resp = await fetch(`${api.defaults.baseURL}/compare/performance`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}`, ...getLLMHeaders() },
        body: JSON.stringify({
          environment_a_id: parseInt(envAId), environment_b_id: parseInt(envBId),
          analysis_areas: Array.from(selectedAreas),
        }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await readSseStream(resp, (evt) => {
        if (evt.type === 'phase') setPhase(evt.content);
        if (evt.type === 'data_a') setDataA(evt.data);
        if (evt.type === 'data_b') setDataB(evt.data);
        if (evt.type === 'analysis_token') setAnalysisText(prev => prev + evt.content);
        if (evt.type === 'error') setError(evt.content);
      });
    } catch (e) {
      setError(e.message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div>
      <div className="admin-toolbar" style={{ flexWrap: 'wrap', gap: '0.5rem' }}>
        {ALL_AREAS.map(a => (
          <label key={a} style={{ fontSize: '0.78rem', display: 'flex', alignItems: 'center', gap: '0.25rem' }}>
            <input type="checkbox" checked={selectedAreas.has(a)} onChange={() => toggleArea(a)} />
            {a.replace('_', ' ')}
          </label>
        ))}
        <button className="admin-btn-primary" onClick={run} disabled={running || selectedAreas.size === 0}>
          {running ? <Loader2 size={14} className="spin" /> : null} {running ? 'Comparing…' : 'Compare Environments'}
        </button>
      </div>
      {error && <p className="admin-error">{error}</p>}
      {phase && <p className="admin-muted">{phase}</p>}

      {(dataA || dataB) && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', marginTop: '0.6rem' }}>
          <div>
            <h4>{envAName}</h4>
            <pre className="admin-muted" style={{ fontSize: '0.7rem', maxHeight: 260, overflow: 'auto', whiteSpace: 'pre-wrap' }}>
              {dataA ? JSON.stringify(dataA, null, 2) : 'waiting…'}
            </pre>
          </div>
          <div>
            <h4>{envBName}</h4>
            <pre className="admin-muted" style={{ fontSize: '0.7rem', maxHeight: 260, overflow: 'auto', whiteSpace: 'pre-wrap' }}>
              {dataB ? JSON.stringify(dataB, null, 2) : 'waiting…'}
            </pre>
          </div>
        </div>
      )}

      {analysisText && (
        <div className="md" style={{ marginTop: '1rem' }}>
          <ReactMarkdown>{analysisText}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}

function PatchesCompareView({ envAId, envBId, envAName, envBName }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState('');
  const [scanning, setScanning] = useState(null);   // 'a' | 'b' | null

  const load = () => {
    setError('');
    api.get(`/compare/patches?environment_a_id=${envAId}&environment_b_id=${envBId}`)
      .then(r => setRows(r.data))
      .catch(e => setError(e.response?.data?.detail || e.message));
  };

  useEffect(() => { load(); }, [envAId, envBId]);

  const scan = async (side) => {
    setScanning(side);
    try {
      await api.post(`/patching/gap/scan/${side === 'a' ? envAId : envBId}`);
      setTimeout(load, 2000);
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally {
      setScanning(null);
    }
  };

  const statusPill = (s) => s === 'ok' ? <span className="admin-pill ok">ok</span>
    : s === 'never_scanned' ? <span className="admin-muted">never scanned</span>
      : <span className="admin-pill off">{s}</span>;

  return (
    <div>
      <p className="admin-muted">
        Reads the latest applied-patch snapshot for each side (Admin Console → Environments → Patch
        Targets, then Patching → Patch Gap → Scan). No target list required — this compares what's
        actually applied on each environment.
      </p>
      <div className="admin-toolbar">
        <button className="admin-btn-ghost" disabled={scanning === 'a'} onClick={() => scan('a')}>
          {scanning === 'a' ? 'Scanning…' : `Scan ${envAName}`}
        </button>
        <button className="admin-btn-ghost" disabled={scanning === 'b'} onClick={() => scan('b')}>
          {scanning === 'b' ? 'Scanning…' : `Scan ${envBName}`}
        </button>
        <button className="admin-btn-ghost" onClick={load}>Refresh</button>
      </div>
      {error && <p className="admin-error">{error}</p>}
      <table className="admin-table">
        <thead><tr>
          <th>Component</th><th>Only on A ({envAName})</th><th>Only on B ({envBName})</th>
          <th>Common</th><th>A status</th><th>B status</th>
        </tr></thead>
        <tbody>
          {(rows || []).map(r => (
            <tr key={r.component}>
              <td>{r.component}</td>
              <td title={r.only_in_a.join(', ')}>{r.only_in_a.length ? `${r.only_in_a.length} patches` : '—'}</td>
              <td title={r.only_in_b.join(', ')}>{r.only_in_b.length ? `${r.only_in_b.length} patches` : '—'}</td>
              <td>{r.common_count}</td>
              <td>{statusPill(r.a_status)}</td>
              <td>{statusPill(r.b_status)}</td>
            </tr>
          ))}
          {rows && rows.length === 0 && (
            <tr><td colSpan={6} className="admin-muted">
              No patch data for either environment yet — define Patch Targets and scan both sides first.
            </td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function ConfigCompareView({ envAId, envBId, envAName, envBName }) {
  const [diff, setDiff] = useState(null);
  const [error, setError] = useState('');
  const [scanning, setScanning] = useState(null);

  const load = () => {
    setError('');
    api.get(`/compare/config?environment_a_id=${envAId}&environment_b_id=${envBId}`)
      .then(r => setDiff(r.data))
      .catch(e => setError(e.response?.data?.detail || e.message));
  };

  useEffect(() => { load(); }, [envAId, envBId]);

  const scan = async (side) => {
    setScanning(side);
    try {
      await api.post(`/compare/config/scan/${side === 'a' ? envAId : envBId}`);
      setTimeout(load, 2500);
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally {
      setScanning(null);
    }
  };

  return (
    <div>
      <p className="admin-muted">
        DB init parameters (non-default only), site-level profile options, and active
        responsibilities. Profile values may contain sensitive data — reviewed here only, never
        sent to an LLM.
      </p>
      <div className="admin-toolbar">
        <button className="admin-btn-ghost" disabled={scanning === 'a'} onClick={() => scan('a')}>
          {scanning === 'a' ? 'Scanning…' : `Scan ${envAName}`}
        </button>
        <button className="admin-btn-ghost" disabled={scanning === 'b'} onClick={() => scan('b')}>
          {scanning === 'b' ? 'Scanning…' : `Scan ${envBName}`}
        </button>
        <button className="admin-btn-ghost" onClick={load}>Refresh</button>
      </div>
      {error && <p className="admin-error">{error}</p>}

      {CONFIG_CATEGORIES.map(({ key, label }) => {
        const cat = diff?.[key];
        return (
          <details key={key} open style={{ marginTop: '0.8rem' }}>
            <summary style={{ cursor: 'pointer', fontWeight: 600 }}>
              {label}
              {cat && (
                <span className="admin-muted" style={{ fontWeight: 400, marginLeft: 8, fontSize: '0.75rem' }}>
                  A: {cat.a_status}{cat.a_scanned_at ? ` (${new Date(cat.a_scanned_at).toLocaleString()})` : ''} ·
                  {' '}B: {cat.b_status}{cat.b_scanned_at ? ` (${new Date(cat.b_scanned_at).toLocaleString()})` : ''}
                </span>
              )}
            </summary>
            {cat && (
              <table className="admin-table" style={{ marginTop: '0.4rem' }}>
                <thead><tr><th>Name</th><th>Only on A</th><th>Only on B</th><th>Different value</th></tr></thead>
                <tbody>
                  <tr>
                    <td className="admin-muted">count</td>
                    <td>{cat.diff.only_in_a.length}</td>
                    <td>{cat.diff.only_in_b.length}</td>
                    <td>{cat.diff.different.length}</td>
                  </tr>
                  {cat.diff.different.map(d => (
                    <tr key={d.name}>
                      <td>{d.name}</td>
                      <td colSpan={2} className="admin-muted">A: {String(d.a)}  →  B: {String(d.b)}</td>
                      <td></td>
                    </tr>
                  ))}
                  {cat.diff.only_in_a.map(n => (
                    <tr key={`a-${n}`}><td>{n}</td><td>only here</td><td>—</td><td>—</td></tr>
                  ))}
                  {cat.diff.only_in_b.map(n => (
                    <tr key={`b-${n}`}><td>{n}</td><td>—</td><td>only here</td><td>—</td></tr>
                  ))}
                </tbody>
              </table>
            )}
          </details>
        );
      })}
    </div>
  );
}
