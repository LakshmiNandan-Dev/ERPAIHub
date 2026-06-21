import { useState, useRef, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { Users, X, Loader2, Search } from 'lucide-react';
import api from '../../api';
import '../Performance/PerformanceAgent.css';
import './HcmAgent.css';

const LABEL_MAP = { idle: 'Idle', running: 'Working...', complete: 'Complete', error: 'Error' };

export default function HcmAgent({ onClose }) {
  // Admin-managed environments (read-only; no secrets) from /config
  const [environments, setEnvironments] = useState([]);
  const [selectedEnv, setSelectedEnv] = useState('');

  // Inquiry catalog
  const [catalog, setCatalog] = useState([]);
  const [inquiryId, setInquiryId] = useState('');
  const [params, setParams] = useState({});

  // Shared run/stream state
  const [mode, setMode] = useState('inquiry');        // 'inquiry' | 'ask'
  const [phase, setPhase] = useState('');
  const [analysisText, setAnalysisText] = useState('');
  const [status, setStatus] = useState('idle');
  const [running, setRunning] = useState(false);
  const [rows, setRows] = useState(null);
  const [source, setSource] = useState('');
  const [question, setQuestion] = useState('');
  const analysisRef = useRef(null);
  const readerRef = useRef(null);

  const resetParams = (inquiry) => {
    setParams(Object.fromEntries((inquiry?.params || []).map(p => [p.name, p.default ?? ''])));
  };

  useEffect(() => {
    api.get('/config/environments')
      .then(r => { setEnvironments(r.data); if (r.data[0]?.name) setSelectedEnv(r.data[0].name); })
      .catch(() => {});
    api.get('/hcm/inquiries')
      .then(r => {
        const list = r.data.inquiries || [];
        setCatalog(list);
        if (list[0]) { setInquiryId(list[0].id); resetParams(list[0]); }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (analysisRef.current) analysisRef.current.scrollTop = analysisRef.current.scrollHeight;
  }, [analysisText]);

  const selectedInquiry = catalog.find(q => q.id === inquiryId) || null;

  const onInquiryChange = (id) => {
    setInquiryId(id);
    resetParams(catalog.find(q => q.id === id));
    setRows(null); setAnalysisText(''); setPhase(''); setStatus('idle');
  };

  const getLLMHeaders = () => {
    try {
      const llm = JSON.parse(localStorage.getItem('llm_config') || '{}');
      return {
        'X-LLM-Provider': llm.provider || 'ollama',
        'X-LLM-Model': llm.model || '',
        'X-LLM-Base-Url': llm.base_url || '',
      };
    } catch { return { 'X-LLM-Provider': 'ollama' }; }
  };

  const getEnvCredentials = () => {
    const env = environments.find(e => e.name === selectedEnv) || {};
    return {
      db_host: env.db_host || null,
      db_port: env.db_port || 1521,
      db_sid: env.db_sid || null,
      db_user: env.db_user || null,
      db_password: env.db_password || null,
    };
  };

  // Shared SSE reader
  const readStream = async (resp, onEvent) => {
    const reader = resp.body.getReader();
    readerRef.current = reader;
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
        if (raw === '[DONE]') { setStatus(s => (s === 'error' ? s : 'complete')); return; }
        try { onEvent(JSON.parse(raw)); } catch { /* malformed */ }
      }
    }
  };

  const handleEvent = (evt) => {
    if (evt.type === 'phase') setPhase(evt.content);
    if (evt.type === 'data') { setRows(evt.rows || []); setSource(evt.source || ''); }
    if (evt.type === 'analysis_token') setAnalysisText(prev => prev + evt.content);
    if (evt.type === 'analysis_complete') setStatus('complete');
    if (evt.type === 'error') { setPhase(`❌ ${evt.content}`); setStatus('error'); }
  };

  const runInquiry = async () => {
    if (running || !inquiryId) return;
    setRunning(true); setStatus('running'); setPhase(''); setAnalysisText(''); setRows(null); setSource('');
    const token = localStorage.getItem('session_token');
    try {
      const resp = await fetch(`${api.defaults.baseURL}/hcm/inquiry`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...getLLMHeaders() },
        body: JSON.stringify({
          inquiry_id: inquiryId,
          params,
          environment: selectedEnv || 'EBS',
          interpret: true,
          ...getEnvCredentials(),
        }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await readStream(resp, handleEvent);
    } catch (err) {
      setPhase(`❌ Connection error: ${err.message}`); setStatus('error');
    } finally {
      setRunning(false);
    }
  };

  const runAsk = async () => {
    if (running || !question.trim()) return;
    setRunning(true); setStatus('running'); setPhase(''); setAnalysisText(''); setRows(null);
    const token = localStorage.getItem('session_token');
    try {
      const resp = await fetch(`${api.defaults.baseURL}/hcm/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...getLLMHeaders() },
        body: JSON.stringify({ question }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await readStream(resp, handleEvent);
    } catch (err) {
      setPhase(`❌ Connection error: ${err.message}`); setStatus('error');
    } finally {
      setRunning(false);
    }
  };

  const stop = () => { readerRef.current?.cancel(); setRunning(false); setStatus('idle'); setPhase(''); };

  const columns = rows && rows.length > 0 ? Object.keys(rows[0]) : [];

  return (
    <div className="perf-modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="perf-modal glass">

        <div className="perf-header">
          <div className="perf-header-left">
            <Users size={20} style={{ color: '#60A5FA' }} />
            <h2>Oracle EBS HCM &amp; Payroll Agent <span className="hcm-ro-pill">read-only</span></h2>
          </div>
          <button className="perf-close-btn" onClick={onClose}>&times;</button>
        </div>

        <div className="perf-mode-tabs">
          <button className={`perf-mode-tab ${mode === 'inquiry' ? 'active' : ''}`}
            onClick={() => { if (!running) { setMode('inquiry'); setAnalysisText(''); setPhase(''); setStatus('idle'); } }}
            disabled={running}>
            🔎 Inquiry
          </button>
          <button className={`perf-mode-tab ${mode === 'ask' ? 'active' : ''}`}
            onClick={() => { if (!running) { setMode('ask'); setAnalysisText(''); setPhase(''); setStatus('idle'); setRows(null); } }}
            disabled={running}>
            💬 Ask (Advisor)
          </button>
        </div>

        <div className="perf-body">
          {/* Left panel */}
          <div className="perf-left">
            {mode === 'inquiry' ? (
              <div className="perf-config">
                <div>
                  <div className="perf-config-label">Target Environment</div>
                  {environments.length > 0 ? (
                    <select className="perf-env-select" value={selectedEnv}
                      onChange={e => setSelectedEnv(e.target.value)} disabled={running}>
                      {environments.map(env => (
                        <option key={env.name} value={env.name}>{env.name} — {env.db_host || 'No host'}</option>
                      ))}
                    </select>
                  ) : (
                    <div className="hcm-note">No environments configured — inquiries run on simulated data.</div>
                  )}
                </div>

                <div>
                  <div className="perf-config-label">Inquiry</div>
                  <select className="perf-env-select" value={inquiryId}
                    onChange={e => onInquiryChange(e.target.value)} disabled={running}>
                    {catalog.map(q => <option key={q.id} value={q.id}>{q.label}</option>)}
                  </select>
                  {selectedInquiry && <div className="hcm-inquiry-desc">{selectedInquiry.description}</div>}
                </div>

                {(selectedInquiry?.params || []).map(p => (
                  <div key={p.name}>
                    <div className="perf-config-label">
                      {p.label}{p.required ? ' *' : ''}
                    </div>
                    <input
                      className="hcm-input"
                      type="text"
                      value={params[p.name] ?? ''}
                      placeholder={p.help || (p.required ? 'required' : 'optional')}
                      onChange={e => setParams(prev => ({ ...prev, [p.name]: e.target.value }))}
                      disabled={running}
                    />
                  </div>
                ))}

                {running ? (
                  <button className="btn-outline perf-run-btn" onClick={stop}><X size={15} /> Stop</button>
                ) : (
                  <button className="btn-primary perf-run-btn" onClick={runInquiry} disabled={!inquiryId}>
                    <Search size={15} /> Run Inquiry
                  </button>
                )}

                {rows && (
                  <div className="hcm-results">
                    <div className="hcm-results-head">
                      Results — {rows.length} row{rows.length === 1 ? '' : 's'}
                      <span className={`hcm-source-badge ${source}`}>{source === 'live' ? '🟢 live' : '🧪 simulated'}</span>
                    </div>
                    {rows.length > 0 ? (
                      <div className="hcm-table-wrap">
                        <table className="perf-metric-table">
                          <thead><tr>{columns.map(c => <th key={c}>{c}</th>)}</tr></thead>
                          <tbody>
                            {rows.slice(0, 50).map((r, i) => (
                              <tr key={i}>
                                {columns.map(c => <td key={c} title={String(r[c] ?? '')}>{String(r[c] ?? '')}</td>)}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <div className="hcm-note">No rows returned. Try a different parameter or effective date.</div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="perf-config">
                <div className="perf-config-label">Ask an HCM / Payroll functional question</div>
                <textarea
                  className="hcm-input hcm-textarea"
                  rows={6}
                  value={question}
                  placeholder="e.g. What is the Oracle EBS payroll process flow from run to GL transfer?"
                  onChange={e => setQuestion(e.target.value)}
                  disabled={running}
                />
                {running ? (
                  <button className="btn-outline perf-run-btn" onClick={stop}><X size={15} /> Stop</button>
                ) : (
                  <button className="btn-primary perf-run-btn" onClick={runAsk} disabled={!question.trim()}>
                    💬 Ask
                  </button>
                )}
                <div className="hcm-note">Read-only advisor — grounded in the knowledge base when a match is found.</div>
              </div>
            )}
          </div>

          {/* Right panel */}
          <div className="perf-right">
            <div className="perf-analysis-header">
              <h3>🤖 {mode === 'inquiry' ? 'Inquiry Interpretation' : 'Functional Answer'}</h3>
              <span className={`perf-status-badge ${status}`}>{LABEL_MAP[status]}</span>
            </div>

            {phase && (
              <div className="perf-phase-bar">
                {running && <Loader2 size={13} style={{ animation: 'spin 1s linear infinite', flexShrink: 0 }} />}
                {phase}
              </div>
            )}

            <div className="perf-analysis-body" ref={analysisRef}>
              {!analysisText && !running && (
                <div className="perf-empty-state">
                  <Users size={48} />
                  <p>
                    {mode === 'inquiry'
                      ? <>Pick an inquiry, optionally enter parameters, then click <strong>Run Inquiry</strong>. Results are read-only; with no environment configured they use simulated HCM data.</>
                      : <>Ask any Oracle EBS HCM/Payroll functional question and click <strong>Ask</strong>.</>}
                  </p>
                </div>
              )}
              {analysisText && (
                <div className="perf-analysis-md">
                  <ReactMarkdown>{analysisText}</ReactMarkdown>
                  {running && <span className="perf-cursor" />}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
