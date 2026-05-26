import React, { useState, useEffect, useRef } from 'react';
import api from '../../api';
import './DeploymentCenter.css';
import { Rocket, Terminal, History, PackageCheck, CheckCircle2, XCircle, Loader2, Download, Ban, RotateCcw, Trash2, Bot } from 'lucide-react';
import DeploymentAgent from './DeploymentAgent';

const TERMINAL_STATUSES = new Set(['pending', 'extracting', 'downloading', 'deploying']);

export default function DeploymentCenter({ onClose, preselectedRunId }) {
  const [deployments, setDeployments] = useState([]);
  const [activeTab, setActiveTab] = useState('agent');
  const [selectedRun, setSelectedRun] = useState(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [selectedStep, setSelectedStep] = useState(null);
  const [actionLoading, setActionLoading] = useState(null); // 'cancel' | 'retry' | 'delete-<id>'
  const [migrateModal, setMigrateModal] = useState(null); // { run, targetEnv } | null
  const [migrateServerIdx, setMigrateServerIdx] = useState(0);
  const [migrateSubmitting, setMigrateSubmitting] = useState(false);

  // Read configured environments from localStorage (set via Settings modal)
  const [environments] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem('ebs_environments') || '[]');
    } catch {
      return [];
    }
  });

  // Read configured SSH server connections from localStorage
  const [serverConnections] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem('ebs_server_connections') || '[]');
    } catch {
      return [];
    }
  });

  // Form fields
  const [sourceDocType, setSourceDocType] = useState('confluence');
  const [sourceDocName, setSourceDocName] = useState('EBS AP Custom Supplier Patch');
  const [targetInstance, setTargetInstance] = useState(environments[0]?.name || 'DEV');
  const [selectedServerIndex, setSelectedServerIndex] = useState(0);
  const [useGit, setUseGit] = useState(false);
  const [gitRepoUrl, setGitRepoUrl] = useState('');
  const [gitBranch, setGitBranch] = useState('main');
  const [sourceContent, setSourceContent] = useState(
    `EBS Custom Patch deployment notes:\n` +
    `1. Compile custom supplier definition table: xxap_supplier_tbl.sql\n` +
    `   Command: sqlplus apps/apps @xxap_supplier_tbl.sql\n` +
    `2. Update the custom supplier logic: xxap_supplier_pkg.pls\n` +
    `   Command: alter package xxap_supplier_pkg compile body;\n` +
    `3. Restart custom notification service: xxap_notification_reload.sh\n` +
    `   Command: sh xxap_notification_reload.sh`
  );

  const pollIntervalRef = useRef(null);

  useEffect(() => {
    fetchDeployments();
    return () => stopPolling();
  }, []);

  useEffect(() => {
    if (preselectedRunId) {
      selectRunById(preselectedRunId, 'terminal');
    }
  }, [preselectedRunId]);

  const fetchDeployments = async () => {
    setLoading(true);
    try {
      const res = await api.get('/deployments/');
      setDeployments(res.data);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  /** Fetch full detail for a run, set it active, and start/stop polling as needed. */
  const selectRunById = async (runId, tab = 'terminal') => {
    try {
      const res = await api.get(`/deployments/${runId}`);
      const run = res.data;
      setSelectedRun(run);
      if (run.steps?.length > 0) {
        const activeOrFirst = run.steps.find(s => s.status === 'running') || run.steps[0];
        setSelectedStep(activeOrFirst);
      } else {
        setSelectedStep(null);
      }
      setActiveTab(tab);
      if (TERMINAL_STATUSES.has(run.status)) {
        startPolling(run.id);
      } else {
        stopPolling();
      }
    } catch (err) {
      console.error(err);
    }
  };

  const handleCreateDeployment = async (e) => {
    e.preventDefault();
    if (!sourceContent.trim()) return;

    // Resolve SSH credentials from the selected server connection profile
    const srvProfile = serverConnections[selectedServerIndex] || null;
    // Resolve DB credentials from the selected environment profile
    const envProfile = environments.find(e => e.name === targetInstance) || {};

    setSubmitting(true);
    try {
      const res = await api.post('/deployments/', {
        source_doc_type: sourceDocType,
        source_doc_name: sourceDocName,
        source_content: sourceContent,
        target_instance: targetInstance,
        git_repo_url: useGit ? gitRepoUrl : null,
        git_branch: useGit ? gitBranch : null,
        ssh_host: srvProfile?.hostname || null,
        ssh_port: srvProfile?.port || 22,
        ssh_username: srvProfile?.username || null,
        ssh_password: srvProfile?.password || null,
        db_host: envProfile.db_host     || null,
        db_port: envProfile.db_port     || 1521,
        db_sid:  envProfile.db_sid      || null,
        db_user: envProfile.db_user     || null,
        db_password: envProfile.db_password || null,
      });
      const run = res.data;
      setSelectedRun(run);
      setSelectedStep(null);
      setActiveTab('terminal');
      startPolling(run.id);
      fetchDeployments();
    } catch (err) {
      console.error(err);
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = async (runId) => {
    setActionLoading('cancel');
    try {
      const res = await api.patch(`/deployments/${runId}/cancel`);
      setSelectedRun(res.data);
      stopPolling();
      fetchDeployments();
    } catch (err) {
      console.error(err);
    } finally {
      setActionLoading(null);
    }
  };

  const handleRetry = async (runId) => {
    setActionLoading('retry');
    try {
      const res = await api.post(`/deployments/${runId}/retry`);
      const run = res.data;
      setSelectedRun(run);
      setSelectedStep(null);
      startPolling(run.id);
      fetchDeployments();
    } catch (err) {
      console.error(err);
    } finally {
      setActionLoading(null);
    }
  };

  const handleDeleteRun = async (runId) => {
    if (!window.confirm('Delete this deployment run and all its logs?')) return;
    setActionLoading(`delete-${runId}`);
    try {
      await api.delete(`/deployments/${runId}`);
      if (selectedRun?.id === runId) {
        setSelectedRun(null);
        setSelectedStep(null);
        setActiveTab('history');
        stopPolling();
      }
      fetchDeployments();
    } catch (err) {
      alert(err.response?.data?.detail || 'Delete failed.');
      console.error(err);
    } finally {
      setActionLoading(null);
    }
  };

  /** Open the migration confirmation modal. */
  const handleMigrate = (run, targetEnvName) => {
    // Default SSH server: try to match by hostname against source run, else index 0
    const defaultSrvIdx = serverConnections.findIndex(
      s => s.hostname === run.ssh_host
    );
    setMigrateServerIdx(defaultSrvIdx >= 0 ? defaultSrvIdx : 0);
    setMigrateModal({ run, targetEnvName });
  };

  /** Called when user clicks Confirm in the migration modal. */
  const confirmMigrate = async () => {
    if (!migrateModal) return;
    const { run, targetEnvName } = migrateModal;
    const envProfile  = environments.find(e => e.name === targetEnvName) || {};
    const srvProfile  = serverConnections[migrateServerIdx] || null;

    setMigrateSubmitting(true);
    try {
      const res = await api.post(`/deployments/${run.id}/migrate`, {
        target_instance: targetEnvName,
        // Target environment DB credentials
        db_host:     envProfile.db_host     || null,
        db_port:     envProfile.db_port     || 1521,
        db_sid:      envProfile.db_sid      || null,
        db_user:     envProfile.db_user     || null,
        db_password: envProfile.db_password || null,
        // SSH — use selected server (or null to inherit source server on backend)
        ssh_host:     srvProfile?.hostname || null,
        ssh_port:     srvProfile?.port     || 22,
        ssh_username: srvProfile?.username || null,
        ssh_password: srvProfile?.password || null,
      });
      setMigrateModal(null);
      const newRun = res.data;
      setSelectedRun(newRun);
      setSelectedStep(null);
      setActiveTab('terminal');
      startPolling(newRun.id);
      fetchDeployments();
    } catch (err) {
      alert(err.response?.data?.detail || 'Migration failed.');
      console.error(err);
    } finally {
      setMigrateSubmitting(false);
    }
  };

  const startPolling = (runId) => {
    stopPolling();
    pollIntervalRef.current = setInterval(async () => {
      try {
        const res = await api.get(`/deployments/${runId}`);
        const run = res.data;
        setSelectedRun(run);

        if (run.steps?.length > 0) {
          setSelectedStep(prev =>
            prev ? (run.steps.find(s => s.id === prev.id) || run.steps.find(s => s.status === 'running') || run.steps[0])
                 : (run.steps.find(s => s.status === 'running') || run.steps[0])
          );
        }

        if (['completed', 'failed', 'cancelled'].includes(run.status)) {
          stopPolling();
          fetchDeployments();
        }
      } catch (err) {
        console.error(err);
      }
    }, 1500);
  };

  const stopPolling = () => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  };

  const getStatusBadge = (status) => {
    switch (status) {
      case 'completed':  return <span className="deploy-badge completed"><CheckCircle2 size={11} /> Success</span>;
      case 'failed':     return <span className="deploy-badge failed"><XCircle size={11} /> Failed</span>;
      case 'deploying':  return <span className="deploy-badge running"><Loader2 size={11} className="spin-icon" /> Deploying...</span>;
      case 'extracting': return <span className="deploy-badge extracting"><Loader2 size={11} className="spin-icon" /> Parsing...</span>;
      case 'downloading':return <span className="deploy-badge downloading"><Download size={11} /> Git Sync...</span>;
      case 'cancelled':  return <span className="deploy-badge cancelled"><Ban size={11} /> Cancelled</span>;
      default:           return <span className="deploy-badge pending"><Loader2 size={11} /> Pending</span>;
    }
  };

  const getStepStatusIcon = (status) => {
    switch (status) {
      case 'success': return <CheckCircle2 size={14} color="#10B981" />;
      case 'failed':  return <XCircle size={14} color="#EF4444" />;
      case 'running': return <Loader2 size={14} color="#60A5FA" className="spin-icon" />;
      default:        return <Loader2 size={14} color="#6B7280" />;
    }
  };

  const getProgressPercent = () => {
    if (!selectedRun?.steps?.length) return 0;
    if (selectedRun.status === 'completed') return 100;
    const done = selectedRun.steps.filter(s => s.status === 'success').length;
    return Math.round((done / selectedRun.steps.length) * 100);
  };

  const isInProgress = selectedRun && TERMINAL_STATUSES.has(selectedRun.status);
  const isRetryable = selectedRun && ['failed', 'cancelled'].includes(selectedRun.status);

  // Returns environments a run can be migrated to (excludes its own target instance).
  // Falls back to standard EBS names when none are configured in Settings.
  const FALLBACK_ENVS = ['DEV', 'UAT', 'UAT2', 'PROD'];
  const getMigrateOptions = (runTargetInstance) => {
    const configured = environments.filter(e => e.name !== runTargetInstance);
    if (configured.length > 0) return configured;
    return FALLBACK_ENVS
      .filter(name => name !== runTargetInstance)
      .map(name => ({ name }));
  };

  return (
    <div className="rag-upload-modal-overlay">
      <div className="rag-upload-modal-content glass deploy-modal-content">
        <div className="modal-header">
          <div className="modal-title-group">
            <Rocket size={20} className="modal-title-svg" />
            <h2>EBS Code Deployment AI Agent</h2>
          </div>
          <button className="close-modal-btn" onClick={() => { stopPolling(); onClose(); }}>×</button>
        </div>

        <div className="rag-tabs-selector">
          <button
            className={`tab-btn ${activeTab === 'agent' ? 'active' : ''}`}
            onClick={() => { stopPolling(); setActiveTab('agent'); }}
          >
            <Bot size={14} style={{ marginRight: '0.35rem', verticalAlign: 'middle' }} />Agent Deploy
          </button>
          <button
            className={`tab-btn ${activeTab === 'create' ? 'active' : ''}`}
            onClick={() => { stopPolling(); setActiveTab('create'); }}
          >
            <PackageCheck size={14} style={{ marginRight: '0.35rem', verticalAlign: 'middle' }} />Manual Deploy
          </button>
          <button
            className={`tab-btn ${activeTab === 'terminal' ? 'active' : ''}`}
            disabled={!selectedRun}
            onClick={() => setActiveTab('terminal')}
          >
            <Terminal size={14} style={{ marginRight: '0.35rem', verticalAlign: 'middle' }} />Terminal {selectedRun && `#${selectedRun.id}`}
          </button>
          <button
            className={`tab-btn ${activeTab === 'history' ? 'active' : ''}`}
            onClick={() => { stopPolling(); setActiveTab('history'); fetchDeployments(); }}
          >
            <History size={14} style={{ marginRight: '0.35rem', verticalAlign: 'middle' }} />History
          </button>
        </div>

        <div className="modal-body-scroll">

          {/* ─── Tab: Agent Deploy (ReAct) ────────────────────────── */}
          {activeTab === 'agent' && (
            <DeploymentAgent
              serverConnections={serverConnections}
              environments={environments}
              onDeploymentTriggered={(runId) => {
                selectRunById(runId, 'terminal');
                fetchDeployments();
              }}
            />
          )}

          {/* ─── Tab 1: Manual Deploy ──────────────────────────────── */}
          {activeTab === 'create' && (
            <form onSubmit={handleCreateDeployment} className="deploy-form-container">
              <div className="hub-section glass mcp-panel-section">
                <h3 className="section-title"><PackageCheck size={16} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Code Deployment Parameter Launchpad</h3>
                <p className="mcp-desc">Choose the target environment, paste the deployment plan, and launch the agent.</p>

                <div className="mcp-grid-inputs">
                  <div className="input-group">
                    <label>Source Document Type</label>
                    <select value={sourceDocType} onChange={e => setSourceDocType(e.target.value)}>
                      <option value="confluence">📁 Confluence Page Notes</option>
                      <option value="word">📄 Microsoft Word Document</option>
                      <option value="pdf">📕 Adobe PDF Document</option>
                      <option value="chat">💬 User Chat Transcript</option>
                    </select>
                  </div>

                  <div className="input-group">
                    <label>Document Name</label>
                    <input
                      type="text"
                      value={sourceDocName}
                      onChange={e => setSourceDocName(e.target.value)}
                      placeholder="e.g. patch_v1.0"
                    />
                  </div>

                  <div className="input-group">
                    <label>Target EBS Environment</label>
                    <select value={targetInstance} onChange={e => setTargetInstance(e.target.value)}>
                      {environments.length > 0
                        ? environments.map(env => (
                            <option key={env.name} value={env.name}>
                              {env.name} — {env.db_host || env.name}
                            </option>
                          ))
                        : (
                          <>
                            <option value="DEV">DEV</option>
                            <option value="UAT">UAT</option>
                            <option value="UAT2">UAT2</option>
                            <option value="PROD">PROD</option>
                          </>
                        )
                      }
                    </select>
                    {environments.length === 0 && (
                      <span className="env-hint">⚠️ No environments configured. Add them in Settings → Environments & Database.</span>
                    )}
                  </div>

                  <div className="input-group">
                    <label>SSH Server Connection</label>
                    <select
                      value={selectedServerIndex}
                      onChange={e => setSelectedServerIndex(Number(e.target.value))}
                      disabled={serverConnections.length === 0}
                    >
                      {serverConnections.length > 0
                        ? serverConnections.map((srv, i) => (
                            <option key={srv.name} value={i}>
                              {srv.name} ({srv.username}@{srv.hostname})
                            </option>
                          ))
                        : <option value={0}>— No SSH servers configured —</option>
                      }
                    </select>
                    {serverConnections.length === 0 && (
                      <span className="env-hint">⚠️ No SSH servers configured. Add them in Settings → SSH Server Connections.</span>
                    )}
                  </div>

                  <div className="input-group full-width" style={{ display: 'flex', alignItems: 'center', gap: '0.6rem', margin: '0.2rem 0', gridColumn: 'span 2' }}>
                    <input
                      type="checkbox"
                      id="use-git-checkbox"
                      checked={useGit}
                      onChange={e => setUseGit(e.target.checked)}
                      style={{ width: 'auto', height: 'auto', margin: 0, cursor: 'pointer' }}
                    />
                    <label htmlFor="use-git-checkbox" style={{ margin: 0, cursor: 'pointer', textTransform: 'none', fontWeight: '500', display: 'inline', color: 'var(--text-primary)', fontSize: '0.85rem' }}>
                      📥 Retrieve patch source files from Git repository
                    </label>
                  </div>

                  {useGit && (
                    <>
                      <div className="input-group">
                        <label>Git Source Repository URL</label>
                        <input
                          type="text"
                          value={gitRepoUrl}
                          onChange={e => setGitRepoUrl(e.target.value)}
                          placeholder="https://github.com/org/repo"
                        />
                      </div>
                      <div className="input-group">
                        <label>Git Branch / Tag</label>
                        <input
                          type="text"
                          value={gitBranch}
                          onChange={e => setGitBranch(e.target.value)}
                        />
                      </div>
                    </>
                  )}

                  <div className="input-group full-width">
                    <label>Paste Deployment Plan Instructions & Script Commands</label>
                    <textarea
                      rows="8"
                      value={sourceContent}
                      onChange={e => setSourceContent(e.target.value)}
                      placeholder="Paste instructions, SQL files, packages, and compilation shell scripts here..."
                      required
                    />
                  </div>
                </div>

                <div className="mcp-actions-row">
                  <button type="submit" className="btn-primary mcp-save-btn" disabled={submitting}>
                    {submitting
                      ? <><Loader2 size={14} className="spin-icon" style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Activating Agent...</>
                      : <><Rocket size={14} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Launch Deployment AI Agent</>
                    }
                  </button>
                </div>
              </div>
            </form>
          )}

          {/* ─── Tab 2: Terminal Console ────────────────────────────── */}
          {activeTab === 'terminal' && selectedRun && (
            <div className="terminal-panel-container">
              <div className="hub-section glass terminal-overall-summary">
                <div className="terminal-summary-header">
                  <div className="summary-title-group">
                    <h4>Deployment Run #{selectedRun.id}</h4>
                    <span className="summary-target-badge">{selectedRun.target_instance}</span>
                    {getStatusBadge(selectedRun.status)}
                  </div>
                  <div className="terminal-run-actions">
                    <span className="progress-percentage-text">{getProgressPercent()}% Complete</span>
                    {isInProgress && (
                      <button
                        className="btn-outline run-action-btn cancel-btn"
                        onClick={() => handleCancel(selectedRun.id)}
                        disabled={actionLoading === 'cancel'}
                      >
                        {actionLoading === 'cancel' ? <Loader2 size={13} className="spin-icon" /> : <><Ban size={13} style={{ marginRight: '0.3rem', verticalAlign: 'middle' }} />Cancel</>}
                      </button>
                    )}
                    {isRetryable && (
                      <button
                        className="btn-primary run-action-btn retry-btn"
                        onClick={() => handleRetry(selectedRun.id)}
                        disabled={actionLoading === 'retry'}
                      >
                        {actionLoading === 'retry' ? <Loader2 size={13} className="spin-icon" /> : <><RotateCcw size={13} style={{ marginRight: '0.3rem', verticalAlign: 'middle' }} />Retry</>}
                      </button>
                    )}
                    {selectedRun.status === 'completed' && (
                      <div className="migrate-group">
                        <span className="migrate-label">
                          <Rocket size={12} style={{ marginRight: '0.25rem', verticalAlign: 'middle' }} />
                          Migrate to:
                        </span>
                        {getMigrateOptions(selectedRun.target_instance).map(env => (
                          <button
                            key={env.name}
                            className="btn-primary migrate-btn-tag"
                            onClick={() => handleMigrate(selectedRun, env.name)}
                          >
                            {env.name}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
                <div className="deployment-progress-track">
                  <div
                    className={`deployment-progress-fill ${selectedRun.status === 'failed' ? 'failed' : selectedRun.status === 'cancelled' ? 'cancelled' : ''}`}
                    style={{ width: `${getProgressPercent()}%` }}
                  />
                </div>
              </div>

              <div className="terminal-split-screen">
                <div className="terminal-steps-list glass">
                  <div className="terminal-steps-header">Deployment Run Steps</div>
                  <div className="steps-scroll-wrapper">
                    {(!selectedRun.steps || selectedRun.steps.length === 0) && (
                      <div className="loading-steps-indicator">
                        <Loader2 size={24} className="spin-icon" style={{ color: 'var(--accent-primary)' }} />
                        <p>Agent is extracting deployment steps from document...</p>
                      </div>
                    )}
                    {selectedRun.steps?.map(step => (
                      <div
                        key={step.id}
                        className={`step-log-item ${selectedStep?.id === step.id ? 'active' : ''} ${step.status}`}
                        onClick={() => setSelectedStep(step)}
                      >
                        <span className="step-icon-status">{getStepStatusIcon(step.status)}</span>
                        <div className="step-item-meta">
                          <span className="step-number-tag">Step {step.step_number}</span>
                          <span className="step-file-name">{step.file_name}</span>
                          <span className="step-type-badge">{step.execution_type.toUpperCase().replace(/_/g, ' ')}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="terminal-console-viewer glass">
                  <div className="console-header">
                    <span style={{ display: 'flex', alignItems: 'center', gap: '0.4rem' }}><Terminal size={13} />Live Command Execution Console</span>
                    {selectedStep && <span className="console-file-tag">{selectedStep.file_name}</span>}
                  </div>
                  <pre className="console-log-output">
                    {selectedStep?.log_output || '[WAITING] Log pipeline initialisation... Click a step to inspect.'}
                  </pre>
                </div>
              </div>
            </div>
          )}

          {/* ─── Tab 3: History ─────────────────────────────────────── */}
          {activeTab === 'history' && (
            <div className="deploy-history-container">
              {loading && <div className="loading-steps-indicator">Loading past deployments...</div>}
              {!loading && deployments.length === 0 && (
                <div className="loading-steps-indicator">No previous deployments found.</div>
              )}
              {deployments.map(run => (
                <div key={run.id} className="hub-section glass mcp-panel-section history-card">
                  <div className="history-header">
                    <div className="history-title-group">
                      <h4>Run #{run.id}: {run.source_doc_name || 'Generic Plan'}</h4>
                      <span className="source-doc-badge">{run.source_doc_type.toUpperCase()}</span>
                    </div>
                    <div className="history-meta">
                      <span className="history-date">{new Date(run.created_at).toLocaleString()}</span>
                      <span className="target-env-tag">{run.target_instance}</span>
                      {getStatusBadge(run.status)}
                    </div>
                  </div>

                  {run.git_repo_url && (
                    <div className="history-details-body">
                      <p className="history-details-content">
                        📦 Git: <code>{run.git_repo_url}</code>
                        {run.ssh_host && <> · 🖥 SSH: <code>{run.ssh_username}@{run.ssh_host}</code></>}
                      </p>
                    </div>
                  )}

                  <div className="history-actions-row">
                    <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                      <button className="btn-outline" onClick={() => selectRunById(run.id, 'terminal')}>
                        <Terminal size={13} style={{ marginRight: '0.35rem', verticalAlign: 'middle' }} />View Logs
                      </button>
                      <button
                        className="btn-outline delete-run-btn"
                        onClick={() => handleDeleteRun(run.id)}
                        disabled={actionLoading === `delete-${run.id}` || TERMINAL_STATUSES.has(run.status)}
                        title={TERMINAL_STATUSES.has(run.status) ? 'Cancel the deployment first' : 'Delete this run'}
                      >
                        {actionLoading === `delete-${run.id}` ? <Loader2 size={13} className="spin-icon" /> : <Trash2 size={13} />}
                      </button>
                    </div>

                    {run.status === 'completed' && (
                      <div className="migrate-group">
                        <span className="migrate-label"><Rocket size={12} style={{ marginRight: '0.25rem', verticalAlign: 'middle' }} />Migrate to:</span>
                        {getMigrateOptions(run.target_instance).map(env => (
                          <button
                            key={env.name}
                            className="btn-primary migrate-btn-tag"
                            onClick={() => handleMigrate(run, env.name)}
                          >
                            {env.name}
                          </button>
                        ))}
                      </div>
                    )}

                    {run.status === 'failed' && (
                      <button
                        className="btn-primary run-action-btn retry-btn"
                        onClick={async () => {
                          await selectRunById(run.id, 'terminal');
                          handleRetry(run.id);
                        }}
                      >
                        <RotateCcw size={13} style={{ marginRight: '0.35rem', verticalAlign: 'middle' }} />Retry
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* ── Migration confirmation modal ─────────────────────────────────────── */}
      {migrateModal && (() => {
        const { run, targetEnvName } = migrateModal;
        const envProfile = environments.find(e => e.name === targetEnvName) || {};
        const hasDbCreds = !!(envProfile.db_user && envProfile.db_password);
        return (
          <div className="migrate-modal-overlay" onClick={() => !migrateSubmitting && setMigrateModal(null)}>
            <div className="migrate-modal" onClick={e => e.stopPropagation()}>
              <div className="migrate-modal-header">
                <Rocket size={16} className="migrate-modal-icon" />
                <span>Migrate to <strong>{targetEnvName}</strong></span>
              </div>

              <div className="migrate-modal-body">
                <div className="migrate-section-label">Source deployment</div>
                <div className="migrate-info-row">
                  <span className="migrate-info-key">Run&nbsp;#</span>
                  <span className="migrate-info-val">#{run.id} — {run.source_doc_name}</span>
                </div>
                <div className="migrate-info-row">
                  <span className="migrate-info-key">From</span>
                  <span className="migrate-info-val">{run.target_instance}</span>
                </div>

                <div className="migrate-section-label" style={{ marginTop: '1rem' }}>Target environment — {targetEnvName}</div>
                {hasDbCreds ? (
                  <>
                    <div className="migrate-info-row">
                      <span className="migrate-info-key">DB host</span>
                      <span className="migrate-info-val">{envProfile.db_host || '—'}:{envProfile.db_port || 1521}</span>
                    </div>
                    <div className="migrate-info-row">
                      <span className="migrate-info-key">DB SID</span>
                      <span className="migrate-info-val">{envProfile.db_sid || '—'}</span>
                    </div>
                    <div className="migrate-info-row">
                      <span className="migrate-info-key">DB user</span>
                      <span className="migrate-info-val">{envProfile.db_user}</span>
                    </div>
                    <div className="migrate-info-row">
                      <span className="migrate-info-key">DB pass</span>
                      <span className="migrate-info-val migrate-masked">••••••••</span>
                    </div>
                  </>
                ) : (
                  <div className="migrate-warn">
                    ⚠️ No DB credentials configured for <strong>{targetEnvName}</strong>.
                    Add them in <strong>Settings → Environments &amp; Database</strong>.
                  </div>
                )}

                <div className="migrate-section-label" style={{ marginTop: '1rem' }}>SSH server</div>
                {serverConnections.length > 0 ? (
                  <select
                    className="migrate-select"
                    value={migrateServerIdx}
                    onChange={e => setMigrateServerIdx(Number(e.target.value))}
                    disabled={migrateSubmitting}
                  >
                    {serverConnections.map((s, i) => (
                      <option key={i} value={i}>
                        {s.name} ({s.username}@{s.hostname}:{s.port || 22})
                      </option>
                    ))}
                    {run.ssh_host && (
                      <option value={-1}>Inherit from source run ({run.ssh_username}@{run.ssh_host})</option>
                    )}
                  </select>
                ) : (
                  <div className="migrate-info-row">
                    <span className="migrate-info-val" style={{ color: 'var(--text-muted)' }}>
                      {run.ssh_host
                        ? `Inheriting from source: ${run.ssh_username}@${run.ssh_host}`
                        : 'No SSH server — simulation mode'}
                    </span>
                  </div>
                )}
              </div>

              <div className="migrate-modal-footer">
                <button
                  className="btn-outline"
                  onClick={() => setMigrateModal(null)}
                  disabled={migrateSubmitting}
                >
                  Cancel
                </button>
                <button
                  className="btn-primary"
                  onClick={confirmMigrate}
                  disabled={migrateSubmitting}
                >
                  {migrateSubmitting
                    ? <><Loader2 size={13} className="spin-icon" style={{ marginRight: '0.4rem' }} />Migrating…</>
                    : <><Rocket size={13} style={{ marginRight: '0.4rem' }} />Confirm &amp; Migrate</>
                  }
                </button>
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}
