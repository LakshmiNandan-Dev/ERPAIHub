import { useState, useEffect, useRef } from 'react';
import api from '../../api';
import './RagUpload.css';

export default function RagUpload({ onClose }) {
  const [activeTab, setActiveTab] = useState('kb'); // 'kb', 'rl', or 'mcp'
  const [documents, setDocuments] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState('');
  const [pollTimer, setPollTimer] = useState(null);
  
  // RL & RLAIF Stats State
  const [rlStats, setRlStats] = useState({
    total_rated: 0,
    thumbs_up: 0,
    thumbs_down: 0,
    with_corrections: 0,
    rlaif_total: 0,
    rlaif_passed: 0,
    rlaif_failed: 0,
    rlaif_corrections: 0
  });

  // MCP Credentials & Status State
  const [dbHost, setDbHost] = useState(localStorage.getItem('mcp_db_host') || 'localhost');
  const [dbPort, setDbPort] = useState(localStorage.getItem('mcp_db_port') || '5432');
  const [dbUser, setDbUser] = useState(localStorage.getItem('mcp_db_user') || 'postgres');
  const [dbName, setDbName] = useState(localStorage.getItem('mcp_db_name') || 'oraebs');
  const [dbPass, setDbPass] = useState('••••••••');
  const [dbStatus, setDbStatus] = useState('success'); // 'success', 'loading', 'error'
  const [copied, setCopied] = useState(false);

  // Enabled MCP Connectors State
  const [enabledServers, setEnabledServers] = useState({
    ebs: true,
    github: false,
    filesystem: false,
    postgres: false,
    gdrive: false,
    memory: false,
    teams: false
  });

  const mcpRegistry = {
    ebs: {
      name: "Oracle EBS AI Agent",
      icon: "🔌",
      desc: "Local diagnostic tools, database read safety audits, active concurrent jobs tracker, and RAG knowledge search.",
      config: {
        "command": "/Users/nagendrapalla/AI/programming/react/oraebsagent/api/.venv/bin/python",
        "args": ["-m", "app.mcp_server"],
        "cwd": "/Users/nagendrapalla/AI/programming/react/oraebsagent/api"
      }
    },
    github: {
      name: "GitHub Developer Connector",
      icon: "🐙",
      desc: "Enables LLMs to inspect files, search repositories, manage issues, and commit code directly.",
      config: {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {
          "GITHUB_PERSONAL_ACCESS_TOKEN": "YOUR_GITHUB_TOKEN_HERE"
        }
      }
    },
    teams: {
      name: "Microsoft Teams (M365)",
      icon: "💬",
      desc: "Integrate Teams, Outlook, and Office 365. Reads chats, lists channels, manages calendar meetings, and posts messages via Microsoft Graph API.",
      config: {
        "command": "npx",
        "args": ["-y", "@softeria/ms-365-mcp-server", "--org-mode"]
      }
    },
    filesystem: {
      name: "Local Filesystem Agent",
      icon: "📂",
      desc: "Provides secure access to read and write allowed folders on your local workstation.",
      config: {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/nagendrapalla/AI/programming"]
      }
    },
    postgres: {
      name: "Standard PostgreSQL Client",
      icon: "🗄️",
      desc: "Executes raw queries, inspects database column configurations, and manages direct schema migrations.",
      config: {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres", "postgresql://postgres:password@localhost:5432/oraebs"]
      }
    },
    gdrive: {
      name: "Google Drive Indexer",
      icon: "📁",
      desc: "Gives AI models read access to spreadsheets, docs, and folders inside your workspace.",
      config: {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-gdrive"]
      }
    },
    memory: {
      name: "Persistent Memory Graph",
      icon: "🧠",
      desc: "Enables long-term recall using a semantic knowledge graph to persist facts across chat turns.",
      config: {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"]
      }
    }
  };

  const getDynamicConfig = () => {
    const servers = {};
    Object.keys(enabledServers).forEach(key => {
      if (enabledServers[key]) {
        servers[key === 'ebs' ? 'oracle-ebs-agent' : key] = mcpRegistry[key].config;
      }
    });
    return { mcpServers: servers };
  };

  const fileInputRef = useRef(null);

  const handleTestConnection = () => {
    setDbStatus('loading');
    setTimeout(() => {
      localStorage.setItem('mcp_db_host', dbHost);
      localStorage.setItem('mcp_db_port', dbPort);
      localStorage.setItem('mcp_db_user', dbUser);
      localStorage.setItem('mcp_db_name', dbName);
      setDbStatus('success');
    }, 1200);
  };

  const handleCopyConfig = () => {
    const config = getDynamicConfig();
    navigator.clipboard.writeText(JSON.stringify(config, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const fetchDocuments = async () => {
    try {
      const res = await api.get('/rag/documents');
      setDocuments(res.data);
    } catch (err) {
      console.error('Failed to fetch documents', err);
    }
  };

  const fetchRlStats = async () => {
    try {
      const res = await api.get('/rl/stats');
      setRlStats(res.data);
    } catch (err) {
      console.error('Failed to fetch RL stats', err);
    }
  };

  useEffect(() => {
    fetchDocuments();
    fetchRlStats();

    // Poll every 3s while any docs are indexing
    const timer = setInterval(() => {
      fetchDocuments();
      fetchRlStats();
    }, 3000);

    setPollTimer(timer);
    return () => clearInterval(timer);
  }, []);

  const handleUpload = async (file) => {
    if (!file) return;

    const ext = file.name.split('.').pop().toLowerCase();
    if (!['pdf', 'txt', 'md'].includes(ext)) {
      setError('Only PDF, TXT, and MD files are supported.');
      return;
    }

    setError('');
    setUploading(true);
    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await api.post('/rag/upload', formData, {
        headers: { 'Content-Type': 'multipart/form-data' }
      });
      setDocuments(prev => [res.data, ...prev]);
    } catch (err) {
      setError(err.response?.data?.detail || 'Upload failed.');
    } finally {
      setUploading(false);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    handleUpload(e.dataTransfer.files[0]);
  };

  const handleDelete = async (docId) => {
    try {
      await api.delete(`/rag/documents/${docId}`);
      setDocuments(prev => prev.filter(d => d.id !== docId));
    } catch (err) {
      console.error('Delete failed', err);
    }
  };

  const handleExportDpo = async (type = 'rlhf') => {
    try {
      const token = localStorage.getItem('session_token');
      const response = await fetch(`${api.defaults.baseURL}/rl/export?type=${type}`, {
        headers: {
          'Authorization': `Bearer ${token}`
        }
      });
      if (!response.ok) throw new Error('Export failed');
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `ebs_${type}_preference_dataset.jsonl`;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (err) {
      console.error('Export failed', err);
      setError(`Failed to download ${type.toUpperCase()} dataset.`);
    }
  };

  const statusBadge = (status) => {
    const map = {
      indexing: { label: '⏳ Indexing...', cls: 'badge-indexing' },
      ready: { label: '✅ Ready', cls: 'badge-ready' },
      failed: { label: '❌ Failed', cls: 'badge-failed' },
    };
    const s = map[status] || { label: status, cls: '' };
    return <span className={`status-badge ${s.cls}`}>{s.label}</span>;
  };

  return (
    <div className="rag-overlay" onClick={onClose}>
      <div className="rag-panel glass" onClick={e => e.stopPropagation()}>
        <div className="rag-header">
          <div>
            <h2>📚 Enterprise Optimization Center</h2>
            <p>Ground AI responses using RAG, and collect preference training data to enable Reinforcement Learning.</p>
          </div>
          <button className="close-btn" onClick={onClose}>✕</button>
        </div>

        {/* Navigation Tabs */}
        <div className="center-tabs">
          <button 
            className={`tab-btn ${activeTab === 'kb' ? 'active' : ''}`}
            onClick={() => setActiveTab('kb')}
          >
            📚 RAG Knowledge Base
          </button>
          <button 
            className={`tab-btn ${activeTab === 'rl' ? 'active' : ''}`}
            onClick={() => setActiveTab('rl')}
          >
            🧠 RL & Alignment Hub
          </button>
          <button 
            className={`tab-btn ${activeTab === 'mcp' ? 'active' : ''}`}
            onClick={() => setActiveTab('mcp')}
          >
            🔌 Connectors & MCP
          </button>
        </div>

        {error && <div className="rag-error">{error}</div>}

        {activeTab === 'kb' && (
          <>
            {/* Drop Zone */}
            <div
              className={`drop-zone ${dragOver ? 'drag-over' : ''} ${uploading ? 'uploading' : ''}`}
              onDragOver={e => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
              onClick={() => !uploading && fileInputRef.current.click()}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept=".pdf,.txt,.md"
                style={{ display: 'none' }}
                onChange={e => handleUpload(e.target.files[0])}
              />
              {uploading ? (
                <p>⏳ Uploading and queueing for indexing...</p>
              ) : (
                <>
                  <div className="drop-icon">📄</div>
                  <p>Drop a file here or <span className="upload-link">click to browse</span></p>
                  <p className="drop-hint">Supports PDF, TXT, MD · Max 50MB</p>
                </>
              )}
            </div>

            {/* Documents Table */}
            <div className="docs-list">
              {documents.length === 0 ? (
                <p className="no-docs">No documents uploaded yet.</p>
              ) : (
                <table className="docs-table">
                  <thead>
                    <tr>
                      <th>Filename</th>
                      <th>Type</th>
                      <th>Status</th>
                      <th>Chunks</th>
                      <th>Uploaded</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {documents.map(doc => (
                      <tr key={doc.id}>
                        <td className="doc-name">{doc.filename}</td>
                        <td><span className="type-badge">.{doc.file_type}</span></td>
                        <td>{statusBadge(doc.status)}</td>
                        <td>{doc.chunk_count ?? '—'}</td>
                        <td>{new Date(doc.created_at).toLocaleDateString()}</td>
                        <td>
                          <button
                            className="doc-delete-btn"
                            onClick={() => handleDelete(doc.id)}
                            title="Delete document"
                          >🗑</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}

        {activeTab === 'rl' && (
          <div className="rl-hub-container">
            {/* Human Feedback Block */}
            <div className="hub-section">
              <h3 className="section-title">👥 Human Feedback Metrics (RLHF)</h3>
              <div className="stats-grid">
                <div className="stat-card glass">
                  <div className="stat-value">{rlStats.total_rated}</div>
                  <div className="stat-label">Total Rated Samples</div>
                </div>
                <div className="stat-card glass">
                  <div className="stat-value text-green">👍 {rlStats.thumbs_up}</div>
                  <div className="stat-label">Helpful Upvotes</div>
                </div>
                <div className="stat-card glass">
                  <div className="stat-value text-red">👎 {rlStats.thumbs_down}</div>
                  <div className="stat-label">Critic Downvotes</div>
                </div>
                <div className="stat-card glass">
                  <div className="stat-value text-purple">🎯 {rlStats.with_corrections}</div>
                  <div className="stat-label">Gold Corrections</div>
                </div>
              </div>
            </div>

            {/* AI Feedback Block */}
            <div className="hub-section">
              <h3 className="section-title">🤖 AI Automated Critique Metrics (RLAIF)</h3>
              <div className="stats-grid">
                <div className="stat-card glass">
                  <div className="stat-value">{rlStats.rlaif_total}</div>
                  <div className="stat-label">Total AI Audits Run</div>
                </div>
                <div className="stat-card glass">
                  <div className="stat-value text-green">🛡️ {rlStats.rlaif_passed}</div>
                  <div className="stat-label">Passed Grounding</div>
                </div>
                <div className="stat-card glass">
                  <div className="stat-value text-red">⚠️ {rlStats.rlaif_failed}</div>
                  <div className="stat-label">Auditor Warnings</div>
                </div>
                <div className="stat-card glass">
                  <div className="stat-value text-purple">💡 {rlStats.rlaif_corrections}</div>
                  <div className="stat-label">AI Auto-Corrections</div>
                </div>
              </div>
            </div>

            {/* Export & Action Hub */}
            <div className="alignment-actions glass">
              <h3>Export Alignment Preference Datasets</h3>
              <p>
                Download a Direct Preference Optimization (DPO) formatted preference dataset containing prompt-response pairs to train and align your local model.
              </p>
              <div className="export-btn-group">
                <button 
                  className="btn-primary export-btn" 
                  onClick={() => handleExportDpo('rlhf')}
                  disabled={rlStats.total_rated === 0}
                >
                  👥 Export Human RLHF Dataset (JSONL)
                </button>
                <button 
                  className="btn-secondary export-btn" 
                  onClick={() => handleExportDpo('rlaif')}
                  disabled={rlStats.rlaif_total === 0}
                >
                  🤖 Export AI RLAIF Dataset (JSONL)
                </button>
              </div>
            </div>

            {/* Quick Tutorial */}
            <div className="tutorial-container glass">
              <h4>🔥 Aligning your LLM (DPO Training)</h4>
              <p>Feed the exported dataset directly into standard frameworks to fine-tune your model to match correct Oracle EBS guidelines:</p>
              <pre className="code-block-preview">
{`from trl import DPOTrainer
# Load model and either ebs_rlhf_dataset or ebs_rlaif_dataset
trainer = DPOTrainer(
    model=llama_model,
    beta=0.1, 
    train_dataset=ebs_preference_dataset,
    tokenizer=tokenizer
)
trainer.train()`}
              </pre>
            </div>
          </div>
        )}

        {activeTab === 'mcp' && (
          <div className="rl-hub-container">
            {/* Database access details configuration block */}
            <div className="hub-section glass mcp-panel-section">
              <h3 className="section-title">🔌 Oracle EBS Database Connector Credentials</h3>
              <p className="mcp-desc">Configure host details and access parameters for the Mock EBS Diagnostic Connector.</p>
              
              <div className="mcp-grid-inputs">
                <div className="input-group">
                  <label>Database Host</label>
                  <input 
                    type="text" 
                    value={dbHost} 
                    onChange={e => setDbHost(e.target.value)} 
                    placeholder="localhost" 
                  />
                </div>
                <div className="input-group">
                  <label>Port</label>
                  <input 
                    type="text" 
                    value={dbPort} 
                    onChange={e => setDbPort(e.target.value)} 
                    placeholder="5432" 
                  />
                </div>
                <div className="input-group">
                  <label>Username</label>
                  <input 
                    type="text" 
                    value={dbUser} 
                    onChange={e => setDbUser(e.target.value)} 
                    placeholder="postgres" 
                  />
                </div>
                <div className="input-group">
                  <label>Database Name</label>
                  <input 
                    type="text" 
                    value={dbName} 
                    onChange={e => setDbName(e.target.value)} 
                    placeholder="oraebs" 
                  />
                </div>
                <div className="input-group full-width">
                  <label>Password</label>
                  <input 
                    type="password" 
                    value={dbPass} 
                    onChange={e => setDbPass(e.target.value)} 
                  />
                </div>
              </div>
              
              <div className="mcp-actions-row">
                <button 
                  className={`btn-primary mcp-save-btn ${dbStatus}`} 
                  onClick={handleTestConnection}
                  disabled={dbStatus === 'loading'}
                >
                  {dbStatus === 'loading' ? '⏳ Testing Connection...' : '⚡ Test & Save EBS Connector'}
                </button>
                {dbStatus === 'success' && (
                  <span className="connection-badge success">🟢 Connected to Mock EBS Database</span>
                )}
              </div>
            </div>

            {/* Recommended MCP Directory */}
            <div className="hub-section glass mcp-panel-section">
              <h3 className="section-title">🌐 Recommended & Frequently Used MCP Servers</h3>
              <p className="mcp-desc">Toggle the servers you wish to enable. The Claude Desktop settings file below will compile and update in real-time!</p>
              
              <div className="mcp-directory-grid">
                {Object.keys(mcpRegistry).map(key => {
                  const s = mcpRegistry[key];
                  return (
                    <div key={key} className={`mcp-dir-card glass ${enabledServers[key] ? 'enabled' : ''}`}>
                      <div className="mcp-card-header">
                        <span className="mcp-card-icon">{s.icon}</span>
                        <div className="mcp-card-title-group">
                          <h4>{s.name}</h4>
                          <span className="mcp-key-badge">{key === 'ebs' ? 'oracle-ebs-agent' : key}</span>
                        </div>
                        <label className="switch-toggle">
                          <input 
                            type="checkbox" 
                            checked={enabledServers[key]} 
                            onChange={e => {
                              if (key === 'ebs') return; // EBS is mandatory
                              setEnabledServers(prev => ({
                                ...prev,
                                [key]: e.target.checked
                              }));
                            }} 
                            disabled={key === 'ebs'}
                          />
                          <span className="slider"></span>
                        </label>
                      </div>
                      <p className="mcp-card-desc">{s.desc}</p>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* MCP Connector details block */}
            <div className="hub-section glass mcp-panel-section">
              <h3 className="section-title">🛠️ Claude Desktop Model Context Protocol (MCP) Integration</h3>
              <p className="mcp-desc">
                Enable external AI models (like Claude Desktop) to directly use your Oracle EBS agent diagnostics, databases, concurrent programs, and RAG knowledge.
              </p>
              
              <div className="mcp-code-container">
                <div className="mcp-code-header">
                  <span>claude_desktop_config.json Settings</span>
                  <button className="copy-config-btn" onClick={handleCopyConfig}>
                    {copied ? '✓ Copied!' : '📋 Copy Config'}
                  </button>
                </div>
                <pre className="code-block-preview">
{JSON.stringify(getDynamicConfig(), null, 2)}
                </pre>
              </div>
              
              <div className="tutorial-container">
                <h4>🚀 How to activate the MCP server:</h4>
                <p>1. Copy the compiled JSON block above.</p>
                <p>2. Open your Claude Desktop settings file (located at <code>~/Library/Application Support/Claude/claude_desktop_config.json</code> on macOS).</p>
                <p>3. Paste the config block inside the main object, save, and restart Claude Desktop!</p>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
