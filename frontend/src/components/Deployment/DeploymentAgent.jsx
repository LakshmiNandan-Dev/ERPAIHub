import { useState, useRef, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import api from '../../api';
import {
  Bot, Send, RotateCcw, ChevronDown, ChevronRight,
  CheckCircle2, Server, Globe, GitBranch, FileCode2, Loader2, Link, KeyRound
} from 'lucide-react';

const WELCOME_MSG = {
  role: 'agent',
  type: 'question',
  content: "Hello! I'm your Oracle EBS Deployment Agent. I'll guide you step-by-step through the deployment process.\n\nWhat would you like to deploy today?\n- Paste your deployment notes or script list directly\n- Share a **Confluence page URL** and I'll fetch the instructions automatically\n- Or just describe the files and packages",
  field: 'instructions',
};

const CONTEXT_FIELDS = [
  { key: 'instructions',    label: 'Instructions', icon: FileCode2,   secret: false },
  { key: 'confluence_url',  label: 'Confluence',   icon: Link,         secret: false },
  { key: 'environment',     label: 'Environment',  icon: Globe,        secret: false },
  { key: 'server_name',     label: 'SSH Server',   icon: Server,       secret: false },
  { key: 'source_type',     label: 'Source',       icon: GitBranch,    secret: false },
  { key: 'git_token',       label: 'Git Token',    icon: KeyRound,     secret: true  },
  { key: 'steps',           label: 'Steps',        icon: CheckCircle2, secret: false },
];

export default function DeploymentAgent({ serverConnections, environments, onDeploymentTriggered }) {
  const [messages, setMessages]         = useState([WELCOME_MSG]);
  const [agentHistory, setAgentHistory] = useState([]);
  const [agentContext, setAgentContext] = useState({
    instructions: null, confluence_url: null, confluence_token: null,
    environment: null, server_name: null, server_index: null,
    source_type: null, git_url: null, git_branch: null, git_token: null,
    steps: null, confirmed: null,
  });
  const [currentField, setCurrentField] = useState('instructions');
  const [inputValue, setInputValue]     = useState('');
  const [isLoading, setIsLoading]       = useState(false);
  const [collapsedThoughts, setCollapsedThoughts] = useState({});

  const messagesEndRef = useRef(null);
  const inputRef       = useRef(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // ── Context update from user answer ──────────────────────────────────────────
  const updateContext = (field, userText, updatedHistory, updatedContext) => {
    const ctx = { ...updatedContext };

    if (field === 'instructions') {
      // Detect Confluence URL pasted as instructions
      if (/atlassian\.net|\/confluence\//i.test(userText)) {
        ctx.confluence_url = userText.trim();
        // Don't set instructions yet — agent will fetch the page
      } else {
        ctx.instructions = userText;
      }
    } else if (field === 'confluence_url') {
      ctx.confluence_url = userText.trim();
    } else if (field === 'confluence_token') {
      ctx.confluence_token = userText.trim();
    } else if (field === 'git_token') {
      const v = userText.trim().toLowerCase();
      ctx.git_token = (v === 'none' || v === 'public' || v === 'no' || v === '') ? '' : userText.trim();
    } else if (field === 'environment') {
      const envs = ['DEV', 'UAT', 'UAT2', 'PROD'];
      const match = envs.find(e => userText.toUpperCase().includes(e));
      ctx.environment = match || userText.toUpperCase().trim();
    } else if (field === 'server') {
      const idx = parseInt(userText.trim()) - 1;
      if (!isNaN(idx) && serverConnections[idx]) {
        ctx.server_index = idx;
        ctx.server_name  = serverConnections[idx].name;
      } else {
        // Try matching by name
        const byName = serverConnections.findIndex(s =>
          s.name.toLowerCase().includes(userText.toLowerCase())
        );
        if (byName >= 0) {
          ctx.server_index = byName;
          ctx.server_name  = serverConnections[byName].name;
        } else {
          ctx.server_name = userText.trim();
        }
      }
    } else if (field === 'source_type') {
      ctx.source_type = userText.toLowerCase().includes('git') ? 'git' : 'local';
    } else if (field === 'git_url') {
      ctx.git_url = userText.trim();
    } else if (field === 'git_branch') {
      ctx.git_branch = userText.trim();
    } else if (field === 'confirmation') {
      ctx.confirmed = /^(yes|y|ok|proceed|confirm|sure)/i.test(userText.trim());
    }

    return ctx;
  };

  // ── Send message to agent ─────────────────────────────────────────────────────
  const sendMessage = async (text) => {
    if (!text.trim() || isLoading) return;

    // Add user bubble
    const userMsg = { role: 'user', type: 'user', content: text };
    setMessages(prev => [...prev, userMsg]);
    setInputValue('');
    setIsLoading(true);

    // Snapshot current state before async work
    const historySnapshot = [...agentHistory];
    const contextSnapshot = { ...agentContext };

    // Update context based on what field the agent was collecting
    const updatedCtx = updateContext(currentField, text, historySnapshot, contextSnapshot);
    setAgentContext(updatedCtx);

    try {
      const token = localStorage.getItem('session_token');
      const llm   = (() => { try { return JSON.parse(localStorage.getItem('llm_config') || '{}'); } catch { return {}; } })();

      const resp = await fetch(`${api.defaults.baseURL}/deployments/agent/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`,
          ...(llm.provider  && { 'X-LLM-Provider': llm.provider }),
          ...(llm.model     && { 'X-LLM-Model':    llm.model }),
          ...(llm.base_url  && { 'X-LLM-Base-Url': llm.base_url }),
        },
        body: JSON.stringify({
          history:               historySnapshot,
          user_message:          text,
          context:               updatedCtx,
          available_servers:     serverConnections.map(s => ({ ...s, password: undefined })),
          available_environments: environments,
        }),
      });

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      const reader  = resp.body.getReader();
      const decoder = new TextDecoder();
      let   buffer  = '';
      let   newHistory = [...historySnapshot];
      let   newCtx     = { ...updatedCtx };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const raw = line.slice(6).trim();
          if (raw === '[DONE]') break;

          let msg;
          try { msg = JSON.parse(raw); } catch { continue; }

          switch (msg.type) {
            case 'thought':
              setMessages(prev => [...prev, {
                role: 'agent', type: 'thought',
                content: msg.content,
                id: Date.now() + Math.random(),
              }]);
              break;

            case 'observation':
              // Internal tool result — show subtly
              setMessages(prev => [...prev, {
                role: 'agent', type: 'observation', content: msg.content,
              }]);
              break;

            case 'question':
              setMessages(prev => [...prev, {
                role: 'agent', type: 'question', content: msg.content,
              }]);
              setCurrentField(msg.field || '');
              break;

            case 'plan':
              setMessages(prev => [...prev, {
                role: 'agent', type: 'plan', content: msg.content, steps: msg.steps,
              }]);
              setCurrentField('confirmation');
              if (msg.steps) newCtx.steps = msg.steps;
              break;

            case 'needs_input':
              if (msg.history) newHistory = msg.history;
              if (msg.context) {
                // Merge backend context updates (e.g. extracted steps)
                newCtx = { ...newCtx, ...msg.context };
              }
              break;

            case 'trigger_deployment':
              await triggerActualDeployment(msg.context || newCtx);
              newHistory = msg.history || newHistory;
              break;

            case 'finish':
              setMessages(prev => [...prev, {
                role: 'agent', type: 'finish', content: msg.content,
              }]);
              break;

            case 'error':
              setMessages(prev => [...prev, {
                role: 'agent', type: 'error', content: msg.content,
              }]);
              break;

            default:
              break;
          }
        }
      }

      setAgentHistory(newHistory);
      setAgentContext(newCtx);
    } catch (err) {
      console.error('Agent error', err);
      setMessages(prev => [...prev, {
        role: 'agent', type: 'error',
        content: `Connection error: ${err.message}. Make sure the server is running.`,
      }]);
    } finally {
      setIsLoading(false);
      inputRef.current?.focus();
    }
  };

  // ── Trigger the actual deployment via existing API ────────────────────────────
  const triggerActualDeployment = async (ctx) => {
    const server = ctx.server_index != null ? serverConnections[ctx.server_index] : null;
    // Look up stored DB credentials for the selected environment
    const envProfile = environments.find(e => e.name === ctx.environment) || {};

    const body = {
      source_doc_type: ctx.confluence_url ? 'confluence' : 'chat',
      source_doc_name: ctx.confluence_url || 'Agent Deployment',
      source_content:  ctx.instructions || '',
      target_instance: ctx.environment  || 'DEV',
      git_repo_url:    ctx.source_type === 'git' ? ctx.git_url    || null : null,
      git_branch:      ctx.source_type === 'git' ? ctx.git_branch || 'main' : null,
      git_token:       ctx.source_type === 'git' ? ctx.git_token  || null : null,
      // Reference admin-managed resources; credentials resolved & decrypted server-side
      environment_id:  envProfile.id || null,
      ssh_server_id:   server?.id || null,
    };

    try {
      const res = await api.post('/deployments/', body);
      setMessages(prev => [...prev, {
        role: 'agent', type: 'finish',
        content: `Deployment launched successfully (Run #${res.data.id}). Switching to the terminal view...`,
      }]);
      setTimeout(() => onDeploymentTriggered(res.data.id), 1200);
    } catch (err) {
      setMessages(prev => [...prev, {
        role: 'agent', type: 'error',
        content: `Failed to create deployment: ${err?.response?.data?.detail || err.message}`,
      }]);
    }
  };

  // ── Reset ─────────────────────────────────────────────────────────────────────
  const resetSession = () => {
    setMessages([WELCOME_MSG]);
    setAgentHistory([]);
    setAgentContext({
      instructions: null, confluence_url: null, confluence_token: null,
      environment: null, server_name: null, server_index: null,
      source_type: null, git_url: null, git_branch: null, git_token: null,
      steps: null, confirmed: null,
    });
    setCurrentField('instructions');
    setInputValue('');
    setIsLoading(false);
  };

  // ── Render helpers ────────────────────────────────────────────────────────────
  const toggleThought = (id) =>
    setCollapsedThoughts(prev => ({ ...prev, [id]: !prev[id] }));

  const contextComplete = Object.values(agentContext).filter(Boolean).length;

  return (
    <div className="da-root">
      {/* Left: conversation */}
      <div className="da-chat-panel">
        {/* Header */}
        <div className="da-header">
          <div className="da-header-left">
            <Bot size={18} className="da-header-icon" />
            <div>
              <div className="da-header-title">EBS Deployment Agent</div>
              <div className="da-header-sub">ReAct · Reasoning + Acting</div>
            </div>
          </div>
          <button className="da-reset-btn" onClick={resetSession} title="Start a new session">
            <RotateCcw size={14} />
            New Session
          </button>
        </div>

        {/* Messages */}
        <div className="da-messages">
          {messages.map((msg, idx) => {
            if (msg.type === 'thought') {
              const collapsed = collapsedThoughts[msg.id] === true; // default expanded
              return (
                <div key={idx} className="da-thought-wrap">
                  <button className="da-thought-toggle" onClick={() => toggleThought(msg.id)}>
                    {collapsed ? <ChevronRight size={11} /> : <ChevronDown size={11} />}
                    Agent Reasoning
                  </button>
                  {!collapsed && (
                    <div className="da-thought-body">{msg.content}</div>
                  )}
                </div>
              );
            }

            if (msg.type === 'observation') {
              return (
                <div key={idx} className="da-observation">
                  <span className="da-obs-label">Tool Result</span>
                  <pre className="da-obs-body">{msg.content}</pre>
                </div>
              );
            }

            if (msg.type === 'user') {
              return (
                <div key={idx} className="da-bubble-row user">
                  <div className="da-bubble user">{msg.content}</div>
                </div>
              );
            }

            if (msg.type === 'plan') {
              return (
                <div key={idx} className="da-bubble-row agent">
                  <div className="da-plan-bubble">
                    <div className="da-plan-icon">📋</div>
                    <div className="da-plan-body">
                      <ReactMarkdown>{msg.content}</ReactMarkdown>
                    </div>
                  </div>
                </div>
              );
            }

            if (msg.type === 'finish') {
              return (
                <div key={idx} className="da-bubble-row agent">
                  <div className="da-finish-bubble">
                    <CheckCircle2 size={16} className="da-finish-icon" />
                    <span>{msg.content}</span>
                  </div>
                </div>
              );
            }

            if (msg.type === 'error') {
              return (
                <div key={idx} className="da-bubble-row agent">
                  <div className="da-error-bubble">{msg.content}</div>
                </div>
              );
            }

            // Default: 'question' and any agent text
            return (
              <div key={idx} className="da-bubble-row agent">
                <div className="da-agent-avatar"><Bot size={13} /></div>
                <div className="da-bubble agent">
                  <ReactMarkdown>{msg.content}</ReactMarkdown>
                </div>
              </div>
            );
          })}

          {isLoading && (
            <div className="da-bubble-row agent">
              <div className="da-agent-avatar"><Bot size={13} /></div>
              <div className="da-bubble agent da-thinking">
                <Loader2 size={13} className="da-spin" />
                <span>Reasoning...</span>
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="da-input-row">
          <input
            ref={inputRef}
            className="da-input"
            value={inputValue}
            onChange={e => setInputValue(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(inputValue); } }}
            placeholder={isLoading ? 'Agent is thinking...' : 'Type your answer...'}
            disabled={isLoading}
          />
          <button
            className="da-send-btn"
            onClick={() => sendMessage(inputValue)}
            disabled={isLoading || !inputValue.trim()}
          >
            <Send size={14} />
          </button>
        </div>
      </div>

      {/* Right: context tracker */}
      <div className="da-context-panel">
        <div className="da-ctx-header">Collected Context</div>
        <div className="da-ctx-progress">
          <div
            className="da-ctx-fill"
            style={{ width: `${Math.min(100, (contextComplete / CONTEXT_FIELDS.length) * 100)}%` }}
          />
        </div>

        <div className="da-ctx-items">
          {CONTEXT_FIELDS.map(({ key, label, icon: Icon, secret }) => {
            const val = agentContext[key];
            // git_token: null=not asked, ''=public repo, string=token set
            const isDone = key === 'git_token'
              ? val !== null
              : (val != null && val !== '');
            // Only show git_token row when source_type is git
            if (key === 'git_token' && agentContext.source_type !== 'git') return null;
            // Only show confluence_url row when it's been set
            if (key === 'confluence_url' && !agentContext.confluence_url) return null;

            let displayVal;
            if (key === 'steps') {
              displayVal = Array.isArray(val) ? `${val.length} step${val.length !== 1 ? 's' : ''}` : null;
            } else if (secret && isDone) {
              displayVal = '••••••••';
            } else if (key === 'git_token' && val === '') {
              displayVal = 'public repo';
            } else if (key === 'confluence_url' && val) {
              // Trim long URLs
              displayVal = val.length > 40 ? val.slice(0, 40) + '…' : val;
            } else {
              displayVal = val;
            }

            return (
              <div key={key} className={`da-ctx-item ${isDone ? 'done' : 'pending'}`}>
                <Icon size={13} className="da-ctx-icon" />
                <div className="da-ctx-text">
                  <div className="da-ctx-label">{label}</div>
                  {isDone
                    ? <div className="da-ctx-value">{String(displayVal)}</div>
                    : <div className="da-ctx-empty">Not collected yet</div>
                  }
                </div>
                <div className={`da-ctx-dot ${isDone ? 'done' : ''}`} />
              </div>
            );
          })}
        </div>

        {/* Step breakdown */}
        {agentContext.steps?.length > 0 && (
          <div className="da-ctx-steps">
            <div className="da-ctx-steps-title">Planned Steps</div>
            {agentContext.steps.map((s, i) => (
              <div key={i} className="da-ctx-step">
                <span className="da-ctx-step-num">{i + 1}</span>
                <div>
                  <div className="da-ctx-step-file">{s.file_name}</div>
                  <div className="da-ctx-step-type">{s.execution_type}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
