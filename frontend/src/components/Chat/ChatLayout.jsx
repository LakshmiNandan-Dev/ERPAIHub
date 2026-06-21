import { useState, useEffect, useRef } from 'react';
import api from '../../api';
import './ChatLayout.css';
import RagUpload from '../Rag/RagUpload';
import DeploymentCenter from '../Deployment/DeploymentCenter';
import PerformanceAgent from '../Performance/PerformanceAgent';
import AdminConsole from '../Admin/AdminConsole';
import MonitoringConsole from '../Monitoring/MonitoringConsole';
import CloneCenter from '../Cloning/CloneCenter';
import PatchCenter from '../Patching/PatchCenter';
import HcmAgent from '../Functional/HcmAgent';
import ReactMarkdown from 'react-markdown';
import {
  BrainCircuit, MessageSquarePlus, Trash2, Settings2, LogOut,
  Server, Globe, Bot, Cpu, Pencil, Check, X, ShieldCheck, KeyRound, Activity
} from 'lucide-react';

export default function ChatLayout({ setAuthToken }) {
  const [sessions, setSessions] = useState([]);
  const [activeSession, setActiveSession] = useState(null);
  const [messages, setMessages] = useState([]);
  const [inputMessage, setInputMessage] = useState('');
  const [loading, setLoading] = useState(false);
  const abortRef = useRef(null);
  const activeSessionIdRef = useRef(null);
  const [deletingId, setDeletingId] = useState(null);
  const [renamingId, setRenamingId] = useState(null);
  const [renameValue, setRenameValue] = useState('');
  const renameInputRef = useRef(null);
  const [showKnowledgeBase, setShowKnowledgeBase] = useState(false);
  const [showDeployments, setShowDeployments] = useState(false);
  const [showPerformance, setShowPerformance] = useState(false);
  const [showCloning, setShowCloning] = useState(false);
  const [showPatching, setShowPatching] = useState(false);
  const [showHcm, setShowHcm] = useState(false);
  const [showAdmin, setShowAdmin] = useState(false);
  const [showMonitoring, setShowMonitoring] = useState(false);

  // Change-password modal
  const [showChangePw, setShowChangePw] = useState(false);
  const [pwOld, setPwOld] = useState('');
  const [pwNew, setPwNew] = useState('');
  const [pwConfirm, setPwConfirm] = useState('');
  const [pwError, setPwError] = useState('');
  const [pwSuccess, setPwSuccess] = useState('');
  const [pwSaving, setPwSaving] = useState(false);

  const handleChangePassword = async (e) => {
    e.preventDefault();
    setPwError(''); setPwSuccess('');
    if (pwNew.length < 4) { setPwError('New password must be at least 4 characters.'); return; }
    if (pwNew !== pwConfirm) { setPwError('New passwords do not match.'); return; }
    setPwSaving(true);
    try {
      await api.post('/auth/change-password', { old_password: pwOld, new_password: pwNew });
      setPwSuccess('Password updated successfully.');
      setPwOld(''); setPwNew(''); setPwConfirm('');
    } catch (err) {
      setPwError(err.response?.data?.detail || 'Could not change password.');
    } finally {
      setPwSaving(false);
    }
  };
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
  const [llmBaseUrl, setLlmBaseUrl] = useState(_llmStored.base_url || 'http://localhost:11434');

  // For Ollama, leave the model blank by default so the server-side credential
  // (admin-managed) decides the model — the browser no longer pins a tiny model
  // that would override it. Cloud providers keep a sensible default.
  const LLM_DEFAULTS = {
    ollama: '', openai: 'gpt-4o-mini',
    anthropic: 'claude-haiku-4-5-20251001', gemini: 'gemini-2.0-flash',
  };

  const handleSaveLLMConfig = (e) => {
    e.preventDefault();
    // Only the provider/model selection is stored locally. API keys live
    // server-side (admin-managed, encrypted) and are never kept in the browser.
    const config = {
      provider: llmProvider,
      model: llmModel.trim() || LLM_DEFAULTS[llmProvider],
      base_url: llmBaseUrl.trim() || 'http://localhost:11434',
    };
    localStorage.setItem('llm_config', JSON.stringify(config));
    setLlmModel(config.model);
  };

  // Admin-managed SSH servers & environments (read-only here; no secrets).
  // Created and edited in the Admin Console; consumed via /config/*.
  const [serverConnections, setServerConnections] = useState([]);
  const [environments, setEnvironments] = useState([]);

  const messagesEndRef = useRef(null);
  const userMenuRef = useRef(null);
  const [user, setUser] = useState(() => {
    try { return JSON.parse(localStorage.getItem('user') || '{}'); } catch { return {}; }
  });

  // Role-based access. Agent invocation is granted per-agent by the user's
  // assigned roles (allowed_agents from /auth/getuser); admins get every agent.
  // Chat and the Knowledge Base are open to all. The Admin Console stays on the
  // admin/dba tier.
  const role = String(user.role || (user.is_admin ? 'admin' : 'user')).toLowerCase();
  const allowedAgents = new Set(user.allowed_agents || []);
  // Maps an agent-dropdown value to its backend gated-agent name.
  const AGENT_OF = { deployments: 'deployment', performance: 'performance', cloning: 'cloning', patching: 'patching', hcm: 'hcm' };
  const canAgent = (name) => role === 'admin' || allowedAgents.has(name);
  const canInvokeAgents = role === 'admin' || allowedAgents.size > 0;
  const canAdminConsole = role === 'admin' || role === 'dba';
  const roleLabel = role === 'admin' ? 'Administrator' : role === 'dba' ? 'DBA' : 'Member';

  // Refresh the current user (e.g. is_admin) so the Admin Console appears without re-login.
  useEffect(() => {
    api.get('/auth/getuser')
      .then(res => { setUser(res.data); localStorage.setItem('user', JSON.stringify(res.data)); })
      .catch(() => {});
  }, []);

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

  // Load the admin-managed servers & environments (read-only, no secrets).
  useEffect(() => {
    api.get('/config/servers').then(r => setServerConnections(r.data)).catch(() => {});
    api.get('/config/environments').then(r => setEnvironments(r.data)).catch(() => {});
  }, []);

  const handleAgentChange = (val) => {
    // Gated agents require a role grant; diagnostic chat and the KB stay open.
    const gated = AGENT_OF[val];
    if (gated && !canAgent(gated)) {
      setActiveAgent('diagnostic');
      return;
    }
    setActiveAgent(val);
    if (val === 'deployments') {
      setShowDeployments(true);
    } else if (val === 'kb') {
      setShowKnowledgeBase(true);
    } else if (val === 'performance') {
      setShowPerformance(true);
    } else if (val === 'cloning') {
      setShowCloning(true);
    } else if (val === 'patching') {
      setShowPatching(true);
    } else if (val === 'hcm') {
      setShowHcm(true);
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

  // Track the active session id for async stream guards, and abort an in-flight
  // response when the user leaves its session — so other sessions stay usable
  // (the partial reply is saved server-side and reappears on return).
  useEffect(() => {
    activeSessionIdRef.current = activeSession?.id ?? null;
    return () => {
      if (abortRef.current) {
        abortRef.current.abort();
        setLoading(false);
      }
    };
  }, [activeSession?.id]);

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

  const handleStop = () => {
    // Abort the in-flight stream. The connection drops, so the backend
    // generator is cancelled (Ollama stops generating) and its `finally`
    // persists whatever was produced so far. The partial reply stays on screen.
    if (abortRef.current) abortRef.current.abort();
  };

  const handleSendMessage = async (e) => {
    e.preventDefault();
    if (!inputMessage.trim() || !activeSession) return;

    const streamSessionId = activeSession.id;
    const userMessage = { role: 'user', content: inputMessage };
    setMessages(prev => [...prev, userMessage]);
    const sentContent = inputMessage;
    setInputMessage('');
    setLoading(true);

    // Add an empty assistant bubble that we'll fill token by token
    setMessages(prev => [...prev, { role: 'assistant', content: '' }]);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const token = localStorage.getItem('session_token');
      const response = await fetch(
        `${api.defaults.baseURL}/chat/sessions/${activeSession.id}/stream`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`
          },
          body: JSON.stringify({ content: sentContent }),
          signal: controller.signal
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
            // Append token to the assistant bubble — only while the user is
            // still viewing this stream's session (otherwise it's dropped here
            // and reappears from the DB on return).
            if (activeSessionIdRef.current === streamSessionId) {
              setMessages(prev => {
                const msgs = [...prev];
                msgs[msgs.length - 1] = {
                  ...msgs[msgs.length - 1],
                  content: msgs[msgs.length - 1].content + parsed
                };
                return msgs;
              });
            }
          } catch { /* skip malformed lines */ }
        }
      }

      // Only touch the view if the user is still on this stream's session.
      if (activeSessionIdRef.current === streamSessionId) {
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
      }

    } catch (err) {
      if (err.name === 'AbortError') {
        // Stopped by the user — keep the partial reply already streamed.
        // The backend persisted it; it reconciles on the next message/reload.
      } else {
        console.error('Failed to send message', err);
        if (activeSessionIdRef.current === streamSessionId) {
          setMessages(prev => {
            const msgs = [...prev];
            msgs[msgs.length - 1] = { role: 'assistant', content: '⚠️ Failed to get a response. Please check that Ollama is running.' };
            return msgs;
          });
        }
      }
    } finally {
      abortRef.current = null;
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
                <option value="deployments" disabled={!canAgent('deployment')}>🚀 Code Deployment Agent{canAgent('deployment') ? '' : ' 🔒'}</option>
                <option value="kb">📚 RAG Knowledge Base Agent</option>
                <option value="performance" disabled={!canAgent('performance')}>⚡ Performance Analyzer{canAgent('performance') ? '' : ' 🔒'}</option>
                <option value="cloning" disabled={!canAgent('cloning')}>🧬 EBS Cloning Agent{canAgent('cloning') ? '' : ' 🔒'}</option>
                <option value="patching" disabled={!canAgent('patching')}>🩹 EBS Patching Agent{canAgent('patching') ? '' : ' 🔒'}</option>
                <option value="hcm" disabled={!canAgent('hcm')}>🧑‍💼 HCM &amp; Payroll Agent{canAgent('hcm') ? '' : ' 🔒'}</option>
                <option value="finance" disabled>💸 Cash Management Agent (Soon)</option>
                <option value="purchasing" disabled>🛒 Purchasing PO Agent (Soon)</option>
              </select>
              {!canInvokeAgents && (
                <span className="agent-locked-note" title="Your role grants no agents. Ask an administrator to assign a role.">
                  🔒 Chat & Knowledge Base only — agents require a role grant
                </span>
              )}
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
                  <span className="user-role-badge">{roleLabel}</span>
                </div>
                <div className="dropdown-divider" />
                {canAdminConsole && (
                  <button className="dropdown-item" onClick={() => { setShowUserMenu(false); setShowAdmin(true); }}>
                    <ShieldCheck size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Admin Console
                  </button>
                )}
                {role === 'admin' && (
                  <button className="dropdown-item" onClick={() => { setShowUserMenu(false); setShowMonitoring(true); }}>
                    <Activity size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Monitoring Console
                  </button>
                )}
                <button className="dropdown-item" onClick={() => { setShowUserMenu(false); setSettingsTab('servers'); setShowSettingsModal(true); }}>
                  <Server size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />SSH Server Connections
                </button>
                <button className="dropdown-item" onClick={() => { setShowUserMenu(false); setSettingsTab('environments'); setShowSettingsModal(true); }}>
                  <Globe size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Environments & Database
                </button>
                <button className="dropdown-item" onClick={() => { setShowUserMenu(false); setSettingsTab('llm'); setShowSettingsModal(true); }}>
                  <Cpu size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />AI Model Settings
                </button>
                <button className="dropdown-item" onClick={() => { setShowUserMenu(false); setPwError(''); setPwSuccess(''); setPwOld(''); setPwNew(''); setPwConfirm(''); setShowChangePw(true); }}>
                  <KeyRound size={13} style={{ marginRight: '0.4rem', verticalAlign: 'middle' }} />Change Password
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
            {loading ? (
              <button
                type="button"
                className="btn-primary chat-stop-btn"
                onClick={handleStop}
                title="Stop generating"
                aria-label="Stop generating"
              >
                ■
              </button>
            ) : (
              <button
                type="submit"
                className="btn-primary"
                disabled={!activeSession || !inputMessage.trim()}
              >
                Send
              </button>
            )}
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
      {showCloning && (
        <CloneCenter onClose={() => { setShowCloning(false); setActiveAgent('diagnostic'); }} />
      )}
      {showPatching && (
        <PatchCenter onClose={() => { setShowPatching(false); setActiveAgent('diagnostic'); }} />
      )}
      {showHcm && (
        <HcmAgent onClose={() => { setShowHcm(false); setActiveAgent('diagnostic'); }} />
      )}
      {showAdmin && <AdminConsole onClose={() => setShowAdmin(false)} role={role} currentUser={user} />}
      {showMonitoring && <MonitoringConsole onClose={() => setShowMonitoring(false)} />}

      {showChangePw && (
        <div className="settings-modal-overlay" onMouseDown={() => setShowChangePw(false)}>
          <div className="settings-modal-container" style={{ maxWidth: '420px' }} onMouseDown={e => e.stopPropagation()}>
            <div className="settings-modal-header">
              <h3><KeyRound size={16} style={{ verticalAlign: 'middle', marginRight: 8 }} />Change Password</h3>
              <button className="settings-modal-close" onClick={() => setShowChangePw(false)}>×</button>
            </div>
            <form onSubmit={handleChangePassword} className="stab-editor-form" style={{ padding: '1.2rem' }}>
              {pwError && <div className="auth-alert error" style={{ marginBottom: '0.8rem' }}>{pwError}</div>}
              {pwSuccess && <div className="auth-alert success" style={{ marginBottom: '0.8rem' }}>{pwSuccess}</div>}
              <div className="settings-form-group">
                <label>Current Password</label>
                <input type="password" value={pwOld} onChange={e => setPwOld(e.target.value)}
                  placeholder="••••••••" required autoFocus />
              </div>
              <div className="settings-form-group" style={{ marginTop: '0.7rem' }}>
                <label>New Password</label>
                <input type="password" value={pwNew} onChange={e => setPwNew(e.target.value)}
                  placeholder="At least 4 characters" required />
              </div>
              <div className="settings-form-group" style={{ marginTop: '0.7rem' }}>
                <label>Confirm New Password</label>
                <input type="password" value={pwConfirm} onChange={e => setPwConfirm(e.target.value)}
                  placeholder="Re-enter new password" required />
              </div>
              <div className="stab-form-actions" style={{ marginTop: '1.1rem' }}>
                <button type="button" className="btn-outline stab-sm-btn" onClick={() => setShowChangePw(false)}>Close</button>
                <button type="submit" className="btn-primary stab-sm-btn" disabled={pwSaving}>
                  {pwSaving ? 'Saving…' : 'Update Password'}
                </button>
              </div>
            </form>
          </div>
        </div>
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
                onClick={() => setSettingsTab('servers')}
              >
                🖥️ SSH Servers
              </button>
              <button
                className={`settings-tab-btn ${settingsTab === 'environments' ? 'active' : ''}`}
                onClick={() => setSettingsTab('environments')}
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
                  </div>
                  <p className="stab-managed-note">🔒 Managed centrally by your administrator in the <strong>Admin Console</strong>.</p>

                  <div className="stab-list">
                    {serverConnections.length === 0
                      ? <p className="stab-empty">No SSH servers configured yet.</p>
                      : serverConnections.map((s) => (
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
                  </div>
                  <p className="stab-managed-note">🔒 Managed centrally by your administrator in the <strong>Admin Console</strong>. Database passwords are encrypted server-side.</p>

                  <div className="stab-list">
                    {environments.length === 0
                      ? <p className="stab-empty">No environments configured yet.</p>
                      : environments.map((env) => (
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
                        <label className="srv-type-radio">
                          <input type="radio" name="llm_provider" value="gemini"
                            checked={llmProvider === 'gemini'}
                            onChange={() => { setLlmProvider('gemini'); setLlmModel(''); }} />
                          <span className="srv-type-label">
                            <span className="srv-type-icon">✨</span>
                            <span>
                              <strong>Google (Gemini)</strong>
                              <small>Gemini 2.0 Flash, 1.5 Pro</small>
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

                      {/* API keys for cloud providers are managed centrally by an
                          administrator (encrypted, server-side) — not entered here. */}
                      {llmProvider !== 'ollama' && (
                        <div className="settings-form-group" style={{ gridColumn: 'span 2' }}>
                          <p className="stab-managed-note">
                            🔒 The API key for <strong>{llmProvider}</strong> is configured by your administrator
                            in the <strong>Admin Console</strong> and applied automatically.
                          </p>
                        </div>
                      )}
                    </div>

                    {/* Current active config summary */}
                    <div className="llm-active-badge">
                      <span className="llm-active-label">Active:</span>
                      <span className="llm-active-value">
                        {(() => { try { const c = JSON.parse(localStorage.getItem('llm_config') || '{}'); return c.provider ? `${c.provider} / ${c.model || '(server default)'}` : 'ollama / (server default)'; } catch { return 'ollama / (server default)'; } })()}
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
