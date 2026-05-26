import { useState, useEffect, useRef } from 'react';
import api from '../../api';
import './ChatLayout.css';
import RagUpload from '../Rag/RagUpload';
import DeploymentCenter from '../Deployment/DeploymentCenter';
import PerformanceAgent from '../Performance/PerformanceAgent';
import ReactMarkdown from 'react-markdown';
import {
  BrainCircuit, MessageSquarePlus, Trash2, Settings2, LogOut,
  Server, Globe, Bot, Cpu, Pencil, Check, X
} from 'lucide-react';

export default function ChatLayout({ setAuthToken }) {
  const [sessions, setSessions] = useState([]);
  const [activeSession, setActiveSession] = useState(null);
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const [deletingId, setDeletingId] = useState(null);
  const [renamingId, setRenamingId] = useState(null);
  const [renameValue, setRenameValue] = useState('');
  const renameInputRef = useRef(null);
  const [showKnowledgeBase, setShowKnowledgeBase] = useState(false);
  const [showDeployments, setShowDeployments] = useState(false);
  const [showPerformance, setShowPerformance] = useState(false);
  const [activeCorrectionId, setActiveCorrectionId] = useState(null);
  const [correctionText, setCorrectionText] = useState('');

  // Deployment history side panel states
  const [deployHistory, setDeployHistory] = useState([]);
  const [selectedHistoryRunId, setSelectedHistoryRunId] = useState(null);

  // Agent selector & User Settings menu
  const [activeAgent, setActiveAgent] = useState('diagnostic');
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [showSettingsModal, setShowSettingsModal] = useState(false);
  const [settingsTab, setSettingsTab] = useState('servers'); // 'servers' | 'environments' | 'llm'

  // LLM provider config
  const _llmStored = (() => { try { return JSON.parse(localStorage.getItem('llm_config') || '{}'); } catch { return {}; } })();
  const [llmProvider, setLlmProvider] = useState(_llmStored.provider || 'ollama');
  const [llmModel, setLlmModel] = useState(_llmStored.model || '');
  const [llmApiKey, setLlmApiKey] = useState(_llmStored.api_key || '');
  const [llmBaseUrl, setLlmBaseUrl] = useState(_llmStored.base_url || 'http://localhost:11434');

  const LLM_DEFAULTS = { ollama: 'llama3.2:1b', openai: 'gpt-4o-mini', anthropic: 'claude-haiku-4-5-20251001' };

  const handleSaveLLMConfig = (e) => {
    e.preventDefault();
    const config = {
      provider: llmProvider,
      model: llmModel.trim() || LLM_DEFAULTS[llmProvider],
      api_key: llmApiKey.trim(),
      base_url: llmBaseUrl.trim() || 'http://localhost:11434',
    };
    localStorage.setItem('llm_config', JSON.stringify(config));
    setLlmModel(config.model);
  };

  // SSH Server connections
  const [serverConnections, setServerConnections] = useState(() => {
    try { return JSON.parse(localStorage.getItem('ebs_server_connections') || '[]'); } catch { return []; }
  });
  const [isAddingSrv, setIsAddingSrv] = useState(false);
  const [editingSrvIndex, setEditingSrvIndex] = useState(null);
  const [srvFormName, setSrvFormName] = useState('');
  const [srvFormHostname, setSrvFormHostname] = useState('');
  const [srvFormPort, setSrvFormPort] = useState('22');
  const [srvFormUsername, setSrvFormUsername] = useState('');
  const [srvFormPassword, setSrvFormPassword] = useState('');
  const [srvFormType, setSrvFormType] = useState('application');
  const [srvFormServices, setSrvFormServices] = useState({ web: true, forms: true, concurrent: true });

  // Environments with Oracle DB credentials
  const [environments, setEnvironments] = useState(() => {
    try {
      const stored = JSON.parse(localStorage.getItem('ebs_environments') || '[]');
      return stored.filter(e => e.name).map(e => ({
        name: e.name,
        db_host: e.db_host || '',
        db_port: e.db_port || 1521,
        db_sid: e.db_sid || '',
        db_user: e.db_user || 'apps',
        db_password: e.db_password || '',
      }));
    } catch { return []; }
  });
  const [isAddingEnv, setIsAddingEnv] = useState(false);
  const [editingEnvIndex, setEditingEnvIndex] = useState(null);
  const [envFormName, setEnvFormName] = useState('');
  const [envFormDbHost, setEnvFormDbHost] = useState('');
  const [envFormDbPort, setEnvFormDbPort] = useState('1521');
  const [envFormDbSid, setEnvFormDbSid] = useState('');
  const [envFormDbUser, setEnvFormDbUser] = useState('apps');
  const [envFormDbPassword, setEnvFormDbPassword] = useState('');

  const messagesEndRef = useRef(null);
  const userMenuRef = useRef(null);
  const user = JSON.parse(localStorage.getItem('user') || '{}');

  useEffect(() => {
    function handleClickOutside(event) {
      if (userMenuRef.current && !userMenuRef.current.contains(event.target)) {
        setShowUserMenu(false);
      }
    }
    document.addEventListener("mousedown", handleClickOutside);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
    };
  }, [userMenuRef]);

  const resetSrvForm = () => {
    setSrvFormName(''); setSrvFormHostname(''); setSrvFormPort('22');
    setSrvFormUsername(''); setSrvFormPassword('');
    setSrvFormType('application');
    setSrvFormServices({ web: true, forms: true, concurrent: true });
  };

  const handleSaveServer = (e) => {
    e.preventDefault();
    if (!srvFormName.trim() || !srvFormHostname.trim()) return;
    const entry = {
      name: srvFormName.trim(),
      hostname: srvFormHostname.trim(),
      port: parseInt(srvFormPort) || 22,
      username: srvFormUsername.trim(),
      password: srvFormPassword.trim(),
      server_type: srvFormType,
      app_services: srvFormType === 'application'
        ? Object.entries(srvFormServices).filter(([, v]) => v).map(([k]) => k)
        : [],
    };
    const updated = editingSrvIndex !== null
      ? serverConnections.map((s, i) => i === editingSrvIndex ? entry : s)
      : [...serverConnections, entry];
    setServerConnections(updated);
    localStorage.setItem('ebs_server_connections', JSON.stringify(updated));
    setIsAddingSrv(false);
    setEditingSrvIndex(null);
    resetSrvForm();
  };

  const handleEditServer = (index) => {
    const s = serverConnections[index];
    setEditingSrvIndex(index);
    setIsAddingSrv(true);
    setSrvFormName(s.name);
    setSrvFormHostname(s.hostname);
    setSrvFormPort(String(s.port || 22));
    setSrvFormUsername(s.username || '');
    setSrvFormPassword(s.password || '');
    setSrvFormType(s.server_type || 'application');
    const svc = { web: false, forms: false, concurrent: false };
    (s.app_services || []).forEach(k => { if (k in svc) svc[k] = true; });
    setSrvFormServices(svc);
  };

  const handleDeleteServer = (index) => {
    if (!window.confirm('Delete this server connection?')) return;
    const updated = serverConnections.filter((_, i) => i !== index);
    setServerConnections(updated);
    localStorage.setItem('ebs_server_connections', JSON.stringify(updated));
  };

  const resetEnvForm = () => {
    setEnvFormName(''); setEnvFormDbHost(''); setEnvFormDbPort('1521');
    setEnvFormDbSid(''); setEnvFormDbUser('apps'); setEnvFormDbPassword('');
  };

  const handleSaveEnv = (e) => {
    e.preventDefault();
    if (!envFormName.trim()) return;
    const entry = {
      name: envFormName.trim().toUpperCase(),
      db_host: envFormDbHost.trim(),
      db_port: parseInt(envFormDbPort) || 1521,
      db_sid: envFormDbSid.trim(),
      db_user: envFormDbUser.trim(),
      db_password: envFormDbPassword.trim(),
    };
    const updated = editingEnvIndex !== null
      ? environments.map((e, i) => i === editingEnvIndex ? entry : e)
      : [...environments, entry];
    setEnvironments(updated);
    localStorage.setItem('ebs_environments', JSON.stringify(updated));
    setIsAddingEnv(false);
    setEditingEnvIndex(null);
    resetEnvForm();
  };

  const handleEditEnv = (index) => {
    const env = environments[index];
    setEditingEnvIndex(index);
    setIsAddingEnv(true);
    setEnvFormName(env.name);
    setEnvFormDbHost(env.db_host || '');
    setEnvFormDbPort(String(env.db_port || 1521));
    setEnvFormDbSid(env.db_sid || '');
    setEnvFormDbUser(env.db_user || 'apps');
    setEnvFormDbPassword(env.db_password || '');
  };

  const handleDeleteEnv = (index) => {
    if (!window.confirm('Delete this environment?')) return;
    const updated = environments.filter((_, i) => i !== index);
    setEnvironments(updated);
    localStorage.setItem('ebs_environments', JSON.stringify(updated));
  };

  const handleAgentChange = (val) => {
    setActiveAgent(val);
    if (val === 'deployments') {
      setShowDeployments(true);
    } else if (val === 'kb') {
      setShowKnowledgeBase(true);
    } else if (val === 'performance') {
      setShowPerformance(true);
    }
  };

  const getUserInitials = (name) => {
    if (!name) return 'NP';
    const parts = name.split(/[\s_.]+/);
    if (parts.length >= 2) {
      return (parts[0][0] + parts[1][0]).toUpperCase();
    }
    return name.slice(0, 2).toUpperCase();
  };

  const fetchSessions = async () => {
    try {
      const res = await api.get('/chat/sessions');
      setSessions(res.data);
      if (res.data.length > 0 && !activeSession) {
        setActiveSession(res.data[0]);
      }
    } catch (err) {
      console.error('Failed to fetch sessions', err);
    }
  };

  const fetchMessages = async (sessionId) => {
    try {
      const res = await api.get(`/chat/sessions/${sessionId}/messages`);
      setMessages(res.data);
    } catch (err) {
      console.error('Failed to fetch messages', err);
    }
  };

  useEffect(() => {
    fetchSessions();
  }, []);

  const fetchDeployHistory = async () => {
    try {
      const res = await api.get('/deployments/');
      setDeployHistory(res.data);
    } catch (err) {
      console.error('Failed to fetch deployment history', err);
    }
  };

  useEffect(() => {
    fetchDeployHistory();
    const interval = setInterval(fetchDeployHistory, 4000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (activeSession) {
      fetchMessages(activeSession.id);
    } else {
      setMessages([]);
    }
  }, [activeSession]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const createNewSession = async () => {
    // If the most recent session is already empty, just switch to it
    const latestSession = sessions[0];
    if (latestSession && latestSession.title === 'New Conversation') {
      setActiveSession(latestSession);
      return;
    }
    // Also block if the currently active session has no messages yet
    if (activeSession && messages.length === 0) {
      return;
    }
    try {
      const res = await api.post('/chat/sessions', { title: 'New Conversation' });
      setSessions(prev => [res.data, ...prev]);
      setActiveSession(res.data);
      setMessages([]);
    } catch (err) {
      console.error('Failed to create session', err);
    }
  };

  const handleDeleteSession = async (e, sessionId) => {
    e.stopPropagation(); // Don't trigger session selection
    setDeletingId(sessionId);
    try {
      await api.delete(`/chat/sessions/${sessionId}`);
      const remaining = sessions.filter(s => s.id !== sessionId);
      setSessions(remaining);
      if (activeSession?.id === sessionId) {
        setActiveSession(remaining.length > 0 ? remaining[0] : null);
      }
    } catch (err) {
      console.error('Failed to delete session', err);
    } finally {
      setDeletingId(null);
    }
  };

  const startRename = (e, session) => {
    e.stopPropagation();
    setRenamingId(session.id);
    setRenameValue(session.title || '');
    setTimeout(() => renameInputRef.current?.select(), 0);
  };

  const commitRename = async (sessionId) => {
    // Read from the DOM directly — avoids any stale closure on renameValue
    const title = (renameInputRef.current?.value ?? renameValue).trim();
    if (!title) { cancelRename(); return; }

    // Apply immediately so the user sees the new title at once
    setSessions(prev => prev.map(s => s.id === sessionId ? { ...s, title } : s));
    if (activeSession?.id === sessionId) setActiveSession(prev => ({ ...prev, title }));
    setRenamingId(null);

    try {
      await api.patch(`/chat/sessions/${sessionId}`, { title });
    } catch (err) {
      console.error('Rename failed — keeping UI title, will sync on next refresh', err);
      // Don't revert: the user intentionally renamed it; it syncs on next page load
    }
  };

  const cancelRename = () => setRenamingId(null);

  const handleSendMessage = async (e) => {
    e.preventDefault();
    if (!inputMessage.trim() || !activeSession) return;

    const userMessage = { role: 'user', content: inputMessage };
    setMessages(prev => [...prev, userMessage]);
    const sentContent = inputMessage;
    setInputMessage('');
    setLoading(true);

    // Add an empty assistant bubble that we'll fill token by token
    setMessages(prev => [...prev, { role: 'assistant', content: '' }]);

    try {
      const token = localStorage.getItem('session_token');
      const response = await fetch(
        `http://127.0.0.1:8000/chat/sessions/${activeSession.id}/stream`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`
          },
          body: JSON.stringify({ content: sentContent })
        }
      );

      if (!response.ok) throw new Error(`HTTP ${response.status}`);

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop(); // keep incomplete line for next chunk

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6).trim();
          if (payload === '[DONE]') break;

          try {
            const parsed = JSON.parse(payload);
            if (parsed.error) {
              setMessages(prev => {
                const msgs = [...prev];
                msgs[msgs.length - 1] = { role: 'assistant', content: `⚠️ ${parsed.error}` };
                return msgs;
              });
              break;
            }
            // Append token to last assistant bubble
            setMessages(prev => {
              const msgs = [...prev];
              msgs[msgs.length - 1] = {
                ...msgs[msgs.length - 1],
                content: msgs[msgs.length - 1].content + parsed
              };
              return msgs;
            });
          } catch { /* skip malformed lines */ }
        }
      }

      // Update session title in sidebar only when it's still the default placeholder
      const isDefaultTitle = !activeSession.title || activeSession.title === 'New Conversation';
      if (isDefaultTitle) {
        const autoTitle = sentContent.substring(0, 50) + (sentContent.length > 50 ? '...' : '');
        setSessions(prev => prev.map(s =>
          s.id === activeSession.id ? { ...s, title: autoTitle } : s
        ));
        setActiveSession(prev => ({ ...prev, title: autoTitle }));
      }

      // Synchronize with database to get true message IDs for feedback
      await fetchMessages(activeSession.id);

    } catch (err) {
      console.error('Failed to send message', err);
      setMessages(prev => {
        const msgs = [...prev];
        msgs[msgs.length - 1] = { role: 'assistant', content: '⚠️ Failed to get a response. Please check that Ollama is running.' };
        return msgs;
      });
    } finally {
      setLoading(false);
    }
  };

  const handleFeedback = async (messageId, rating, correction = '', notes = '') => {
    try {
      const res = await api.post(`/rl/messages/${messageId}/feedback`, {
        rating,
        correction: correction || null,
        notes: notes || null
      });
      // Update message list with the returned feedback values
      setMessages(prev => prev.map(m => m.id === messageId ? { ...m, ...res.data } : m));
      setActiveCorrectionId(null);
      setCorrectionText('');
    } catch (err) {
      console.error('Failed to submit feedback', err);
    }
  };

  const handleLogout = () => {
    localStorage.removeItem('session_token');
    localStorage.removeItem('user');
    setAuthToken(null);
  };

  return (
    <div className="chat-layout app-container">
      {/* Sidebar */}
      <aside className="chat-sidebar glass">
        <div className="sidebar-header">
          <div className="sidebar-brand">
            <BrainCircuit size={20} className="brand-svg-icon" />
            <h2>AI Agent Hub</h2>
          </div>
          <button onClick={createNewSession} className="btn-primary new-chat-btn">
            <MessageSquarePlus size={13} style={{ marginRight: '0.3rem', verticalAlign: 'middle' }} />
            New Chat
          </button>
        </div>

        <div className="session-list">
          {sessions.length === 0 && (
            <div className="no-sessions">No chats yet. Start a new one!</div>
          )}
          {sessions.map(session => (
            <div
              key={session.id}
              className={`session-item ${activeSession?.id === session.id ? 'active' : ''} ${renamingId === session.id ? 'renaming' : ''}`}
              onClick={() => renamingId !== session.id && setActiveSession(session)}
            >
              <div className="session-item-content">
                {renamingId === session.id ? (
                  <input
                    ref={renameInputRef}
                    className="session-rename-input"
                    value={renameValue}
                    onChange={e => setRenameValue(e.target.value)}
                    onKeyDown={e => {
                      if (e.key === 'Enter') commitRename(session.id);
                      if (e.key === 'Escape') cancelRename();
                    }}
                    onClick={e => e.stopPropagation()}
                    autoFocus
                  />
                ) : (
                  <>
                    <div className="session-title">{session.title || 'New Conversation'}</div>
                    <div className="session-date">{new Date(session.created_at).toLocaleDateString()}</div>
                  </>
                )}
              </div>
              <div className="session-item-actions">
                {renamingId === session.id ? (
                  <>
                    <button className="session-action-btn confirm" onClick={e => { e.stopPropagation(); commitRename(session.id); }} title="Save">
                      <Check size={11} />
                    </button>
                    <button className="session-action-btn cancel" onClick={e => { e.stopPropagation(); cancelRename(); }} title="Cancel">
                      <X size={11} />
                    </button>
                  </>
                ) : (
                  <>
                    <button className="session-action-btn rename-btn" onClick={e => startRename(e, session)} title="Rename">
                      <Pencil size={11} />
                    </button>
                    <button
                      className="delete-btn"
                      onClick={(e) => handleDeleteSession(e, session.id)}
                      disabled={deletingId === session.id}
                      title="Delete chat"
                    >
                      {deletingId === session.id ? '…' : <Trash2 size={12} />}
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>

        <div className="sidebar-footer">
          <div style={{ textAlign: 'center', fontSize: '0.75rem', color: 'var(--text-muted)' }}>
            AI Agent Hub · Active
          </div>
        </div>
      </aside>

      {/* Main Chat Area */}
      <main className="chat-main">
        <header className="chat-header glass">
          <div className="header-left-side">
            <div className="agent-selector-wrapper">
              <label htmlFor="agent-dropdown" className="agent-select-label">Active Agent:</label>
              <select 
                id="agent-dropdown"
                className="agent-select-dropdown" 
                value={activeAgent} 
                onChange={(e) => handleAgentChange(e.target.value)}
              >
                <option value="diagnostic">🤖 General Diagnostic Assistant</option>
                <option value="deployments">🚀 Code Deployment Agent</option>
                <option value="kb">📚 RAG Knowledge Base Agent</option>
                <option value="performance">⚡ Performance Analyzer</option>
                <option value="finance" disabled>💸 Cash Management Agent (Soon)</option>
                <option value="purchasing" disabled>🛒 Purchasing PO Agent (Soon)</option>
              </select>
            </div>
            <h4 className="chat-header-title">
              {activeSession ? `·  ${activeSession.title || 'New Conversation'}` : ''}
            </h4>
          </div>

          <div className="user-profile-wrapper" ref={userMenuRef}>
            <div className="user-avatar-badge" onClick={() => setShowUserMenu(!showUserMenu)}>
              <div className="user-avatar-initials">{getUserInitials(user.username || 'Nagendra Palla')}</div>
              <span className="user-avatar-name">{user.username || 'Nagendra Palla'}</span>
              <span className="chevron-icon">▼</span>
            </div>
            {showUserMenu && (
              <div className="user-dropdown-menu">
                <div className="user-dropdown-header">
                  <strong>{user.username || 'Nagendra Palla'}</strong>
                  <span className="user-role-badge">Administrator</span>
                </div>
                <div className="dropdown-divider" />
                <button className="dropdown-item" onClick={() => { setShowUserMenu(false); setSettingsTab('servers'); setShowSettingsModal(true); }}>
                  <Server size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />SSH Server Connections
                </button>
                <button className="dropdown-item" onClick={() => { setShowUserMenu(false); setSettingsTab('environments'); setShowSettingsModal(true); }}>
                  <Globe size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Environments & Database
                </button>
                <button className="dropdown-item" onClick={() => { setShowUserMenu(false); setSettingsTab('llm'); setShowSettingsModal(true); }}>
                  <Cpu size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />AI Model Settings
                </button>
                <div className="dropdown-divider" />
                <button className="dropdown-item logout" onClick={handleLogout}>
                  <LogOut size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Logout
                </button>
              </div>
            )}
          </div>
        </header>

        <div className="messages-container">
          {!activeSession && (
            <div className="empty-state">
              <h3>Welcome to AI Agent Hub</h3>
              <p>Click <strong>+ New Chat</strong> to begin a conversation.</p>
            </div>
          )}
          {activeSession && messages.length === 0 && (
            <div className="empty-state">
              <h3>How can I help you with Oracle EBS today?</h3>
              <p>Type a message below to start the conversation.</p>
            </div>
          )}

          {messages.map((msg, idx) => {
            const isUser = msg.role === 'user';
            const hasId = !!msg.id;
            const isCorrectionActive = activeCorrectionId === msg.id;

            return (
              <div key={idx} className={`message-wrapper ${isUser ? 'user' : 'assistant'}`}>
                <div className="message-bubble-container">
                  <div className="message-bubble glass">
                    {isUser
                      ? msg.content
                      : <div className="markdown-body"><ReactMarkdown>{msg.content}</ReactMarkdown></div>
                    }
                    
                    {/* Metadata & feedback — only shown for saved assistant messages */}
                    {!isUser && hasId && (
                      <div className="message-meta">
                        <div className="meta-left">
                          {msg.agent_run_id && (
                            <span className="agent-tag">⚡ Agent Run #{msg.agent_run_id}</span>
                          )}
                          {msg.rlaif_rating !== null && msg.rlaif_rating !== undefined && (
                            <span
                              className={`rlaif-badge ${msg.rlaif_rating >= 0 ? 'verified' : 'flagged'}`}
                              title={msg.rlaif_critique || ''}
                            >
                              {msg.rlaif_rating >= 0 ? '🛡️ AI Grounding Verified' : '⚠️ AI Audit Warning'}
                            </span>
                          )}
                        </div>
                        <div className="feedback-controls">
                          <button
                            className={`feedback-btn thumbs-up ${msg.feedback_rating === 1 ? 'active' : ''}`}
                            onClick={() => handleFeedback(msg.id, 1)}
                            title="Correct & Helpful response"
                          >
                            👍
                          </button>
                          <button
                            className={`feedback-btn thumbs-down ${msg.feedback_rating === -1 ? 'active' : ''}`}
                            onClick={() => {
                              if (msg.feedback_rating === -1 && !msg.feedback_correction) {
                                handleFeedback(msg.id, 0);
                              } else {
                                setActiveCorrectionId(msg.id);
                                setCorrectionText(msg.feedback_correction || '');
                              }
                            }}
                            title="Incorrect or Hallucinated response"
                          >
                            👎
                          </button>
                        </div>
                      </div>
                    )}
                  </div>

                  {/* RLAIF Automated AI Critique Report Panel */}
                  {!isUser && msg.rlaif_rating === -1 && msg.rlaif_critique && (
                    <div className="rlaif-critique-panel glass slide-down">
                      <div className="critique-title">🤖 AI QA Auditor Report</div>
                      <p className="critique-reason">{msg.rlaif_critique}</p>
                      {msg.rlaif_correction && (
                        <div className="rlaif-correction-block">
                          <div className="correction-subheader">💡 RLAIF AI-Proposed Correction:</div>
                          <div className="correction-content-box">{msg.rlaif_correction}</div>
                        </div>
                      )}
                    </div>
                  )}

                  {/* Downvote Corrective Feedback Input */}
                  {!isUser && isCorrectionActive && (
                    <div className="correction-container glass slide-down">
                      <h4>🛠 Help Align the Model</h4>
                      <p>Provide the expected correct answer / Oracle EBS script below to build the preference dataset:</p>
                      <textarea
                        value={correctionText}
                        onChange={(e) => setCorrectionText(e.target.value)}
                        placeholder="Type the correct response or query here..."
                        rows={4}
                      />
                      <div className="correction-actions">
                        <button
                          className="btn-outline btn-sm"
                          onClick={() => {
                            setActiveCorrectionId(null);
                            setCorrectionText('');
                          }}
                        >
                          Cancel
                        </button>
                        <button
                          className="btn-primary btn-sm"
                          onClick={() => handleFeedback(msg.id, -1, correctionText)}
                          disabled={!correctionText.trim()}
                        >
                          Save Preferred Response
                        </button>
                      </div>
                    </div>
                  )}

                  {/* Show already saved corrections to confirm feedback loop is working */}
                  {!isUser && msg.feedback_rating === -1 && msg.feedback_correction && !isCorrectionActive && (
                    <div className="saved-correction glass">
                      <span className="correction-label">🎯 Preferred Answer Saved:</span>
                      <p>{msg.feedback_correction}</p>
                      <button
                        className="edit-correction-btn"
                        onClick={() => {
                          setActiveCorrectionId(msg.id);
                          setCorrectionText(msg.feedback_correction);
                        }}
                      >
                        Edit
                      </button>
                    </div>
                  )}
                </div>
              </div>
            );
          })}

          {loading && (
            <div className="message-wrapper assistant">
              <div className="message-bubble glass typing-indicator">
                <span></span><span></span><span></span>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        <div className="chat-input-area glass">
          <form onSubmit={handleSendMessage} className="chat-form">
            <input
              type="text"
              value={inputMessage}
              onChange={(e) => setInputMessage(e.target.value)}
              placeholder={activeSession ? "Ask the AI Agent Hub anything..." : "Select or start a chat to begin..."}
              disabled={!activeSession || loading}
            />
            <button
              type="submit"
              className="btn-primary"
              disabled={!activeSession || loading || !inputMessage.trim()}
            >
              Send
            </button>
          </form>
        </div>
      </main>

      {/* Right Side panel: Deployment Activity feed */}
      <aside className="deployment-history-panel glass">
        <div className="history-header">
          <h3>🚀 EBS Deployment Activity</h3>
        </div>
        <div className="history-list">
          {deployHistory.length === 0 && (
            <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.75rem', padding: '2rem 0' }}>
              No deployments recorded yet.
            </div>
          )}
          {deployHistory.map(run => (
            <div 
              key={run.id} 
              className="history-item"
              onClick={() => {
                setSelectedHistoryRunId(run.id);
                setShowDeployments(true);
              }}
            >
              <div className="history-item-top">
                <span className={`history-instance-badge ${run.target_instance?.toLowerCase() || 'dev'}`}>
                  {run.target_instance || 'DEV'}
                </span>
                <span className={`history-status-pill ${run.status === 'completed' ? 'success' : run.status?.toLowerCase() || 'pending'}`}>
                  {run.status === 'completed' ? 'Success'
                    : run.status === 'failed' ? 'Failed'
                    : run.status === 'cancelled' ? 'Cancelled'
                    : run.status === 'deploying' ? 'Running'
                    : run.status === 'extracting' ? 'Parsing'
                    : run.status === 'downloading' ? 'Syncing'
                    : run.status || 'Pending'}
                </span>
              </div>
              <div className="history-item-body" title={run.source_doc_name}>
                {run.source_doc_name || run.source_doc_type?.toUpperCase() || 'Deployment'}
              </div>
              <div className="history-item-meta">
                <div className="history-user">
                  <span>👤</span>
                  <span>{user.username || 'Nagendra'}</span>
                </div>
                <span>{new Date(run.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
              </div>
            </div>
          ))}
        </div>
      </aside>

      {showKnowledgeBase && (
        <RagUpload onClose={() => { setShowKnowledgeBase(false); setActiveAgent('diagnostic'); }} />
      )}
      {showPerformance && (
        <PerformanceAgent onClose={() => { setShowPerformance(false); setActiveAgent('diagnostic'); }} />
      )}
      {showDeployments && (
        <DeploymentCenter 
          onClose={() => { setShowDeployments(false); setSelectedHistoryRunId(null); setActiveAgent('diagnostic'); }} 
          preselectedRunId={selectedHistoryRunId}
        />
      )}

      {showSettingsModal && (
        <div className="settings-modal-overlay">
          <div className="settings-modal-container settings-modal-wide">
            <div className="settings-modal-header">
              <h3>AI Agent Hub Settings</h3>
              <button className="settings-modal-close" onClick={() => setShowSettingsModal(false)}>×</button>
            </div>

            <div className="settings-tabs-wrapper">
              <button
                className={`settings-tab-btn ${settingsTab === 'servers' ? 'active' : ''}`}
                onClick={() => { setIsAddingSrv(false); setSettingsTab('servers'); }}
              >
                🖥️ SSH Servers
              </button>
              <button
                className={`settings-tab-btn ${settingsTab === 'environments' ? 'active' : ''}`}
                onClick={() => { setIsAddingEnv(false); setSettingsTab('environments'); }}
              >
                🌍 Environments
              </button>
              <button
                className={`settings-tab-btn ${settingsTab === 'llm' ? 'active' : ''}`}
                onClick={() => setSettingsTab('llm')}
              >
                🤖 AI Model
              </button>
            </div>

            <div className="settings-modal-body">

              {/* ── Tab 1: SSH Server Connections ─────────────────── */}
              {settingsTab === 'servers' && (
                <div className="settings-tab-content">
                  <div className="stab-list-header">
                    <span className="stab-list-title">SSH / Application Server Profiles</span>
                    {!isAddingSrv && (
                      <button className="btn-primary stab-add-btn"
                        onClick={() => { setEditingSrvIndex(null); resetSrvForm(); setIsAddingSrv(true); }}>
                        ➕ Add Server
                      </button>
                    )}
                  </div>

                  {isAddingSrv && (
                    <form onSubmit={handleSaveServer} className="stab-editor-form">
                      <p className="stab-form-title">
                        {editingSrvIndex !== null ? '✏️ Edit Server Connection' : '➕ New Server Connection'}
                      </p>
                      <div className="stab-form-grid">
                        <div className="settings-form-group">
                          <label>Connection Name</label>
                          <input type="text" value={srvFormName} onChange={e => setSrvFormName(e.target.value)}
                            placeholder="e.g. DEV Application Node" required />
                        </div>
                        <div className="settings-form-group">
                          <label>Hostname / IP Address</label>
                          <input type="text" value={srvFormHostname} onChange={e => setSrvFormHostname(e.target.value)}
                            placeholder="e.g. dev-ebs.corp.local" required />
                        </div>
                        <div className="settings-form-group">
                          <label>SSH Port</label>
                          <input type="number" value={srvFormPort} onChange={e => setSrvFormPort(e.target.value)}
                            placeholder="22" required />
                        </div>
                        <div className="settings-form-group">
                          <label>SSH Username</label>
                          <input type="text" value={srvFormUsername} onChange={e => setSrvFormUsername(e.target.value)}
                            placeholder="e.g. applmgr" required />
                        </div>
                        <div className="settings-form-group" style={{ gridColumn: 'span 2' }}>
                          <label>SSH Password / Passphrase</label>
                          <input type="password" value={srvFormPassword} onChange={e => setSrvFormPassword(e.target.value)}
                            placeholder="••••••••••••" />
                        </div>

                        {/* Server Type */}
                        <div className="settings-form-group" style={{ gridColumn: 'span 2' }}>
                          <label>Server Type</label>
                          <div className="srv-type-radio-group">
                            <label className="srv-type-radio">
                              <input type="radio" name="srv_type" value="application"
                                checked={srvFormType === 'application'}
                                onChange={() => setSrvFormType('application')} />
                              <span className="srv-type-label">
                                <span className="srv-type-icon">⚙️</span>
                                <span>
                                  <strong>Application Server</strong>
                                  <small>Web (OHS), Forms, Concurrent Manager</small>
                                </span>
                              </span>
                            </label>
                            <label className="srv-type-radio">
                              <input type="radio" name="srv_type" value="database"
                                checked={srvFormType === 'database'}
                                onChange={() => setSrvFormType('database')} />
                              <span className="srv-type-label">
                                <span className="srv-type-icon">🗄️</span>
                                <span>
                                  <strong>Database Server</strong>
                                  <small>Oracle DB node (sqlplus, RMAN)</small>
                                </span>
                              </span>
                            </label>
                          </div>
                        </div>

                        {/* App services checkboxes — only when Application */}
                        {srvFormType === 'application' && (
                          <div className="settings-form-group" style={{ gridColumn: 'span 2' }}>
                            <label>Application Services on This Node</label>
                            <div className="srv-services-group">
                              <label className="srv-service-check">
                                <input type="checkbox"
                                  checked={Object.values(srvFormServices).every(Boolean)}
                                  onChange={e => setSrvFormServices({ web: e.target.checked, forms: e.target.checked, concurrent: e.target.checked })} />
                                All Services
                              </label>
                              <label className="srv-service-check">
                                <input type="checkbox" checked={srvFormServices.web}
                                  onChange={e => setSrvFormServices(p => ({ ...p, web: e.target.checked }))} />
                                Web (OHS / Apache)
                              </label>
                              <label className="srv-service-check">
                                <input type="checkbox" checked={srvFormServices.forms}
                                  onChange={e => setSrvFormServices(p => ({ ...p, forms: e.target.checked }))} />
                                Oracle Forms
                              </label>
                              <label className="srv-service-check">
                                <input type="checkbox" checked={srvFormServices.concurrent}
                                  onChange={e => setSrvFormServices(p => ({ ...p, concurrent: e.target.checked }))} />
                                Concurrent Manager
                              </label>
                            </div>
                          </div>
                        )}
                      </div>

                      <div className="stab-form-actions">
                        <button type="button" className="btn-outline stab-sm-btn"
                          onClick={() => { setIsAddingSrv(false); setEditingSrvIndex(null); resetSrvForm(); }}>
                          Cancel
                        </button>
                        <button type="submit" className="btn-primary stab-sm-btn">
                          Save Connection
                        </button>
                      </div>
                    </form>
                  )}

                  <div className="stab-list">
                    {serverConnections.length === 0
                      ? <p className="stab-empty">No SSH servers configured yet.</p>
                      : serverConnections.map((s, i) => (
                        <div key={s.name} className="stab-list-item">
                          <div className="stab-item-left">
                            <div className="stab-item-name">
                              <strong>{s.name}</strong>
                              <span className={`stab-type-badge ${s.server_type}`}>
                                {s.server_type === 'application' ? '⚙️ Application' : '🗄️ Database'}
                              </span>
                            </div>
                            <div className="stab-item-detail">
                              {s.username}@{s.hostname}:{s.port}
                              {s.server_type === 'application' && s.app_services?.length > 0 && (
                                <span className="stab-services-tag">
                                  {s.app_services.length === 3 ? 'All Services' : s.app_services.map(k =>
                                    k === 'web' ? 'Web' : k === 'forms' ? 'Forms' : 'Concurrent'
                                  ).join(' · ')}
                                </span>
                              )}
                            </div>
                          </div>
                          <div className="stab-item-actions">
                            <button className="btn-outline stab-sm-btn" onClick={() => handleEditServer(i)}>✏️ Edit</button>
                            <button className="btn-outline stab-sm-btn stab-del-btn" onClick={() => handleDeleteServer(i)}>🗑️</button>
                          </div>
                        </div>
                      ))
                    }
                  </div>
                </div>
              )}

              {/* ── Tab 2: Environments & Database ───────────────── */}
              {settingsTab === 'environments' && (
                <div className="settings-tab-content">
                  <div className="stab-list-header">
                    <span className="stab-list-title">Oracle EBS Environments & DB Credentials</span>
                    {!isAddingEnv && (
                      <button className="btn-primary stab-add-btn"
                        onClick={() => { setEditingEnvIndex(null); resetEnvForm(); setIsAddingEnv(true); }}>
                        ➕ Add Environment
                      </button>
                    )}
                  </div>

                  {isAddingEnv && (
                    <form onSubmit={handleSaveEnv} className="stab-editor-form">
                      <p className="stab-form-title">
                        {editingEnvIndex !== null ? '✏️ Edit Environment' : '➕ New Environment'}
                      </p>
                      <div className="stab-form-grid">
                        <div className="settings-form-group" style={{ gridColumn: 'span 2' }}>
                          <label>Environment Name</label>
                          <input type="text" value={envFormName} onChange={e => setEnvFormName(e.target.value)}
                            placeholder="e.g. DEV, UAT, PROD" required />
                        </div>
                        <div className="settings-form-group">
                          <label>Oracle DB Hostname</label>
                          <input type="text" value={envFormDbHost} onChange={e => setEnvFormDbHost(e.target.value)}
                            placeholder="e.g. dev-db.corp.local" required />
                        </div>
                        <div className="settings-form-group">
                          <label>DB Port</label>
                          <input type="number" value={envFormDbPort} onChange={e => setEnvFormDbPort(e.target.value)}
                            placeholder="1521" required />
                        </div>
                        <div className="settings-form-group">
                          <label>SID / Service Name</label>
                          <input type="text" value={envFormDbSid} onChange={e => setEnvFormDbSid(e.target.value)}
                            placeholder="e.g. EBSDEV" required />
                        </div>
                        <div className="settings-form-group">
                          <label>DB Schema Username</label>
                          <input type="text" value={envFormDbUser} onChange={e => setEnvFormDbUser(e.target.value)}
                            placeholder="e.g. apps" required />
                        </div>
                        <div className="settings-form-group" style={{ gridColumn: 'span 2' }}>
                          <label>DB Schema Password</label>
                          <input type="password" value={envFormDbPassword} onChange={e => setEnvFormDbPassword(e.target.value)}
                            placeholder="••••••••••••" />
                        </div>
                      </div>
                      <div className="stab-form-actions">
                        <button type="button" className="btn-outline stab-sm-btn"
                          onClick={() => { setIsAddingEnv(false); setEditingEnvIndex(null); resetEnvForm(); }}>
                          Cancel
                        </button>
                        <button type="submit" className="btn-primary stab-sm-btn">
                          Save Environment
                        </button>
                      </div>
                    </form>
                  )}

                  <div className="stab-list">
                    {environments.length === 0
                      ? <p className="stab-empty">No environments configured yet.</p>
                      : environments.map((env, i) => (
                        <div key={env.name} className="stab-list-item">
                          <div className="stab-item-left">
                            <div className="stab-item-name">
                              <strong>{env.name}</strong>
                              <span className="stab-type-badge env">🌍 EBS Env</span>
                            </div>
                            <div className="stab-item-detail">
                              {env.db_user}@{env.db_host}:{env.db_port}
                              {env.db_sid && <span className="stab-services-tag">SID: {env.db_sid}</span>}
                            </div>
                          </div>
                          <div className="stab-item-actions">
                            <button className="btn-outline stab-sm-btn" onClick={() => handleEditEnv(i)}>✏️ Edit</button>
                            <button className="btn-outline stab-sm-btn stab-del-btn" onClick={() => handleDeleteEnv(i)}>🗑️</button>
                          </div>
                        </div>
                      ))
                    }
                  </div>
                </div>
              )}

              {/* ── Tab 3: AI Model / LLM Provider ──────────────── */}
              {settingsTab === 'llm' && (
                <div className="settings-tab-content">
                  <div className="stab-list-header">
                    <span className="stab-list-title">AI Inference Provider</span>
                  </div>

                  <form onSubmit={handleSaveLLMConfig} className="stab-editor-form">
                    {/* Provider picker */}
                    <div className="settings-form-group" style={{ gridColumn: 'span 2' }}>
                      <label>Provider</label>
                      <div className="srv-type-radio-group">
                        <label className="srv-type-radio">
                          <input type="radio" name="llm_provider" value="ollama"
                            checked={llmProvider === 'ollama'}
                            onChange={() => { setLlmProvider('ollama'); setLlmModel(''); }} />
                          <span className="srv-type-label">
                            <span className="srv-type-icon">🦙</span>
                            <span>
                              <strong>Ollama (Local)</strong>
                              <small>Self-hosted — no API key needed</small>
                            </span>
                          </span>
                        </label>
                        <label className="srv-type-radio">
                          <input type="radio" name="llm_provider" value="openai"
                            checked={llmProvider === 'openai'}
                            onChange={() => { setLlmProvider('openai'); setLlmModel(''); }} />
                          <span className="srv-type-label">
                            <span className="srv-type-icon">🤖</span>
                            <span>
                              <strong>OpenAI (ChatGPT)</strong>
                              <small>GPT-4o, GPT-4o-mini, GPT-4</small>
                            </span>
                          </span>
                        </label>
                        <label className="srv-type-radio">
                          <input type="radio" name="llm_provider" value="anthropic"
                            checked={llmProvider === 'anthropic'}
                            onChange={() => { setLlmProvider('anthropic'); setLlmModel(''); }} />
                          <span className="srv-type-label">
                            <span className="srv-type-icon">🧠</span>
                            <span>
                              <strong>Anthropic (Claude)</strong>
                              <small>Claude Sonnet, Haiku, Opus</small>
                            </span>
                          </span>
                        </label>
                      </div>
                    </div>

                    <div className="stab-form-grid">
                      {/* Model name */}
                      <div className="settings-form-group" style={{ gridColumn: llmProvider === 'ollama' ? '1' : 'span 2' }}>
                        <label>Model Name</label>
                        <input
                          type="text"
                          value={llmModel}
                          onChange={e => setLlmModel(e.target.value)}
                          placeholder={LLM_DEFAULTS[llmProvider]}
                        />
                      </div>

                      {/* Base URL — Ollama only */}
                      {llmProvider === 'ollama' && (
                        <div className="settings-form-group">
                          <label>Ollama Base URL</label>
                          <input
                            type="text"
                            value={llmBaseUrl}
                            onChange={e => setLlmBaseUrl(e.target.value)}
                            placeholder="http://localhost:11434"
                          />
                        </div>
                      )}

                      {/* API Key — cloud providers */}
                      {llmProvider !== 'ollama' && (
                        <div className="settings-form-group" style={{ gridColumn: 'span 2' }}>
                          <label>API Key</label>
                          <input
                            type="password"
                            value={llmApiKey}
                            onChange={e => setLlmApiKey(e.target.value)}
                            placeholder={llmProvider === 'openai' ? 'sk-...' : 'sk-ant-...'}
                          />
                        </div>
                      )}
                    </div>

                    {/* Current active config summary */}
                    <div className="llm-active-badge">
                      <span className="llm-active-label">Active:</span>
                      <span className="llm-active-value">
                        {(() => { try { const c = JSON.parse(localStorage.getItem('llm_config') || '{}'); return c.provider ? `${c.provider} / ${c.model}` : 'ollama / llama3.2:1b (default)'; } catch { return 'ollama / llama3.2:1b (default)'; } })()}
                      </span>
                    </div>

                    <div className="stab-form-actions">
                      <button type="submit" className="btn-primary stab-sm-btn">
                        Save & Apply
                      </button>
                    </div>
                  </form>
                </div>
              )}
            </div>

            <div className="settings-modal-footer">
              <button className="btn-primary" onClick={() => setShowSettingsModal(false)}>Done</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
