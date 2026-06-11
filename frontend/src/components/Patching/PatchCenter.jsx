import { useState, useEffect, useRef } from 'react';
import api from '../../api';
import ReactMarkdown from 'react-markdown';
import { Send, Download, RotateCcw, Loader2, CheckCircle2, Wrench, ListChecks, Activity } from 'lucide-react';
import './PatchCenter.css';

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
            <span className="patch-sub">DB / Grid / RAC OPatch · adop · WebLogic / FMW · simulator</span></h3>
          <button className="patch-close" onClick={onClose}>×</button>
        </div>

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
      </div>
    </div>
  );
}
