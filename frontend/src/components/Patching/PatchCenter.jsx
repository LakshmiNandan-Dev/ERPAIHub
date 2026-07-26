import { useState, useEffect, useRef } from 'react';
import api from '../../api';
import ReactMarkdown from 'react-markdown';
import { Send, Download, RotateCcw, Loader2, CheckCircle2, Wrench, ListChecks, Activity, ShieldAlert, RefreshCw } from 'lucide-react';
import './PatchCenter.css';
import '../Admin/AdminConsole.css';

const NODE_LABEL = {
  'controller': 'Controller',
  'db': 'Database',
  'grid': 'Grid (root)',
  'apps': 'Apps tier',
};

// Buttons for the interactive adop cycle, in cycle order.
const PHASE_BTNS = [
  ['prepare', 'Prepare'], ['apply', 'Apply'], ['finalize', 'Finalize'],
  ['cutover', 'Cutover'], ['cleanup', 'Cleanup'], ['fs_clone', 'Fs_clone'],
];

const hasAdopComponent = (s) => /\b(adop|apps|application|ebs|online)\b/i.test(s || '');

export default function PatchCenter({ onClose }) {
  const [view, setView] = useState('patch');   // 'patch' | 'gap'
  const [messages, setMessages] = useState([]);
  const [context, setContext] = useState({});
  const [field, setField] = useState(null);
  const [phase, setPhase] = useState('interview');   // interview | plan | approval | error | done | cycle
  const [input, setInput] = useState('');
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);
  const [cycleStatus, setCycleStatus] = useState(null);
  const [cycleSteps, setCycleSteps] = useState([]);
  const [liveMode, setLiveMode] = useState(false);
  const endRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => { ask({}); /* eslint-disable-next-line */ }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, run, cycleSteps, cycleStatus]);

  const ask = async (ctx) => {
    setBusy(true);
    try {
      const r = (await api.post('/patching/agent', { context: ctx })).data;
      setContext(r.context || ctx);
      setMessages(m => [...m, { role: 'agent', content: r.content }]);
      if (r.type === 'question') { setField(r.field); setPhase('interview'); }
      else if (r.type === 'error') { setField(null); setPhase('error'); }
      else { setField(null); setPhase('plan'); }
    } catch (e) {
      setMessages(m => [...m, { role: 'agent', content: '⚠️ ' + (e.response?.data?.detail || e.message) }]);
    } finally { setBusy(false); inputRef.current?.focus(); }
  };

  const send = (e) => {
    e?.preventDefault();
    const val = input.trim();
    if (!val || !field || busy) return;
    const next = { ...context, [field]: val };
    setMessages(m => [...m, { role: 'user', content: val }]);
    setInput('');
    ask(next);
  };

  const handleApprovalError = (e) => {
    const detail = e.response?.data?.detail;
    if (e.response?.status === 409 && detail?.requires_approval) {
      const reasons = (detail.guard?.reasons || []).map(x => `- ${x}`).join('\n');
      setMessages(m => [...m, { role: 'agent', content:
        `🔴 **${detail.message}**\n\n${reasons}\n\n` +
        `Logged as patch **#${detail.run_id}**; it now needs approval by a **different** Admin/DBA ` +
        `(separation of duties). An approver can review it in **Admin Console → Audit**.` }]);
      setPhase('approval');
      return true;
    }
    return false;
  };

  const startPatch = async () => {
    setBusy(true);
    try {
      const r = (await api.post('/patching/', { ...context, live: liveMode })).data;
      setRun(r);
      setPhase('done');
      setMessages(m => [...m, { role: 'agent', content:
        `✅ Simulated patch of **${r.environment_name}** (${r.patch_number}) completed — ${(r.steps || []).length} phases. Download the runbook below.` }]);
    } catch (e) {
      if (!handleApprovalError(e)) alert(typeof e.response?.data?.detail === 'string' ? e.response.data.detail : e.message);
    } finally { setBusy(false); }
  };

  const startInteractive = async () => {
    setBusy(true);
    try {
      const r = (await api.post('/patching/', { ...context, interactive: true, live: liveMode })).data;
      setRun(r);
      setCycleSteps([]);
      setPhase('cycle');
      setMessages(m => [...m, { role: 'agent', content:
        `🧭 Interactive adop cycle armed for **${r.environment_name}** (${r.patch_number}). ` +
        `Drive it one phase at a time below — check status anytime.` }]);
      await refreshStatus(r.id);
    } catch (e) {
      if (!handleApprovalError(e)) alert(typeof e.response?.data?.detail === 'string' ? e.response.data.detail : e.message);
    } finally { setBusy(false); }
  };

  const refreshStatus = async (id) => {
    try {
      const st = (await api.get(`/patching/${id ?? run.id}/adop-status`)).data;
      setCycleStatus(st);
    } catch (e) { /* non-fatal */ }
  };

  const runPhase = async (p) => {
    if (!run || busy) return;
    setBusy(true);
    try {
      const res = (await api.post(`/patching/${run.id}/phase`, { phase: p })).data;
      setCycleStatus(res.status);
      if (res.step && p !== 'status') setCycleSteps(s => [...s, res.step]);
      if (p === 'status') {
        setMessages(m => [...m, { role: 'agent', content: `📋 **adop -status:** ${res.status.summary}` }]);
      }
    } catch (e) {
      const d = e.response?.data?.detail;
      setMessages(m => [...m, { role: 'agent', content: '⚠️ ' + (typeof d === 'string' ? d : e.message) }]);
    } finally { setBusy(false); }
  };

  const downloadRunbook = async () => {
    try {
      const res = await api.get(`/patching/${run.id}/runbook`, { responseType: 'blob' });
      const cd = res.headers['content-disposition'] || '';
      const mm = cd.match(/filename="?([^"]+)"?/);
      const fn = mm ? mm[1] : `oraebs_patch_${run.id}.zip`;
      const url = URL.createObjectURL(new Blob([res.data], { type: 'application/zip' }));
      const a = document.createElement('a'); a.href = url; a.download = fn;
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    } catch (e) { alert(e.response?.data?.detail || e.message); }
  };

  const reset = () => {
    setMessages([]); setContext({}); setField(null); setRun(null);
    setCycleStatus(null); setCycleSteps([]); setPhase('interview'); ask({});
  };

  const renderSteps = (list) => (
    <div className="patch-phases">
      {(list || []).map((s, i) => (
        <details key={i} className="patch-phase" open={i >= (list.length - 1)}>
          <summary>
            <CheckCircle2 size={14} className={s.status === 'failed' ? 'fail' : 'ok'} />
            <span className="ph-title">{s.title}</span>
            {s.source && <span className={`ph-src ${s.source}`}>{s.source === 'live' ? '● LIVE' : 'sim'}</span>}
            <span className={`ph-node ${s.node}`}>{NODE_LABEL[s.node] || s.node}</span>
          </summary>
          <pre className="ph-cmd">{s.command}</pre>
          <pre className="ph-log">{s.log}</pre>
        </details>
      ))}
    </div>
  );

  const allowed = cycleStatus?.allowed_phases || [];

  return (
    <div className="patch-overlay">
      <div className="patch-container">
        <div className="patch-header">
          <h3><Wrench size={18} style={{ verticalAlign: 'middle', marginRight: 8 }} />EBS Patching Agent
            <span className="patch-sub">
              {view === 'gap'
                ? 'Patch Gap — live opatch lspatches inventory vs. target patch list (SSH required, no simulator)'
                : 'DB / Grid / RAC OPatch · adop · WebLogic / FMW · live SSH or simulator'}
            </span></h3>
          <button className="patch-close" onClick={onClose}>×</button>
        </div>

        <div className="admin-tabs" style={{ padding: '0 1rem' }}>
          <button className={`admin-tab ${view === 'patch' ? 'active' : ''}`} onClick={() => setView('patch')}>
            <Wrench size={13} style={{ marginRight: 6, verticalAlign: 'middle' }} />Patch
          </button>
          <button className={`admin-tab ${view === 'gap' ? 'active' : ''}`} onClick={() => setView('gap')}>
            <ShieldAlert size={13} style={{ marginRight: 6, verticalAlign: 'middle' }} />Patch Gap
          </button>
        </div>

        {view === 'gap' ? <PatchGapPanel /> : (
        <>
        <div className="patch-body">
          <div className="patch-chat">
            {messages.map((m, i) => (
              <div key={i} className={`patch-msg ${m.role}`}>
                <div className="patch-bubble">
                  {m.role === 'agent'
                    ? <div className="md"><ReactMarkdown>{m.content}</ReactMarkdown></div>
                    : m.content}
                </div>
              </div>
            ))}
            {busy && <div className="patch-msg agent"><div className="patch-bubble"><Loader2 size={14} className="spin" /> working…</div></div>}

            {/* Interactive adop cycle status */}
            {phase === 'cycle' && cycleStatus && (
              <div className="patch-status">
                <p className="st-summary"><Activity size={13} style={{ verticalAlign: 'middle', marginRight: 6 }} />
                  <strong>adop status:</strong> {cycleStatus.summary}
                  {cycleStatus.source && <span className={`ph-src ${cycleStatus.source}`} style={{ marginLeft: 8 }}>
                    {cycleStatus.source === 'live' ? '● LIVE (SSH)' : 'simulated'}</span>}
                </p>
                <div className="st-meta">
                  {(cycleStatus.executed_phases || []).map(p => <span key={p} className="patch-pill">{p} ✓</span>)}
                  {cycleStatus.next_phase && <span>next: <strong>{cycleStatus.next_phase}</strong></span>}
                </div>
                {cycleStatus.readiness && !cycleStatus.readiness.live_phases && (
                  <div className="st-meta" style={{ marginTop: 4 }}>
                    ⚠️ live phases unavailable — configure on the environment: {(cycleStatus.readiness.missing_for_phases || []).join(', ') || 'creds'} (SSH server + APPS/WebLogic passwords).
                  </div>
                )}
                {cycleStatus.live?.output && (
                  <pre className="ph-log" style={{ marginTop: 6 }}>{cycleStatus.live.output}</pre>
                )}
                {cycleStatus.live?.error && (
                  <div className="st-meta" style={{ marginTop: 4 }}>live adop -status unavailable: {cycleStatus.live.error}</div>
                )}
              </div>
            )}

            {/* Phase results */}
            {phase === 'cycle' ? renderSteps(cycleSteps) : (run && renderSteps(run.steps))}
            <div ref={endRef} />
          </div>
        </div>

        <div className="patch-footer">
          {phase === 'interview' && (
            <form onSubmit={send} className="patch-input-row">
              <input
                ref={inputRef}
                value={input}
                onChange={e => setInput(e.target.value)}
                placeholder={field ? 'Type your answer…' : 'Starting…'}
                disabled={busy || !field}
              />
              <button type="submit" className="btn-primary" disabled={busy || !field || !input.trim()}>
                <Send size={14} />
              </button>
            </form>
          )}
          {phase === 'plan' && (
            <div className="patch-approval">
              <label className="patch-live-toggle" title="Execute over SSH using credentials resolved from the environment (read-only checks always go live when SSH is available; destructive phases only with this on). Falls back to the simulator if SSH/creds are missing.">
                <input type="checkbox" checked={liveMode} onChange={e => setLiveMode(e.target.checked)} />
                Run live over SSH (real adop / OPatch using environment credentials)
              </label>
              <div className="patch-actions">
                <button className="btn-outline" onClick={reset}>Start over</button>
                {hasAdopComponent(context.components) && (
                  <button className="btn-outline" onClick={startInteractive} disabled={busy} title="Drive adop prepare/apply/cutover one phase at a time">
                    <ListChecks size={14} /> Drive adop phase-by-phase
                  </button>
                )}
                <button className="btn-primary" onClick={startPatch} disabled={busy}>
                  {busy ? <Loader2 size={14} className="spin" /> : <Wrench size={14} />} Run {liveMode ? 'patch' : 'full simulated patch'}
                </button>
              </div>
            </div>
          )}
          {phase === 'error' && (
            <div className="patch-actions">
              <button className="btn-primary" onClick={reset}><RotateCcw size={14} /> Start over</button>
            </div>
          )}
          {phase === 'approval' && (
            <div className="patch-approval">
              <div className="patch-actions">
                <span className="patch-noperm">🔴 Production target — logged for approval by a different Admin/DBA (maker-checker).</span>
                <button className="btn-outline" onClick={reset}>Start over</button>
              </div>
            </div>
          )}
          {phase === 'cycle' && (
            <div className="patch-approval">
              <div className="patch-cyclebar">
                <button onClick={() => runPhase('status')} disabled={busy}><Activity size={13} /> Status</button>
                {PHASE_BTNS.map(([p, label]) => (
                  <button key={p} onClick={() => runPhase(p)}
                          disabled={busy || !allowed.includes(p)}
                          className={cycleStatus?.next_phase === p ? 'next' : ''}>
                    {label}
                  </button>
                ))}
                <button onClick={() => runPhase('abort')} disabled={busy || !allowed.includes('abort')} className="danger">Abort</button>
              </div>
              <div className="patch-actions">
                <button className="btn-outline" onClick={reset}><RotateCcw size={14} /> New patch</button>
                <button className="btn-primary" onClick={downloadRunbook}><Download size={14} /> Download runbook</button>
              </div>
            </div>
          )}
          {phase === 'done' && (
            <div className="patch-actions">
              <button className="btn-outline" onClick={reset}><RotateCcw size={14} /> New patch</button>
              <button className="btn-primary" onClick={downloadRunbook}><Download size={14} /> Download runbook</button>
            </div>
          )}
        </div>
        </>
        )}
      </div>
    </div>
  );
}

function PatchGapPanel() {
  const [environments, setEnvironments] = useState([]);
  const [environmentId, setEnvironmentId] = useState('');
  const [gaps, setGaps] = useState(null);
  const [error, setError] = useState('');
  const [scanning, setScanning] = useState(false);

  const INV_PAGE_SIZE = 50;
  const [invProduct, setInvProduct] = useState('');
  const [invFilenameQ, setInvFilenameQ] = useState('');
  const [invStatus, setInvStatus] = useState('');
  const [invPage, setInvPage] = useState(0);
  const [inventory, setInventory] = useState(null);
  const [invError, setInvError] = useState('');

  useEffect(() => {
    api.get('/config/environments').then(r => setEnvironments(r.data)).catch(() => {});
  }, []);

  const load = (envId) => {
    if (!envId) { setGaps(null); return; }
    setError('');
    api.get(`/patching/gap/${envId}`)
      .then(r => setGaps(r.data))
      .catch(e => setError(e.response?.data?.detail || e.message));
  };

  useEffect(() => { load(environmentId); }, [environmentId]);

  const loadInventory = () => {
    if (!environmentId) { setInventory(null); return; }
    setInvError('');
    const params = new URLSearchParams();
    if (invProduct) params.set('product', invProduct);
    if (invFilenameQ) params.set('filename', invFilenameQ);
    if (invStatus) params.set('match_status', invStatus);
    params.set('limit', INV_PAGE_SIZE);
    params.set('offset', invPage * INV_PAGE_SIZE);
    api.get(`/patching/gap/files/inventory/${environmentId}?${params.toString()}`)
      .then(r => setInventory(r.data))
      .catch(e => setInvError(e.response?.data?.detail || e.message));
  };

  useEffect(() => { setInvPage(0); }, [environmentId, invStatus]);
  useEffect(() => { loadInventory(); }, [environmentId, invStatus, invPage]);

  const scanNow = async () => {
    if (!environmentId) return;
    setScanning(true);
    setError('');
    try {
      await api.post(`/patching/gap/scan/${environmentId}`);
      setTimeout(() => load(environmentId), 2000);
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally {
      setScanning(false);
    }
  };

  const rows = gaps || [];

  return (
    <div className="admin-body" style={{ padding: '1rem' }}>
      <p className="admin-muted">
        Compares each component's admin-defined target patch list (Admin Console → Environments →
        Patch Targets) against the live <code>opatch lspatches</code> inventory (tech-stack homes) or
        <code>ad_bugs</code> (the <strong>adop</strong> component — application-tier patches). File-level
        drift (Admin Console → Environments → file-scan icon) compares the whole run filesystem against{' '}
        <code>ad_files</code>/<code>ad_file_versions</code> and surfaces mismatches in the Findings feed.
      </p>
      {error && <p className="admin-error">{error}</p>}
      <div className="admin-toolbar" style={{ gap: '0.5rem' }}>
        <select value={environmentId} onChange={e => setEnvironmentId(e.target.value)}>
          <option value="">Select an environment…</option>
          {environments.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
        </select>
        <button className="admin-btn-primary" disabled={!environmentId || scanning} onClick={scanNow}>
          {scanning ? <Loader2 size={14} className="spin" /> : null} {scanning ? 'Scanning…' : 'Scan now'}
        </button>
      </div>

      {environmentId && (
        <table className="admin-table">
          <thead><tr>
            <th>Component</th><th>Home</th><th>Target</th><th>Applied</th><th>Missing</th>
            <th>Last scanned</th><th>Status</th>
          </tr></thead>
          <tbody>
            {rows.map(g => (
              <tr key={g.component}>
                <td>{g.component}</td>
                <td className="admin-muted" style={{ fontSize: '0.75rem' }}>{g.home_path || '—'}</td>
                <td>{(g.target_patches || []).length}</td>
                <td>{(g.applied_patches || []).length}</td>
                <td>
                  {g.missing.length === 0
                    ? <span className="admin-pill ok">none</span>
                    : <span className="admin-pill off" title={g.missing.join(', ')}>{g.missing.length} missing</span>}
                </td>
                <td className="admin-muted">{g.last_scanned_at ? new Date(g.last_scanned_at).toLocaleString() : 'never'}</td>
                <td>{g.scan_status === 'ok'
                  ? <span className="admin-pill ok">ok</span>
                  : g.scan_status === 'never_scanned'
                    ? <span className="admin-muted">—</span>
                    : <span className="admin-pill off">{g.scan_status}</span>}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr><td colSpan={7} className="admin-muted">
                No patch targets defined for this environment yet — add one in Admin Console → Environments.
              </td></tr>
            )}
          </tbody>
        </table>
      )}

      {environmentId && (
        <div style={{ marginTop: '1.5rem', borderTop: '1px solid #FFFFFF', paddingTop: '1rem' }}>
          <h4 style={{ margin: '0 0 0.5rem' }}>File Inventory</h4>
          <p className="admin-muted">
            Every file the latest scan saw — filesystem version and AD_FILES-registered version, not just drift.
          </p>
          {invError && <p className="admin-error">{invError}</p>}
          <div className="admin-toolbar" style={{ gap: '0.5rem', flexWrap: 'wrap' }}>
            <input placeholder="Product (e.g. ap)" value={invProduct}
              onChange={e => setInvProduct(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { setInvPage(0); loadInventory(); } }} style={{ width: 120 }} />
            <input placeholder="Filename contains…" value={invFilenameQ}
              onChange={e => setInvFilenameQ(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') { setInvPage(0); loadInventory(); } }} style={{ width: 180 }} />
            <select value={invStatus} onChange={e => setInvStatus(e.target.value)}>
              <option value="">All statuses</option>
              <option value="match">Match</option>
              <option value="mismatch">Mismatch</option>
              <option value="fs_only">Not in AD_FILES</option>
            </select>
            <button className="admin-btn-ghost" onClick={() => { setInvPage(0); loadInventory(); }}>
              <RefreshCw size={13} /> Apply
            </button>
          </div>
          <table className="admin-table">
            <thead><tr>
              <th>Product</th><th>Filename</th><th>Path</th><th>FS version</th>
              <th>AD_FILES version</th><th>Status</th><th>Last scanned</th>
            </tr></thead>
            <tbody>
              {(inventory?.items || []).map(r => (
                <tr key={r.id}>
                  <td>{r.product}</td>
                  <td>{r.filename}</td>
                  <td className="admin-muted" style={{ fontSize: '0.75rem' }}>{r.path}</td>
                  <td>{r.fs_version || '—'}</td>
                  <td>{r.db_version || '—'}</td>
                  <td>
                    {r.match_status === 'match' && <span className="admin-pill ok">match</span>}
                    {r.match_status === 'mismatch' && <span className="admin-pill off">mismatch</span>}
                    {r.match_status === 'fs_only' && <span className="admin-pill pend">not in AD_FILES</span>}
                  </td>
                  <td className="admin-muted">{new Date(r.last_scanned_at).toLocaleString()}</td>
                </tr>
              ))}
              {inventory && inventory.items.length === 0 && (
                <tr><td colSpan={7} className="admin-muted">No files match these filters.</td></tr>
              )}
              {!inventory && !invError && (
                <tr><td colSpan={7} className="admin-muted">Loading…</td></tr>
              )}
            </tbody>
          </table>
          {inventory && inventory.total > 0 && (
            <div className="admin-toolbar" style={{ justifyContent: 'space-between' }}>
              <span className="admin-muted">
                {invPage * INV_PAGE_SIZE + 1}–{Math.min((invPage + 1) * INV_PAGE_SIZE, inventory.total)} of {inventory.total}
              </span>
              <div style={{ display: 'flex', gap: '0.5rem' }}>
                <button className="admin-btn-ghost" disabled={invPage === 0} onClick={() => setInvPage(p => p - 1)}>Prev</button>
                <button className="admin-btn-ghost" disabled={(invPage + 1) * INV_PAGE_SIZE >= inventory.total}
                  onClick={() => setInvPage(p => p + 1)}>Next</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
