import { useState, useRef, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { Activity, X, ChevronDown, ChevronRight, Loader2, Upload } from 'lucide-react';
import api from '../../api';
import './PerformanceAgent.css';

const AREA_META = {
  wait_events:        { label: 'Wait Events',        icon: '⏱️' },
  top_sql:            { label: 'Top SQL',             icon: '🗃️' },
  memory:             { label: 'Memory (SGA/PGA)',    icon: '🧠' },
  locks:              { label: 'Lock Analysis',       icon: '🔒' },
  tablespace:         { label: 'Tablespace Usage',    icon: '💾' },
  concurrent_manager: { label: 'Concurrent Manager',  icon: '⚙️' },
  statistics:         { label: 'Object Statistics',   icon: '📈' },
};

const ALL_AREAS = Object.keys(AREA_META);

const LOAD_PROFILE_META = [
  { key: 'db_time_per_s',        label: 'DB Time',        fmt: v => `${v} s/s`,                              higherIsBetter: false },
  { key: 'db_cpu_per_s',         label: 'DB CPU',         fmt: v => `${v} s/s`,                              higherIsBetter: false },
  { key: 'physical_reads_per_s', label: 'Physical Reads', fmt: v => Number(v).toLocaleString() + '/s',       higherIsBetter: false },
  { key: 'logical_reads_per_s',  label: 'Logical Reads',  fmt: v => Number(v).toLocaleString() + '/s',       higherIsBetter: false },
  { key: 'redo_bytes_per_s',     label: 'Redo Size',      fmt: v => `${(v / 1048576).toFixed(2)} MB/s`,      higherIsBetter: false },
  { key: 'user_calls_per_s',     label: 'User Calls',     fmt: v => `${v}/s`,                                higherIsBetter: false },
  { key: 'parses_per_s',         label: 'Parses',         fmt: v => `${v}/s`,                                higherIsBetter: false },
  { key: 'hard_parses_per_s',    label: 'Hard Parses',    fmt: v => `${v}/s`,                                higherIsBetter: false },
  { key: 'transactions_per_s',   label: 'Transactions',   fmt: v => `${v}/s`,                                higherIsBetter: true  },
];

const EFFICIENCY_META = [
  { key: 'buffer_cache_hit_pct',  label: 'Buffer Cache Hit%',  fmt: v => `${v}%`, higherIsBetter: true },
  { key: 'library_cache_hit_pct', label: 'Library Cache Hit%', fmt: v => `${v}%`, higherIsBetter: true },
  { key: 'soft_parse_pct',        label: 'Soft Parse%',        fmt: v => `${v}%`, higherIsBetter: true },
  { key: 'execute_to_parse_pct',  label: 'Execute to Parse%',  fmt: v => `${v}%`, higherIsBetter: true },
  { key: 'latch_hit_pct',         label: 'Latch Hit%',         fmt: v => `${v}%`, higherIsBetter: true },
];

export default function PerformanceAgent({ onClose }) {
  // Admin-managed environments (read-only; no secrets) from /config
  const [environments, setEnvironments] = useState([]);
  const [selectedEnv, setSelectedEnv] = useState('');

  useEffect(() => {
    api.get('/config/environments')
      .then(r => { setEnvironments(r.data); if (r.data[0]?.name) setSelectedEnv(r.data[0].name); })
      .catch(() => {});
  }, []);

  // ── Shared state ──────────────────────────────────────────────────────────────
  const [phase, setPhase]             = useState('');
  const [analysisText, setAnalysisText] = useState('');
  const [status, setStatus]           = useState('idle');
  const analysisRef                   = useRef(null);

  // ── Mode state ────────────────────────────────────────────────────────────────
  const [activeMode, setActiveMode] = useState('live');       // 'live' | 'awr'
  const [awrSubTab, setAwrSubTab]   = useState('snapshots');  // 'snapshots' | 'upload'

  // ── Live diagnostics state ────────────────────────────────────────────────────
  const [selectedAreas, setSelectedAreas] = useState(new Set(ALL_AREAS));
  const [running, setRunning]             = useState(false);
  const [progress, setProgress]           = useState([]);
  const [diagnostics, setDiagnostics]     = useState(null);
  const [expandedCards, setExpandedCards] = useState(new Set(ALL_AREAS));
  const readerRef                         = useRef(null);

  // ── AWR snapshot comparison state ─────────────────────────────────────────────
  const [snapshots, setSnapshots]         = useState([]);
  const [snapsLoading, setSnapsLoading]   = useState(false);
  const [baselineBegin, setBaselineBegin] = useState('');
  const [baselineEnd, setBaselineEnd]     = useState('');
  const [compareBegin, setCompareBegin]   = useState('');
  const [compareEnd, setCompareEnd]       = useState('');
  const [awrBaselineData, setAwrBaselineData] = useState(null);
  const [awrCompareData, setAwrCompareData]   = useState(null);

  // ── AWR upload state ──────────────────────────────────────────────────────────
  const [uploadFile1, setUploadFile1] = useState(null);
  const [uploadFile2, setUploadFile2] = useState(null);
  const [dragOver1, setDragOver1]     = useState(false);
  const [dragOver2, setDragOver2]     = useState(false);
  const [parsedInfo, setParsedInfo]   = useState({ baseline: null, compare: null });

  // ── AWR shared ────────────────────────────────────────────────────────────────
  const [awrRunning, setAwrRunning] = useState(false);
  const awrReaderRef                = useRef(null);

  // ── Effects ───────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (analysisRef.current) analysisRef.current.scrollTop = analysisRef.current.scrollHeight;
  }, [analysisText]);

  useEffect(() => {
    if (activeMode !== 'awr' || !selectedEnv) return;
    setSnapsLoading(true);
    const token = localStorage.getItem('session_token');
    fetch(`${api.defaults.baseURL}/performance/snapshots?environment=${encodeURIComponent(selectedEnv)}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then(r => r.json())
      .then(data => {
        setSnapshots(data);
        if (data.length >= 10) {
          setBaselineBegin(String(data[1].snap_id));
          setBaselineEnd(String(data[8].snap_id));
          setCompareBegin(String(data[data.length - 10].snap_id));
          setCompareEnd(String(data[data.length - 2].snap_id));
        }
      })
      .catch(() => setSnapshots([]))
      .finally(() => setSnapsLoading(false));
  }, [activeMode, selectedEnv]);

  useEffect(() => {
    setAnalysisText('');
    setPhase('');
    setStatus('idle');
  }, [activeMode]);

  // ── Shared helpers ────────────────────────────────────────────────────────────
  const getLLMHeaders = () => {
    try {
      const llm = JSON.parse(localStorage.getItem('llm_config') || '{}');
      return {
        'X-LLM-Provider': llm.provider  || 'ollama',
        'X-LLM-Model':    llm.model     || '',
        'X-LLM-Base-Url': llm.base_url  || '',
      };
    } catch { return { 'X-LLM-Provider': 'ollama' }; }
  };

  const getEnvCredentials = () => {
    const env = environments.find(e => e.name === selectedEnv) || {};
    return {
      db_host:     env.db_host     || null,
      db_port:     env.db_port     || 1521,
      db_sid:      env.db_sid      || null,
      db_user:     env.db_user     || null,
      db_password: env.db_password || null,
    };
  };

  // ── Live diagnostics handlers ─────────────────────────────────────────────────
  const toggleArea = (area) => {
    setSelectedAreas(prev => {
      const next = new Set(prev);
      next.has(area) ? next.delete(area) : next.add(area);
      return next;
    });
  };

  const toggleCard = (area) => {
    setExpandedCards(prev => {
      const next = new Set(prev);
      next.has(area) ? next.delete(area) : next.add(area);
      return next;
    });
  };

  const runAnalysis = async () => {
    if (running || selectedAreas.size === 0) return;
    setRunning(true);
    setStatus('running');
    setPhase('');
    setProgress([]);
    setDiagnostics(null);
    setAnalysisText('');

    const token = localStorage.getItem('session_token');
    const body  = JSON.stringify({
      environment:    selectedEnv || 'EBS',
      analysis_areas: [...selectedAreas],
      ...getEnvCredentials(),
    });

    try {
      const resp = await fetch(`${api.defaults.baseURL}/performance/analyze`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}`, ...getLLMHeaders() },
        body,
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      const reader  = resp.body.getReader();
      readerRef.current = reader;
      const decoder = new TextDecoder();
      let   buf     = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop();
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (raw === '[DONE]') { setStatus('complete'); break; }
          try {
            const evt = JSON.parse(raw);
            if (evt.type === 'phase')    { setPhase(evt.content); }
            if (evt.type === 'progress') {
              setProgress(prev => {
                const existing = prev.find(p => p.area === evt.area);
                if (existing) return prev.map(p => p.area === evt.area ? { ...p, msg: evt.content } : p);
                return [...prev, { area: evt.area, msg: evt.content, done: false }];
              });
            }
            if (evt.type === 'diagnostics') {
              setDiagnostics(evt.data);
              setProgress(prev => prev.map(p => ({ ...p, done: true })));
            }
            if (evt.type === 'analysis_token')   { setAnalysisText(prev => prev + evt.content); }
            if (evt.type === 'analysis_complete') { setStatus('complete'); }
            if (evt.type === 'error') { setPhase(`❌ ${evt.content}`); setStatus('error'); }
          } catch { /* malformed */ }
        }
      }
    } catch (err) {
      setPhase(`❌ Connection error: ${err.message}`);
      setStatus('error');
    } finally {
      setRunning(false);
    }
  };

  const stopAnalysis = () => {
    readerRef.current?.cancel();
    setRunning(false);
    setStatus('idle');
    setPhase('');
  };

  // ── AWR SSE stream reader ─────────────────────────────────────────────────────
  const _readAwrStream = async (resp, onEvent) => {
    const reader  = resp.body.getReader();
    awrReaderRef.current = reader;
    const decoder = new TextDecoder();
    let   buf     = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') { setStatus('complete'); return; }
        try { onEvent(JSON.parse(raw)); } catch { /* malformed */ }
      }
    }
  };

  // ── AWR Snapshot Comparison ───────────────────────────────────────────────────
  const runAwrCompare = async () => {
    if (awrRunning || !baselineBegin || !baselineEnd || !compareBegin || !compareEnd) return;
    setAwrRunning(true);
    setStatus('running');
    setPhase('');
    setAnalysisText('');
    setAwrBaselineData(null);
    setAwrCompareData(null);

    const token = localStorage.getItem('session_token');
    try {
      const resp = await fetch(`${api.defaults.baseURL}/performance/awr/compare`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}`, ...getLLMHeaders() },
        body: JSON.stringify({
          environment:         selectedEnv || 'EBS',
          baseline_begin_snap: parseInt(baselineBegin),
          baseline_end_snap:   parseInt(baselineEnd),
          compare_begin_snap:  parseInt(compareBegin),
          compare_end_snap:    parseInt(compareEnd),
        }),
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await _readAwrStream(resp, (evt) => {
        if (evt.type === 'phase')            setPhase(evt.content);
        if (evt.type === 'baseline')         setAwrBaselineData(evt.data);
        if (evt.type === 'compare')          setAwrCompareData(evt.data);
        if (evt.type === 'analysis_token')   setAnalysisText(prev => prev + evt.content);
        if (evt.type === 'analysis_complete') setStatus('complete');
        if (evt.type === 'error') { setPhase(`❌ ${evt.content}`); setStatus('error'); }
      });
    } catch (err) {
      setPhase(`❌ Connection error: ${err.message}`);
      setStatus('error');
    } finally {
      setAwrRunning(false);
    }
  };

  // ── AWR Upload ────────────────────────────────────────────────────────────────
  const runAwrUpload = async () => {
    if (awrRunning || !uploadFile1) return;
    setAwrRunning(true);
    setStatus('running');
    setPhase('');
    setAnalysisText('');
    setParsedInfo({ baseline: null, compare: null });

    const token    = localStorage.getItem('session_token');
    const formData = new FormData();
    formData.append('file1', uploadFile1);
    if (uploadFile2) formData.append('file2', uploadFile2);
    formData.append('environment', selectedEnv || 'EBS');

    try {
      const resp = await fetch(`${api.defaults.baseURL}/performance/awr/upload`, {
        method:  'POST',
        headers: { 'Authorization': `Bearer ${token}`, ...getLLMHeaders() },
        body:    formData,
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      await _readAwrStream(resp, (evt) => {
        if (evt.type === 'phase')            setPhase(evt.content);
        if (evt.type === 'parsed')           setParsedInfo(prev => ({ ...prev, [evt.slot]: evt.data }));
        if (evt.type === 'analysis_token')   setAnalysisText(prev => prev + evt.content);
        if (evt.type === 'analysis_complete') setStatus('complete');
        if (evt.type === 'error') { setPhase(`❌ ${evt.content}`); setStatus('error'); }
      });
    } catch (err) {
      setPhase(`❌ Connection error: ${err.message}`);
      setStatus('error');
    } finally {
      setAwrRunning(false);
    }
  };

  const stopAwrAnalysis = () => {
    awrReaderRef.current?.cancel();
    setAwrRunning(false);
    setStatus('idle');
    setPhase('');
  };

  // ── Severity helpers ──────────────────────────────────────────────────────────
  const getWaitSeverity = (rows) => {
    const lockWait = rows?.find(r => r.event?.includes('lock') || r.event?.includes('enq: TX'));
    if (lockWait && lockWait.time_ms > 30000) return 'critical';
    const highIO = rows?.find(r => (r.event?.includes('sequential') || r.event?.includes('scattered')) && r.time_ms > 400000);
    if (highIO) return 'warning';
    return 'ok';
  };

  const getMemorySeverity = (data) => {
    if (!data) return 'ok';
    if (data.buffer_cache_hit_pct < 90) return 'critical';
    if (data.buffer_cache_hit_pct < 95 || data.shared_pool_hit_pct < 93) return 'warning';
    return 'ok';
  };

  const getLockSeverity = (data) => {
    if (!data) return 'ok';
    if ((data.blocking_sessions?.length || 0) > 3) return 'critical';
    if ((data.blocking_sessions?.length || 0) > 0) return 'warning';
    return 'ok';
  };

  const getTablespaceSeverity = (rows) => {
    if (!rows) return 'ok';
    if (rows.some(r => r.used_pct >= 95)) return 'critical';
    if (rows.some(r => r.used_pct >= 85)) return 'warning';
    return 'ok';
  };

  const getCMSeverity = (data) => {
    if (!data) return 'ok';
    if ((data.queue?.errored_1h || 0) > 10 || (data.queue?.pending || 0) > 40) return 'critical';
    if ((data.queue?.errored_1h || 0) > 3  || (data.queue?.pending || 0) > 20) return 'warning';
    return 'ok';
  };

  const getStatsSeverity = (data) => {
    if (!data) return 'ok';
    const invalid = data.invalid_objects?.rows?.length || 0;
    const stale   = data.stale_statistics?.rows?.length || 0;
    if (invalid > 5 || stale > 6) return 'critical';
    if (invalid > 0 || stale > 2) return 'warning';
    return 'ok';
  };

  const severityOf = (area, d) => {
    if (!d) return 'ok';
    if (area === 'wait_events')        return getWaitSeverity(d.rows);
    if (area === 'memory')             return getMemorySeverity(d);
    if (area === 'locks')              return getLockSeverity(d);
    if (area === 'tablespace')         return getTablespaceSeverity(d.rows);
    if (area === 'concurrent_manager') return getCMSeverity(d);
    if (area === 'statistics')         return getStatsSeverity(d);
    return 'ok';
  };

  // ── Metric card renderers ─────────────────────────────────────────────────────
  const renderWaitEvents = (data) => (
    <table className="perf-metric-table">
      <thead><tr><th>Event</th><th>Waits</th><th>Avg ms</th></tr></thead>
      <tbody>
        {data.rows?.slice(0, 5).map((r, i) => (
          <tr key={i}>
            <td title={r.event}>{r.event}</td>
            <td>{r.waits?.toLocaleString()}</td>
            <td className={r.avg_ms > 20 ? 'highlight-red' : r.avg_ms > 8 ? 'highlight-yellow' : ''}>{r.avg_ms}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  const renderTopSQL = (data) => (
    <table className="perf-metric-table">
      <thead><tr><th>SQL (truncated)</th><th>Elapsed s</th><th>Disk Rds</th></tr></thead>
      <tbody>
        {data.rows?.slice(0, 4).map((r, i) => (
          <tr key={i}>
            <td title={r.sql_text}>{r.sql_text?.slice(0, 40)}…</td>
            <td className={r.elapsed_sec > 5 ? 'highlight-red' : r.elapsed_sec > 2 ? 'highlight-yellow' : ''}>{r.elapsed_sec}</td>
            <td className={r.disk_reads > 100000 ? 'highlight-red' : r.disk_reads > 30000 ? 'highlight-yellow' : ''}>{r.disk_reads?.toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  const renderMemory = (data) => (
    <>
      <div className="perf-stat-row"><span>Buffer Cache Hit%</span>
        <span className={data.buffer_cache_hit_pct < 90 ? 'highlight-red' : data.buffer_cache_hit_pct < 95 ? 'highlight-yellow' : 'highlight-green'}>
          {data.buffer_cache_hit_pct}%
        </span>
      </div>
      <div className="perf-stat-row"><span>Shared Pool Hit%</span>
        <span className={data.shared_pool_hit_pct < 93 ? 'highlight-yellow' : 'highlight-green'}>{data.shared_pool_hit_pct}%</span>
      </div>
      <div className="perf-stat-row"><span>SGA Total</span>
        <span>{data.sga?.rows?.find(r => r.name === 'TOTAL')?.mb?.toLocaleString()} MB</span>
      </div>
      <div className="perf-stat-row"><span>PGA Target</span>
        <span>{data.pga?.rows?.find(r => r.name === 'PGA target parameter')?.mb?.toLocaleString()} MB</span>
      </div>
    </>
  );

  const renderLocks = (data) => (
    <>
      <div className="perf-stat-row"><span>Active Sessions</span><span>{data.total_active}</span></div>
      <div className="perf-stat-row"><span>Waiting Sessions</span>
        <span className={data.total_waiting > 30 ? 'highlight-red' : data.total_waiting > 10 ? 'highlight-yellow' : ''}>{data.total_waiting}</span>
      </div>
      <div className="perf-stat-row"><span>Blocking Sessions</span>
        <span className={(data.blocking_sessions?.length || 0) > 0 ? 'highlight-red' : 'highlight-green'}>{data.blocking_sessions?.length || 0}</span>
      </div>
      {data.blocking_sessions?.slice(0, 2).map((s, i) => (
        <div key={i} className="perf-stat-row" style={{ fontSize: '0.68rem', color: '#f87171' }}>
          <span>SID {s.blocking_sid} → blocks {s.blocked_sid}</span>
          <span>{s.wait_seconds}s</span>
        </div>
      ))}
    </>
  );

  const renderTablespace = (data) => (
    <table className="perf-metric-table">
      <thead><tr><th>Tablespace</th><th>Used GB</th><th>Used%</th></tr></thead>
      <tbody>
        {data.rows?.slice(0, 6).map((r, i) => (
          <tr key={i}>
            <td title={r.tablespace_name}>{r.tablespace_name}</td>
            <td>{r.used_gb}</td>
            <td className={r.used_pct >= 95 ? 'highlight-red' : r.used_pct >= 85 ? 'highlight-yellow' : ''}>{r.used_pct}%</td>
          </tr>
        ))}
      </tbody>
    </table>
  );

  const renderConcurrentManager = (data) => (
    <>
      <div className="perf-stat-row"><span>Pending Requests</span>
        <span className={data.queue?.pending > 40 ? 'highlight-red' : data.queue?.pending > 20 ? 'highlight-yellow' : ''}>{data.queue?.pending}</span>
      </div>
      <div className="perf-stat-row"><span>Running</span><span>{data.queue?.running}</span></div>
      <div className="perf-stat-row"><span>Completed (1h)</span>
        <span className="highlight-green">{data.queue?.completed_1h}</span>
      </div>
      <div className="perf-stat-row"><span>Errored (1h)</span>
        <span className={data.queue?.errored_1h > 10 ? 'highlight-red' : data.queue?.errored_1h > 3 ? 'highlight-yellow' : ''}>{data.queue?.errored_1h}</span>
      </div>
      {(data.long_running_requests?.length || 0) > 0 && (
        <div className="perf-stat-row" style={{ color: '#fbbf24', fontSize: '0.72rem' }}>
          <span>Long-running (&gt;30min)</span><span>{data.long_running_requests.length}</span>
        </div>
      )}
    </>
  );

  const renderStatistics = (data) => (
    <>
      <div className="perf-stat-row"><span>Stale Table Stats</span>
        <span className={(data.stale_statistics?.rows?.length || 0) > 4 ? 'highlight-red' : (data.stale_statistics?.rows?.length || 0) > 0 ? 'highlight-yellow' : 'highlight-green'}>
          {data.stale_statistics?.rows?.length || 0}
        </span>
      </div>
      <div className="perf-stat-row"><span>Invalid Objects</span>
        <span className={(data.invalid_objects?.rows?.length || 0) > 0 ? 'highlight-red' : 'highlight-green'}>{data.invalid_objects?.rows?.length || 0}</span>
      </div>
      {data.invalid_objects?.rows?.slice(0, 2).map((o, i) => (
        <div key={i} className="perf-stat-row" style={{ fontSize: '0.68rem', color: '#f87171' }}>
          <span title={o.object_name}>{o.object_name?.slice(0, 22)}</span>
          <span>{o.object_type}</span>
        </div>
      ))}
    </>
  );

  const renderCardBody = (area, data) => {
    if (!data) return null;
    if (area === 'wait_events')        return renderWaitEvents(data);
    if (area === 'top_sql')            return renderTopSQL(data);
    if (area === 'memory')             return renderMemory(data);
    if (area === 'locks')              return renderLocks(data);
    if (area === 'tablespace')         return renderTablespace(data);
    if (area === 'concurrent_manager') return renderConcurrentManager(data);
    if (area === 'statistics')         return renderStatistics(data);
    return null;
  };

  // ── AWR delta table ───────────────────────────────────────────────────────────
  const computeDelta = (bVal, cVal) => {
    if (bVal == null || cVal == null || bVal === 0) return null;
    return ((cVal - bVal) / Math.abs(bVal) * 100).toFixed(1);
  };

  const getDeltaClass = (pct, higherIsBetter) => {
    if (pct == null) return 'delta-neutral';
    const v = parseFloat(pct);
    if (Math.abs(v) < 1) return 'delta-neutral';
    if (higherIsBetter) return v > 0 ? 'delta-good' : 'delta-bad';
    return v > 0 ? 'delta-bad' : 'delta-good';
  };

  const renderDeltaRow = (label, bVal, cVal, fmt, higherIsBetter) => {
    const pct   = computeDelta(bVal, cVal);
    const cls   = getDeltaClass(pct, higherIsBetter);
    const sign  = pct != null && parseFloat(pct) > 0 ? '+' : '';
    const arrow = pct == null ? '' : parseFloat(pct) > 0 ? ' ▲' : ' ▼';
    return (
      <tr key={label}>
        <td className="awr-delta-label">{label}</td>
        <td className="awr-delta-val">{bVal != null ? fmt(bVal) : '—'}</td>
        <td className="awr-delta-val">{cVal != null ? fmt(cVal) : '—'}</td>
        <td className={`awr-delta-val ${cls}`}>{pct != null ? `${sign}${pct}%${arrow}` : '—'}</td>
      </tr>
    );
  };

  const renderDeltaTable = () => {
    if (!awrBaselineData || !awrCompareData) return null;
    const b = awrBaselineData;
    const c = awrCompareData;
    return (
      <div className="awr-delta-section">
        <div className="awr-period-info">
          <div className="awr-period-pill baseline">
            <span className="awr-period-pill-label">Baseline</span>
            <span>{b.period}</span>
            {b.is_peak && <span className="awr-peak-badge">PEAK</span>}
          </div>
          <div className="awr-period-pill compare">
            <span className="awr-period-pill-label">Compare</span>
            <span>{c.period}</span>
            {c.is_peak && <span className="awr-peak-badge">PEAK</span>}
          </div>
        </div>
        <table className="awr-delta-table">
          <thead>
            <tr><th>Metric</th><th>Baseline</th><th>Compare</th><th>Δ%</th></tr>
          </thead>
          <tbody>
            <tr className="awr-section-hdr"><td colSpan={4}>Load Profile</td></tr>
            {LOAD_PROFILE_META.map(m => renderDeltaRow(
              m.label, b.load_profile?.[m.key], c.load_profile?.[m.key], m.fmt, m.higherIsBetter
            ))}
            <tr className="awr-section-hdr"><td colSpan={4}>Instance Efficiency</td></tr>
            {EFFICIENCY_META.map(m => renderDeltaRow(
              m.label, b.instance_efficiency?.[m.key], c.instance_efficiency?.[m.key], m.fmt, m.higherIsBetter
            ))}
          </tbody>
        </table>
        <div className="awr-events-side-by-side">
          <div className="awr-events-panel">
            <div className="awr-events-panel-head">Top Events — Baseline</div>
            {b.top_events?.slice(0, 5).map((e, i) => (
              <div key={i} className="awr-event-row">
                <span className="awr-event-name" title={e.event}>{e.event?.slice(0, 24)}</span>
                <span className="awr-event-pct">{e.pct_db_time}%</span>
              </div>
            ))}
          </div>
          <div className="awr-events-panel">
            <div className="awr-events-panel-head">Top Events — Compare</div>
            {c.top_events?.slice(0, 5).map((e, i) => (
              <div key={i} className="awr-event-row">
                <span className="awr-event-name" title={e.event}>{e.event?.slice(0, 24)}</span>
                <span className="awr-event-pct">{e.pct_db_time}%</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    );
  };

  // ── Snapshot selector component ───────────────────────────────────────────────
  const groupedSnaps = snapshots.reduce((acc, s) => {
    if (!acc[s.day_label]) acc[s.day_label] = [];
    acc[s.day_label].push(s);
    return acc;
  }, {});

  const SnapSelect = ({ value, onChange, label }) => (
    <div className="awr-snap-group">
      <div className="awr-snap-group-label">{label}</div>
      <select
        className="awr-snap-select"
        value={value}
        onChange={e => onChange(e.target.value)}
        disabled={awrRunning || snapsLoading}
      >
        <option value="">— select —</option>
        {Object.entries(groupedSnaps).map(([day, snaps]) => (
          <optgroup key={day} label={day}>
            {snaps.map(s => (
              <option key={s.snap_id} value={String(s.snap_id)}>
                {s.snap_id}  {s.label}{s.is_peak ? ' ★' : ''}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  );

  // ── Upload drop zone helpers ──────────────────────────────────────────────────
  const makeDropProps = (slot) => {
    const setDrag = slot === 1 ? setDragOver1 : setDragOver2;
    const setFile = slot === 1 ? setUploadFile1 : setUploadFile2;
    return {
      onDragEnter: (e) => { e.preventDefault(); setDrag(true); },
      onDragLeave: (e) => { e.preventDefault(); setDrag(false); },
      onDragOver:  (e) => { e.preventDefault(); },
      onDrop: (e) => {
        e.preventDefault();
        setDrag(false);
        const f = e.dataTransfer.files?.[0];
        if (f) setFile(f);
      },
    };
  };

  const LABEL_MAP = { idle: 'Idle', running: 'Analyzing...', complete: 'Complete', error: 'Error' };
  const anyRunning = running || awrRunning;

  // ── Render ────────────────────────────────────────────────────────────────────
  return (
    <div className="perf-modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="perf-modal glass">

        {/* Header */}
        <div className="perf-header">
          <div className="perf-header-left">
            <Activity size={20} style={{ color: '#60A5FA' }} />
            <h2>Oracle EBS Performance Analyzer</h2>
          </div>
          <button className="perf-close-btn" onClick={onClose}>&times;</button>
        </div>

        {/* Mode tabs */}
        <div className="perf-mode-tabs">
          <button
            className={`perf-mode-tab ${activeMode === 'live' ? 'active' : ''}`}
            onClick={() => { if (!anyRunning) setActiveMode('live'); }}
            disabled={anyRunning}
          >
            📊 Live Diagnostics
          </button>
          <button
            className={`perf-mode-tab ${activeMode === 'awr' ? 'active' : ''}`}
            onClick={() => { if (!anyRunning) setActiveMode('awr'); }}
            disabled={anyRunning}
          >
            📋 AWR Analysis
          </button>
        </div>

        {/* Body */}
        <div className="perf-body">

          {/* ── Left Panel ─────────────────────────────────────────────────── */}
          {activeMode === 'live' ? (
            <div className="perf-left">
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
                    <div style={{ fontSize: '0.78rem', color: '#f87171', padding: '0.4rem 0' }}>
                      No environments configured. Add one in Settings → Environments.
                    </div>
                  )}
                </div>

                <div>
                  <div className="perf-config-label" style={{ marginBottom: '0.45rem' }}>Analysis Areas</div>
                  <div className="perf-areas-grid">
                    {ALL_AREAS.map(area => (
                      <label key={area} className="perf-area-check">
                        <input type="checkbox" checked={selectedAreas.has(area)}
                          onChange={() => toggleArea(area)} disabled={running} />
                        {AREA_META[area].icon} {AREA_META[area].label}
                      </label>
                    ))}
                  </div>
                </div>

                {running ? (
                  <button className="btn-outline perf-run-btn" onClick={stopAnalysis}>
                    <X size={15} /> Stop Analysis
                  </button>
                ) : (
                  <button className="btn-primary perf-run-btn" onClick={runAnalysis}
                    disabled={selectedAreas.size === 0 || environments.length === 0}>
                    <Activity size={15} /> Run Analysis
                  </button>
                )}
              </div>

              {progress.length > 0 && (
                <div className="perf-progress-list">
                  {progress.map(p => (
                    <div key={p.area} className={`perf-progress-item ${p.done ? 'done' : 'active'}`}>
                      {p.done ? '✓' : <Loader2 size={11} style={{ animation: 'spin 1s linear infinite' }} />}
                      {AREA_META[p.area]?.icon} {p.msg}
                    </div>
                  ))}
                </div>
              )}

              {diagnostics && (
                <div className="perf-metrics">
                  {[...selectedAreas].map(area => {
                    const data     = diagnostics[area];
                    const severity = severityOf(area, data);
                    const expanded = expandedCards.has(area);
                    if (!data) return null;
                    return (
                      <div key={area} className="perf-metric-card">
                        <div className="perf-metric-card-header" onClick={() => toggleCard(area)}>
                          <div className="perf-metric-card-title">
                            {AREA_META[area].icon} {AREA_META[area].label}
                          </div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}>
                            <span className={`perf-metric-severity severity-${severity}`}>
                              {severity === 'critical' ? '❌ Critical' : severity === 'warning' ? '⚠️ Warning' : '✅ OK'}
                            </span>
                            {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                          </div>
                        </div>
                        {expanded && (
                          <div className="perf-metric-card-body">{renderCardBody(area, data)}</div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          ) : (
            /* ── AWR Left Panel ───────────────────────────────────────────── */
            <div className="perf-left">
              <div className="perf-config" style={{ paddingBottom: '0.5rem' }}>
                <div className="perf-config-label">Target Environment</div>
                {environments.length > 0 ? (
                  <select className="perf-env-select" value={selectedEnv}
                    onChange={e => setSelectedEnv(e.target.value)} disabled={awrRunning}>
                    {environments.map(env => (
                      <option key={env.name} value={env.name}>{env.name} — {env.db_host || 'No host'}</option>
                    ))}
                  </select>
                ) : (
                  <div style={{ fontSize: '0.78rem', color: '#f87171', padding: '0.4rem 0' }}>
                    No environments configured.
                  </div>
                )}
              </div>

              <div className="awr-subtabs">
                <button className={`awr-subtab ${awrSubTab === 'snapshots' ? 'active' : ''}`}
                  onClick={() => { setAwrSubTab('snapshots'); setAnalysisText(''); setPhase(''); setStatus('idle'); }}>
                  Snapshot Comparison
                </button>
                <button className={`awr-subtab ${awrSubTab === 'upload' ? 'active' : ''}`}
                  onClick={() => { setAwrSubTab('upload'); setAnalysisText(''); setPhase(''); setStatus('idle'); }}>
                  Upload Report
                </button>
              </div>

              {awrSubTab === 'snapshots' ? (
                <div className="perf-config">
                  {snapsLoading ? (
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', color: '#94a3b8', fontSize: '0.8rem' }}>
                      <Loader2 size={13} style={{ animation: 'spin 1s linear infinite' }} /> Loading snapshots...
                    </div>
                  ) : snapshots.length === 0 ? (
                    <div style={{ fontSize: '0.78rem', color: '#94a3b8' }}>No snapshots available.</div>
                  ) : (
                    <>
                      <div className="perf-config-label">Baseline Period</div>
                      <div className="awr-period-row">
                        <SnapSelect value={baselineBegin} onChange={setBaselineBegin} label="Begin Snap" />
                        <SnapSelect value={baselineEnd}   onChange={setBaselineEnd}   label="End Snap" />
                      </div>
                      <div className="perf-config-label" style={{ marginTop: '0.4rem' }}>Comparison Period</div>
                      <div className="awr-period-row">
                        <SnapSelect value={compareBegin} onChange={setCompareBegin} label="Begin Snap" />
                        <SnapSelect value={compareEnd}   onChange={setCompareEnd}   label="End Snap" />
                      </div>
                      {awrRunning ? (
                        <button className="btn-outline perf-run-btn" onClick={stopAwrAnalysis}>
                          <X size={15} /> Stop
                        </button>
                      ) : (
                        <button className="btn-primary perf-run-btn" onClick={runAwrCompare}
                          disabled={!baselineBegin || !baselineEnd || !compareBegin || !compareEnd || environments.length === 0}>
                          📊 Compare Periods
                        </button>
                      )}
                    </>
                  )}
                  {renderDeltaTable()}
                </div>
              ) : (
                <div className="perf-config">
                  <div className="perf-config-label">AWR Report Files</div>
                  <div className="awr-upload-zones">
                    <div
                      className={`awr-upload-zone ${uploadFile1 ? 'has-file' : ''} ${dragOver1 ? 'drag-over' : ''}`}
                      {...makeDropProps(1)}
                      onClick={() => document.getElementById('awr-file1-input').click()}
                    >
                      <Upload size={16} style={{ color: uploadFile1 ? '#34d399' : '#94a3b8' }} />
                      <span className="awr-upload-zone-label">
                        {uploadFile1 ? uploadFile1.name : 'Baseline AWR Report'}
                      </span>
                      <span className="awr-upload-zone-sub">
                        {uploadFile1 ? `${(uploadFile1.size / 1024).toFixed(0)} KB` : '.txt or .html — required'}
                      </span>
                      {uploadFile1 && (
                        <button className="awr-clear-file" onClick={(e) => { e.stopPropagation(); setUploadFile1(null); }}>
                          <X size={10} />
                        </button>
                      )}
                      {parsedInfo.baseline && (
                        <span className={`awr-parse-badge ${parsedInfo.baseline.parse_quality || 'limited'}`}>
                          {parsedInfo.baseline.parse_quality || 'limited'}
                        </span>
                      )}
                      <input id="awr-file1-input" type="file" accept=".txt,.html,.htm"
                        style={{ display: 'none' }}
                        onChange={e => { const f = e.target.files?.[0]; if (f) setUploadFile1(f); }} />
                    </div>

                    <div
                      className={`awr-upload-zone ${uploadFile2 ? 'has-file' : ''} ${dragOver2 ? 'drag-over' : ''}`}
                      {...makeDropProps(2)}
                      onClick={() => document.getElementById('awr-file2-input').click()}
                    >
                      <Upload size={16} style={{ color: uploadFile2 ? '#34d399' : '#94a3b8' }} />
                      <span className="awr-upload-zone-label">
                        {uploadFile2 ? uploadFile2.name : 'Comparison AWR Report'}
                      </span>
                      <span className="awr-upload-zone-sub">
                        {uploadFile2 ? `${(uploadFile2.size / 1024).toFixed(0)} KB` : '.txt or .html — optional'}
                      </span>
                      {uploadFile2 && (
                        <button className="awr-clear-file" onClick={(e) => { e.stopPropagation(); setUploadFile2(null); }}>
                          <X size={10} />
                        </button>
                      )}
                      {parsedInfo.compare && (
                        <span className={`awr-parse-badge ${parsedInfo.compare.parse_quality || 'limited'}`}>
                          {parsedInfo.compare.parse_quality || 'limited'}
                        </span>
                      )}
                      <input id="awr-file2-input" type="file" accept=".txt,.html,.htm"
                        style={{ display: 'none' }}
                        onChange={e => { const f = e.target.files?.[0]; if (f) setUploadFile2(f); }} />
                    </div>
                  </div>

                  {awrRunning ? (
                    <button className="btn-outline perf-run-btn" onClick={stopAwrAnalysis}>
                      <X size={15} /> Stop
                    </button>
                  ) : (
                    <button className="btn-primary perf-run-btn" onClick={runAwrUpload}
                      disabled={!uploadFile1 || environments.length === 0}>
                      📋 Analyze Report{uploadFile2 ? 's' : ''}
                    </button>
                  )}

                  {parsedInfo.baseline && (
                    <div className="awr-parsed-summary">
                      <div className="awr-parsed-title">Parsed Baseline</div>
                      {parsedInfo.baseline.db_name && <div className="awr-parsed-row"><span>DB Name</span><span>{parsedInfo.baseline.db_name}</span></div>}
                      {parsedInfo.baseline.begin_snap && <div className="awr-parsed-row"><span>Snap Range</span><span>{parsedInfo.baseline.begin_snap}–{parsedInfo.baseline.end_snap}</span></div>}
                      {parsedInfo.baseline.elapsed_mins && <div className="awr-parsed-row"><span>Elapsed</span><span>{parsedInfo.baseline.elapsed_mins} min</span></div>}
                    </div>
                  )}
                  {parsedInfo.compare && (
                    <div className="awr-parsed-summary">
                      <div className="awr-parsed-title">Parsed Comparison</div>
                      {parsedInfo.compare.db_name && <div className="awr-parsed-row"><span>DB Name</span><span>{parsedInfo.compare.db_name}</span></div>}
                      {parsedInfo.compare.begin_snap && <div className="awr-parsed-row"><span>Snap Range</span><span>{parsedInfo.compare.begin_snap}–{parsedInfo.compare.end_snap}</span></div>}
                      {parsedInfo.compare.elapsed_mins && <div className="awr-parsed-row"><span>Elapsed</span><span>{parsedInfo.compare.elapsed_mins} min</span></div>}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* ── Right Panel ────────────────────────────────────────────────── */}
          <div className="perf-right">
            <div className="perf-analysis-header">
              <h3>🤖 AI Performance Report</h3>
              <span className={`perf-status-badge ${status}`}>{LABEL_MAP[status]}</span>
            </div>

            {phase && (
              <div className="perf-phase-bar">
                {anyRunning && <Loader2 size={13} style={{ animation: 'spin 1s linear infinite', flexShrink: 0 }} />}
                {phase}
              </div>
            )}

            <div className="perf-analysis-body" ref={analysisRef}>
              {!analysisText && !anyRunning && (
                <div className="perf-empty-state">
                  <Activity size={48} />
                  <p>
                    {activeMode === 'live'
                      ? <>Select an environment and analysis areas, then click <strong>Run Analysis</strong> to diagnose your Oracle EBS database performance.</>
                      : awrSubTab === 'snapshots'
                        ? <>Select baseline and comparison snap ranges, then click <strong>Compare Periods</strong> to run an AWR period-over-period analysis.</>
                        : <>Upload one or two AWR reports (.txt or .html), then click <strong>Analyze Report</strong> to get AI-powered insights.</>
                    }
                  </p>
                </div>
              )}
              {analysisText && (
                <div className="perf-analysis-md">
                  <ReactMarkdown>{analysisText}</ReactMarkdown>
                  {anyRunning && <span className="perf-cursor" />}
                </div>
              )}
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}
