import { useState, useEffect, useRef } from 'react';
import api from '../../api';
import ReactMarkdown from 'react-markdown';
import { X, Send, Download, RotateCcw, Loader2, CheckCircle2, GitFork } from 'lucide-react';
import './CloneCenter.css';

const NODE_LABEL = {
  'controller': 'Controller',
  'source-db': 'Source DB',
  'source-apps': 'Source Apps',
  'target-db': 'Target DB',
  'target-apps': 'Target Apps',
};

export default function CloneCenter({ onClose }) {
  const [messages, setMessages] = useState([]);
  const [context, setContext] = useState({});
  const [field, setField] = useState(null);
  const [phase, setPhase] = useState('interview');   // interview | plan | blocked | error | done
  const [input, setInput] = useState('');
  const [run, setRun] = useState(null);
  const [busy, setBusy] = useState(false);
  const endRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => { ask({}); /* eslint-disable-next-line */ }, []);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages, run]);

  const ask = async (ctx) => {
    setBusy(true);
    try {
      const r = (await api.post('/cloning/agent', { context: ctx })).data;
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

  const startClone = async () => {
    setBusy(true);
    try {
      const body = { ...context };
      const r = (await api.post('/cloning/', body)).data;
      setRun(r);
      setPhase('done');
      setMessages(m => [...m, { role: 'agent', content: `✅ Simulated clone **${r.source_name} → ${r.target_name}** completed — ${(r.steps || []).length} phases. Download the runbook below.` }]);
    } catch (e) {
      const detail = e.response?.data?.detail;
      if (e.response?.status === 409 && detail?.guard) {
        const reasons = (detail.guard.reasons || []).map(x => `- ${x}`).join('\n');
        const rid = detail.run_id;
        setMessages(m => [...m, { role: 'agent', content:
          `🛑 **${detail.message}**\n\n${reasons}\n\n` +
          `This request has been logged as clone **#${rid}** and now requires override approval by a ` +
          `**different** Admin/DBA (separation of duties — you cannot approve your own request). ` +
          `An approver can review it in **Admin Console → Audit → Clones**.` }]);
        setPhase('blocked');
      } else {
        alert(typeof detail === 'string' ? detail : (e.message));
      }
    } finally { setBusy(false); }
  };

  const downloadRunbook = async () => {
    try {
      const res = await api.get(`/cloning/${run.id}/runbook`, { responseType: 'blob' });
      const cd = res.headers['content-disposition'] || '';
      const mm = cd.match(/filename="?([^"]+)"?/);
      const fn = mm ? mm[1] : `oraebs_clone_${run.id}.zip`;
      const url = URL.createObjectURL(new Blob([res.data], { type: 'application/zip' }));
      const a = document.createElement('a'); a.href = url; a.download = fn;
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    } catch (e) { alert(e.response?.data?.detail || e.message); }
  };

  const reset = () => {
    setMessages([]); setContext({}); setField(null); setRun(null);
    setCanOverride(false); setOverrideText(''); setPhase('interview'); ask({});
  };

  return (
    <div className="clone-overlay">
      <div className="clone-container">
        <div className="clone-header">
          <h3><GitFork size={18} style={{ verticalAlign: 'middle', marginRight: 8 }} />EBS Cloning Agent
            <span className="clone-sub">RMAN duplicate (DB) + Rapid Clone (apps) · simulator</span></h3>
          <button className="clone-close" onClick={onClose}>×</button>
        </div>

        <div className="clone-body">
          {/* Conversation */}
          <div className="clone-chat">
            {messages.map((m, i) => (
              <div key={i} className={`clone-msg ${m.role}`}>
                <div className="clone-bubble">
                  {m.role === 'agent'
                    ? <div className="md"><ReactMarkdown>{m.content}</ReactMarkdown></div>
                    : m.content}
                </div>
              </div>
            ))}
            {busy && <div className="clone-msg agent"><div className="clone-bubble"><Loader2 size={14} className="spin" /> working…</div></div>}

            {/* Phase results */}
            {run && (
              <div className="clone-phases">
                {(run.steps || []).map(s => (
                  <details key={s.step} className="clone-phase" open={s.step <= 2}>
                    <summary>
                      <CheckCircle2 size={14} className="ok" />
                      <span className="ph-title">{s.step}. {s.title}</span>
                      <span className={`ph-node ${s.node}`}>{NODE_LABEL[s.node] || s.node}</span>
                    </summary>
                    <pre className="ph-cmd">{s.command}</pre>
                    <pre className="ph-log">{s.log}</pre>
                  </details>
                ))}
              </div>
            )}
            <div ref={endRef} />
          </div>
        </div>

        {/* Footer / action bar */}
        <div className="clone-footer">
          {phase === 'interview' && (
            <form onSubmit={send} className="clone-input-row">
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
            <div className="clone-actions">
              <button className="btn-outline" onClick={reset}>Start over</button>
              <button className="btn-primary" onClick={() => startClone()} disabled={busy}>
                {busy ? <Loader2 size={14} className="spin" /> : <GitFork size={14} />} Run simulated clone
              </button>
            </div>
          )}
          {phase === 'error' && (
            <div className="clone-actions">
              <button className="btn-primary" onClick={reset}><RotateCcw size={14} /> Start over</button>
            </div>
          )}
          {phase === 'blocked' && (
            <div className="clone-blocked">
              <div className="clone-actions">
                <span className="clone-noperm">🛑 Blocked by the production guard — logged for override approval by a different Admin/DBA.</span>
                <button className="btn-outline" onClick={reset}>Start over</button>
              </div>
            </div>
          )}
          {phase === 'done' && (
            <div className="clone-actions">
              <button className="btn-outline" onClick={reset}><RotateCcw size={14} /> New clone</button>
              <button className="btn-primary" onClick={downloadRunbook}><Download size={14} /> Download runbook</button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
