import { useState, useEffect } from 'react';
import api from '../../api';
import { MessageCircleQuestion, Loader2, Play, AlertTriangle } from 'lucide-react';
import '../Admin/AdminConsole.css';

export default function NlSqlAgent({ onClose }) {
  const [environments, setEnvironments] = useState([]);
  const [environmentId, setEnvironmentId] = useState('');
  const [schemaStatus, setSchemaStatus] = useState(null);
  const [question, setQuestion] = useState('');
  const [asking, setAsking] = useState(false);
  const [proposal, setProposal] = useState(null);   // QueryResult dict
  const [sql, setSql] = useState('');
  const [running, setRunning] = useState(false);
  const [rows, setRows] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    api.get('/config/environments').then(r => setEnvironments(r.data)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!environmentId) { setSchemaStatus(null); return; }
    api.get(`/nl-sql/schema/${environmentId}`).then(r => setSchemaStatus(r.data)).catch(() => {});
  }, [environmentId]);

  const ask = async () => {
    if (!environmentId || !question.trim() || asking) return;
    setAsking(true); setError(''); setProposal(null); setRows(null);
    try {
      const r = await api.post('/nl-sql/query', {
        environment_id: parseInt(environmentId), question: question.trim(),
      });
      setProposal(r.data);
      setSql(r.data.sql || '');
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally {
      setAsking(false);
    }
  };

  const run = async () => {
    if (!sql.trim() || running) return;
    setRunning(true); setError(''); setRows(null);
    try {
      const r = await api.post('/nl-sql/execute', {
        environment_id: parseInt(environmentId), sql: sql.trim(), max_rows: 50,
      });
      setRows(r.data.rows || []);
    } catch (e) {
      setError(e.response?.data?.detail || e.message);
    } finally {
      setRunning(false);
    }
  };

  const badge = (label, ok) => (
    <span className={`admin-pill ${ok === true ? 'ok' : ok === false ? 'off' : 'admin'}`}>
      {label}: {ok === true ? 'yes' : ok === false ? 'no' : 'n/a'}
    </span>
  );

  return (
    <div className="admin-overlay">
      <div className="admin-container">
        <div className="admin-header">
          <h3><MessageCircleQuestion size={18} style={{ verticalAlign: 'middle', marginRight: 8 }} />Ask Your Data
            <span className="admin-muted" style={{ marginLeft: 10, fontWeight: 400, fontSize: '0.75rem' }}>
              NL → Oracle SQL (TinyLLM)
            </span>
          </h3>
          <button className="admin-close" onClick={onClose}>×</button>
        </div>

        <div className="admin-body">
          <div className="admin-toolbar" style={{ gap: '0.5rem' }}>
            <select value={environmentId} onChange={e => setEnvironmentId(e.target.value)}>
              <option value="">Select an environment…</option>
              {environments.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
            </select>
          </div>

          {environmentId && schemaStatus?.status === 'never_extracted' && (
            <p className="admin-muted" style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
              <AlertTriangle size={14} />
              Using the generic demo schema — no environment-specific extraction yet. Ask an admin
              to set this up in Admin Console → Environments → NL-SQL Setup for much better accuracy.
            </p>
          )}
          {environmentId && schemaStatus?.status === 'error' && (
            <p className="admin-error">Last schema extraction failed: {schemaStatus.scan_error}</p>
          )}

          <div className="admin-toolbar" style={{ gap: '0.5rem' }}>
            <input style={{ flex: 1 }} placeholder="e.g. show me the top 5 vendors by invoice amount"
              value={question} onChange={e => setQuestion(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && ask()} disabled={!environmentId} />
            <button className="admin-btn-primary" onClick={ask} disabled={!environmentId || !question.trim() || asking}>
              {asking ? <Loader2 size={14} className="spin" /> : null} {asking ? 'Thinking…' : 'Ask'}
            </button>
          </div>

          {error && <p className="admin-error">{error}</p>}

          {proposal && (
            <div style={{ marginTop: '0.8rem' }}>
              <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap', marginBottom: '0.4rem' }}>
                {badge('schema-valid', proposal.graph_valid)}
                {badge('constrained', proposal.constrained)}
                {badge('explain-ok', proposal.explain_ok)}
              </div>
              {proposal.note && <p className="admin-muted">{proposal.note}</p>}
              {proposal.tables_used?.length > 0 && (
                <p className="admin-muted">Tables used: {proposal.tables_used.join(', ')}</p>
              )}
              <textarea rows={4} style={{ width: '100%', fontFamily: 'monospace', fontSize: '0.82rem' }}
                value={sql} onChange={e => setSql(e.target.value)} />
              <p className="admin-muted" style={{ fontSize: '0.72rem' }}>
                Review (and edit if needed) before running — this is proposed SQL from an 8M-parameter
                model, not guaranteed correct. Only SELECT statements can be run.
              </p>
              <button className="admin-btn-primary" onClick={run} disabled={!sql.trim() || running}>
                {running ? <Loader2 size={14} className="spin" /> : <Play size={14} />} {running ? 'Running…' : 'Run'}
              </button>
            </div>
          )}

          {rows && (
            <div style={{ marginTop: '0.8rem' }}>
              <p className="admin-muted">{rows.length} row(s)</p>
              <table className="admin-table">
                <tbody>
                  {rows.map((row, i) => (
                    <tr key={i}>{row.map((cell, j) => <td key={j}>{String(cell)}</td>)}</tr>
                  ))}
                  {rows.length === 0 && <tr><td className="admin-muted">No rows returned.</td></tr>}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
