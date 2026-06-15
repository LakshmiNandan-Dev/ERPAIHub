import { useState, useEffect, useCallback } from 'react';
import api from '../../api';
import './AdminConsole.css';
import { Users, Server, Globe, Cpu, Plus, Pencil, Trash2, X, ShieldCheck, LogIn, Activity, Link2, ClipboardList, Download, GraduationCap, Upload, Split, KeyRound } from 'lucide-react';

// `roles` lists which roles may see each tab. Admin sees everything; a DBA is
// limited to the governance tabs (user approvals + audit / clone approvals).
const TABS = [
  { key: 'monitoring',   label: 'Monitoring',    icon: Activity,      roles: ['admin'] },
  { key: 'users',        label: 'Users',         icon: Users,         roles: ['admin', 'dba'] },
  { key: 'roles',        label: 'Roles',         icon: KeyRound,      roles: ['admin'] },
  { key: 'servers',      label: 'SSH Servers',   icon: Server,        roles: ['admin'] },
  { key: 'environments', label: 'Environments',  icon: Globe,         roles: ['admin'] },
  { key: 'llm',          label: 'LLM Providers', icon: Cpu,           roles: ['admin'] },
  { key: 'routing',      label: 'Agent Routing', icon: Split,         roles: ['admin'] },
  { key: 'training',     label: 'Training',      icon: GraduationCap, roles: ['admin'] },
  { key: 'integrations', label: 'Integrations',  icon: Link2,         roles: ['admin'] },
  { key: 'sso',          label: 'Authentication', icon: LogIn,        roles: ['admin'] },
  { key: 'audit',        label: 'Audit',         icon: ClipboardList, roles: ['admin', 'dba'] },
];

const PROVIDERS = ['openai', 'anthropic', 'gemini', 'ollama'];

export default function AdminConsole({ onClose, role = 'admin', currentUser = {} }) {
  const tabs = TABS.filter(t => t.roles.includes(role));
  const [tab, setTab] = useState(tabs[0]?.key || 'users');

  return (
    <div className="admin-overlay">
      <div className="admin-container">
        <div className="admin-header">
          <h3><ShieldCheck size={18} style={{ verticalAlign: 'middle', marginRight: 8 }} />Admin Console
            <span className="admin-pill admin" style={{ marginLeft: 10, textTransform: 'uppercase' }}>{role}</span>
          </h3>
          <button className="admin-close" onClick={onClose}>×</button>
        </div>

        <div className="admin-tabs">
          {tabs.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              className={`admin-tab ${tab === key ? 'active' : ''}`}
              onClick={() => setTab(key)}
            >
              <Icon size={14} style={{ marginRight: 6, verticalAlign: 'middle' }} />{label}
            </button>
          ))}
        </div>

        <div className="admin-body">
          {tab === 'monitoring' && <MonitoringTab />}
          {tab === 'users' && <UsersTab />}
          {tab === 'roles' && <RolesTab />}
          {tab === 'servers' && <ServersTab />}
          {tab === 'environments' && <EnvironmentsTab />}
          {tab === 'llm' && <LlmTab />}
          {tab === 'routing' && <RoutingTab />}
          {tab === 'training' && <TrainingTab />}
          {tab === 'integrations' && <IntegrationsTab />}
          {tab === 'sso' && <SsoTab />}
          {tab === 'audit' && <AuditTab currentUser={currentUser} />}
        </div>
      </div>
    </div>
  );
}

const ROLE_LABELS = { admin: 'Administrator', dba: 'DBA', user: 'User (chat only)' };

/* ── shared helpers ─────────────────────────────────────────────────────────── */

function useResource(path) {
  const [rows, setRows] = useState([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const load = useCallback(async () => {
    setLoading(true); setError('');
    try { setRows((await api.get(path)).data); }
    catch (e) { setError(e.response?.data?.detail || e.message); }
    finally { setLoading(false); }
  }, [path]);
  useEffect(() => { load(); }, [load]);
  return { rows, error, loading, reload: load, setError };
}

function Field({ label, children }) {
  return (
    <label className="admin-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function TestResult({ result }) {
  if (!result) return null;
  return <p className={`admin-test ${result.ok ? 'ok' : 'fail'}`}>{result.message}</p>;
}

/* ── Users ──────────────────────────────────────────────────────────────────── */

function UsersTab() {
  const { rows, error, loading, reload, setError } = useResource('/admin/users');
  const [roles, setRoles] = useState([]);     // defined RBAC roles (for assignment)
  const [form, setForm] = useState(null); // null=closed; {} =new; {...}=edit

  useEffect(() => { api.get('/admin/roles').then(r => setRoles(r.data)).catch(() => {}); }, []);

  const blank = { username: '', email: '', password: '', role: 'user', is_active: true, role_ids: [] };

  const toggleRole = (id) => {
    const has = form.role_ids?.includes(id);
    setForm({ ...form, role_ids: has ? form.role_ids.filter(x => x !== id) : [...(form.role_ids || []), id] });
  };

  const save = async (e) => {
    e.preventDefault();
    try {
      if (form.id) {
        const body = { email: form.email, is_active: form.is_active, role: form.role, role_ids: form.role_ids };
        if (form.password) body.password = form.password;
        await api.patch(`/admin/users/${form.id}`, body);
      } else {
        await api.post('/admin/users', {
          username: form.username, email: form.email,
          password: form.password, role: form.role, role_ids: form.role_ids,
        });
      }
      setForm(null); reload();
    } catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const remove = async (id) => {
    if (!window.confirm('Delete this user?')) return;
    try { await api.delete(`/admin/users/${id}`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const approve = async (id) => {
    try { await api.post(`/admin/users/${id}/approve`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
  };
  const reject = async (id) => {
    if (!window.confirm('Reject this sign-up request? The account stays inactive.')) return;
    try { await api.post(`/admin/users/${id}/reject`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const pendingCount = rows.filter(u => u.approval_status === 'pending').length;

  const statusCell = (u) => {
    if (u.approval_status === 'pending') return <span className="admin-pill pend">Pending approval</span>;
    if (u.approval_status === 'rejected') return <span className="admin-pill off">Rejected</span>;
    return u.is_active ? <span className="admin-pill ok">Active</span> : <span className="admin-pill off">Disabled</span>;
  };

  const roleBadge = (u) => {
    const r = (u.role || (u.is_admin ? 'admin' : 'user')).toLowerCase();
    if (r === 'admin') return <span className="admin-pill admin">Admin</span>;
    if (r === 'dba') return <span className="admin-pill sso">DBA</span>;
    return <span className="admin-pill">User</span>;
  };

  return (
    <div>
      <div className="admin-toolbar">
        <h4>Users {loading && <span className="admin-muted">· loading…</span>}</h4>
        <button className="admin-btn-primary" onClick={() => setForm({ ...blank })}>
          <Plus size={14} /> New User
        </button>
      </div>
      {error && <p className="admin-error">{error}</p>}
      {pendingCount > 0 && (
        <p className="admin-pending-banner">⏳ {pendingCount} pending sign-up request{pendingCount > 1 ? 's' : ''} awaiting approval.</p>
      )}

      <table className="admin-table">
        <thead><tr><th>Username</th><th>Email</th><th>Role</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {rows.map(u => (
            <tr key={u.id}>
              <td>{u.username}</td>
              <td>{u.email}</td>
              <td>{roleBadge(u)}
                  {u.auth_provider !== 'local' && <span className="admin-pill sso">{u.auth_provider}</span>}
                  {(u.roles || []).map(r => <span key={r.id} className="admin-pill">{r.name}</span>)}</td>
              <td>{statusCell(u)}</td>
              <td className="admin-actions">
                {u.approval_status === 'pending' && (
                  <>
                    <button className="admin-approve" title="Approve" onClick={() => approve(u.id)}>✓ Approve</button>
                    <button className="admin-reject" title="Reject" onClick={() => reject(u.id)}>✗</button>
                  </>
                )}
                <button onClick={() => setForm({ ...u, password: '', role_ids: (u.roles || []).map(r => r.id) })}><Pencil size={14} /></button>
                <button onClick={() => remove(u.id)}><Trash2 size={14} /></button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && !loading && <tr><td colSpan={5} className="admin-muted">No users.</td></tr>}
        </tbody>
      </table>

      {form && (
        <FormModal title={form.id ? 'Edit User' : 'New User'} onClose={() => setForm(null)} onSubmit={save}>
          {!form.id && (
            <Field label="Username">
              <input value={form.username} required
                     onChange={e => setForm({ ...form, username: e.target.value })} />
            </Field>
          )}
          <Field label="Email">
            <input type="email" value={form.email} required
                   onChange={e => setForm({ ...form, email: e.target.value })} />
          </Field>
          <Field label={form.id ? 'Reset Password (optional)' : 'Password'}>
            <input type="password" value={form.password} required={!form.id}
                   placeholder={form.id ? 'leave blank to keep' : ''}
                   onChange={e => setForm({ ...form, password: e.target.value })} />
          </Field>
          <Field label="Role">
            <select value={form.role || 'user'} onChange={e => setForm({ ...form, role: e.target.value })}>
              <option value="user">{ROLE_LABELS.user}</option>
              <option value="dba">{ROLE_LABELS.dba}</option>
              <option value="admin">{ROLE_LABELS.admin}</option>
            </select>
          </Field>
          <p className="admin-muted" style={{ fontSize: '0.72rem', marginTop: '-0.2rem' }}>
            <strong>User</strong> can chat only. <strong>DBA</strong> and <strong>Admin</strong> can approve requests; only Admin manages servers, keys & SSO.
            Agent access is granted by the <strong>roles</strong> below (Admin always has every agent).
          </p>
          <Field label="Agent access (roles)">
            <div className="admin-svc">
              {roles.map(r => (
                <label key={r.id} className="admin-check" title={(r.agents || []).map(a => a.name).join(', ') || 'no agents'}>
                  <input type="checkbox" checked={form.role_ids?.includes(r.id)} onChange={() => toggleRole(r.id)} /> {r.name}
                  {(r.agents?.length > 0) && <span className="admin-muted"> · {r.agents.map(a => a.name).join(', ')}</span>}
                </label>
              ))}
              {roles.length === 0 && <span className="admin-muted">No roles defined yet — create one in the Roles tab.</span>}
            </div>
          </Field>
          {form.id && (
            <label className="admin-check">
              <input type="checkbox" checked={!!form.is_active}
                     onChange={e => setForm({ ...form, is_active: e.target.checked })} /> Active
            </label>
          )}
        </FormModal>
      )}
    </div>
  );
}

/* ── Roles & agent permissions ──────────────────────────────────────────────── */

function RolesTab() {
  const { rows, error, loading, reload, setError } = useResource('/admin/roles');
  const [agents, setAgents] = useState([]);   // catalog of gated agents
  const [form, setForm] = useState(null);

  useEffect(() => { api.get('/admin/agents').then(r => setAgents(r.data)).catch(() => {}); }, []);

  const blank = { name: '', description: '', agent_names: [] };

  const toggleAgent = (name) => {
    const has = form.agent_names?.includes(name);
    setForm({ ...form, agent_names: has ? form.agent_names.filter(a => a !== name) : [...(form.agent_names || []), name] });
  };

  const save = async (e) => {
    e.preventDefault();
    const body = { name: form.name, description: form.description, agent_names: form.agent_names };
    try {
      if (form.id) await api.patch(`/admin/roles/${form.id}`, body);
      else await api.post('/admin/roles', body);
      setForm(null); reload();
    } catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const remove = async (id) => {
    if (!window.confirm('Delete this role? Users lose the agent access it granted.')) return;
    try { await api.delete(`/admin/roles/${id}`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  return (
    <div>
      <div className="admin-toolbar">
        <h4>Roles {loading && <span className="admin-muted">· loading…</span>}</h4>
        <button className="admin-btn-primary" onClick={() => setForm({ ...blank })}><Plus size={14} /> New Role</button>
      </div>
      {error && <p className="admin-error">{error}</p>}
      <p className="admin-muted">
        A role grants access to one or more agents; assign roles to users in the <strong>Users</strong> tab.
        Administrators always have every agent, and Chat / Knowledge Base are open to all.
      </p>

      <table className="admin-table">
        <thead><tr><th>Name</th><th>Description</th><th>Agents</th><th>Users</th><th></th></tr></thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.id}>
              <td><strong>{r.name}</strong></td>
              <td className="admin-muted">{r.description || '—'}</td>
              <td>
                {r.agents?.length > 0
                  ? r.agents.map(a => <span key={a.name} className="admin-pill ok">{a.name}</span>)
                  : <span className="admin-muted">none (chat only)</span>}
              </td>
              <td>{r.user_count}</td>
              <td className="admin-actions">
                <button onClick={() => setForm({ id: r.id, name: r.name, description: r.description || '', agent_names: (r.agents || []).map(a => a.name) })}><Pencil size={14} /></button>
                <button onClick={() => remove(r.id)}><Trash2 size={14} /></button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && !loading && <tr><td colSpan={5} className="admin-muted">No roles yet.</td></tr>}
        </tbody>
      </table>

      {form && (
        <FormModal title={form.id ? `Edit Role — ${form.name}` : 'New Role'} onClose={() => setForm(null)} onSubmit={save}>
          <Field label="Name"><input value={form.name} required onChange={e => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="Description"><input value={form.description || ''} onChange={e => setForm({ ...form, description: e.target.value })} /></Field>
          <Field label="Agent permissions">
            <div className="admin-svc">
              {agents.map(a => (
                <label key={a.name} className="admin-check" title={a.description || ''}>
                  <input type="checkbox" checked={form.agent_names?.includes(a.name)} onChange={() => toggleAgent(a.name)} /> {a.name}
                </label>
              ))}
              {agents.length === 0 && <span className="admin-muted">No gated agents available.</span>}
            </div>
          </Field>
        </FormModal>
      )}
    </div>
  );
}

/* ── SSH Servers ────────────────────────────────────────────────────────────── */

function ServersTab() {
  const { rows, error, loading, reload, setError } = useResource('/admin/servers');
  const [form, setForm] = useState(null);
  const [test, setTest] = useState({});

  const blank = {
    name: '', hostname: '', port: 22, username: '', password: '',
    server_type: 'application', app_services: ['web', 'forms', 'concurrent'],
    description: '', is_active: true,
  };

  const toggleSvc = (svc) => {
    const has = form.app_services?.includes(svc);
    setForm({ ...form, app_services: has ? form.app_services.filter(s => s !== svc) : [...(form.app_services || []), svc] });
  };

  const save = async (e) => {
    e.preventDefault();
    const body = { ...form };
    if (form.id && !form.password) delete body.password; // keep existing secret
    try {
      if (form.id) await api.patch(`/admin/servers/${form.id}`, body);
      else await api.post('/admin/servers', body);
      setForm(null); reload();
    } catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const remove = async (id) => {
    if (!window.confirm('Delete this server?')) return;
    try { await api.delete(`/admin/servers/${id}`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const runTest = async (id) => {
    setTest({ ...test, [id]: { message: 'Testing…' } });
    try { setTest({ ...test, [id]: (await api.post(`/admin/servers/${id}/test`)).data }); }
    catch (err) { setTest({ ...test, [id]: { ok: false, message: err.response?.data?.detail || err.message } }); }
  };

  return (
    <div>
      <div className="admin-toolbar">
        <h4>SSH Servers {loading && <span className="admin-muted">· loading…</span>}</h4>
        <button className="admin-btn-primary" onClick={() => setForm({ ...blank })}><Plus size={14} /> New Server</button>
      </div>
      {error && <p className="admin-error">{error}</p>}

      <table className="admin-table">
        <thead><tr><th>Name</th><th>Host</th><th>User</th><th>Type</th><th>Secret</th><th></th></tr></thead>
        <tbody>
          {rows.map(s => (
            <tr key={s.id}>
              <td>{s.name}</td>
              <td>{s.hostname}:{s.port}</td>
              <td>{s.username || '—'}</td>
              <td>{s.server_type}</td>
              <td>{s.has_password ? '🔒 set' : '—'}</td>
              <td className="admin-actions">
                <button onClick={() => runTest(s.id)} title="Test connection">⚡</button>
                <button onClick={() => setForm({ ...s, password: '' })}><Pencil size={14} /></button>
                <button onClick={() => remove(s.id)}><Trash2 size={14} /></button>
                <TestResult result={test[s.id]} />
              </td>
            </tr>
          ))}
          {rows.length === 0 && !loading && <tr><td colSpan={6} className="admin-muted">No servers.</td></tr>}
        </tbody>
      </table>

      {form && (
        <FormModal title={form.id ? 'Edit Server' : 'New Server'} onClose={() => setForm(null)} onSubmit={save}>
          <Field label="Name"><input value={form.name} required onChange={e => setForm({ ...form, name: e.target.value })} /></Field>
          <div className="admin-row">
            <Field label="Hostname"><input value={form.hostname} required onChange={e => setForm({ ...form, hostname: e.target.value })} /></Field>
            <Field label="Port"><input type="number" value={form.port} onChange={e => setForm({ ...form, port: parseInt(e.target.value) || 22 })} /></Field>
          </div>
          <Field label="Username"><input value={form.username || ''} onChange={e => setForm({ ...form, username: e.target.value })} /></Field>
          <Field label="Password">
            <input type="password" value={form.password || ''}
                   placeholder={form.has_password ? '•••• set (leave blank to keep)' : ''}
                   onChange={e => setForm({ ...form, password: e.target.value })} />
          </Field>
          <Field label="Server Type">
            <select value={form.server_type} onChange={e => setForm({ ...form, server_type: e.target.value })}>
              <option value="application">application</option>
              <option value="database">database</option>
            </select>
          </Field>
          {form.server_type === 'application' && (
            <div className="admin-svc">
              {['web', 'forms', 'concurrent'].map(svc => (
                <label key={svc} className="admin-check">
                  <input type="checkbox" checked={form.app_services?.includes(svc)} onChange={() => toggleSvc(svc)} /> {svc}
                </label>
              ))}
            </div>
          )}
          <Field label="Description"><input value={form.description || ''} onChange={e => setForm({ ...form, description: e.target.value })} /></Field>
        </FormModal>
      )}
    </div>
  );
}

/* ── Environments ───────────────────────────────────────────────────────────── */

function EnvironmentsTab() {
  const { rows, error, loading, reload, setError } = useResource('/admin/environments');
  const [servers, setServers] = useState([]);
  const [form, setForm] = useState(null);
  const [test, setTest] = useState({});

  useEffect(() => { api.get('/admin/servers').then(r => setServers(r.data)).catch(() => {}); }, []);

  const blank = {
    name: '', tier: 'nonprod', db_host: '', db_port: 1521, db_sid: '', db_user: 'apps',
    db_password: '', system_user: 'system', system_password: '',
    weblogic_user: 'weblogic', weblogic_password: '', apps_os_user: '',
    description: '', ssh_server_id: null, is_active: true,
  };

  const save = async (e) => {
    e.preventDefault();
    const body = { ...form };
    // On edit, omit blank secret fields so existing values are kept.
    for (const f of ['db_password', 'system_password', 'weblogic_password']) {
      if (form.id && !form[f]) delete body[f];
    }
    try {
      if (form.id) await api.patch(`/admin/environments/${form.id}`, body);
      else await api.post('/admin/environments', body);
      setForm(null); reload();
    } catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const remove = async (id) => {
    if (!window.confirm('Delete this environment?')) return;
    try { await api.delete(`/admin/environments/${id}`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const runTest = async (id) => {
    setTest({ ...test, [id]: { message: 'Testing…' } });
    try { setTest({ ...test, [id]: (await api.post(`/admin/environments/${id}/test`)).data }); }
    catch (err) { setTest({ ...test, [id]: { ok: false, message: err.response?.data?.detail || err.message } }); }
  };

  return (
    <div>
      <div className="admin-toolbar">
        <h4>Environments {loading && <span className="admin-muted">· loading…</span>}</h4>
        <button className="admin-btn-primary" onClick={() => setForm({ ...blank })}><Plus size={14} /> New Environment</button>
      </div>
      {error && <p className="admin-error">{error}</p>}

      <p className="admin-muted">The <strong>tier</strong> classifies an environment for the cloning production guard. Use ⚡ Test to capture the live DBID / global_name.</p>
      <table className="admin-table">
        <thead><tr><th>Name</th><th>Tier</th><th>DB Host</th><th>SID</th><th>Identity (DBID)</th><th>Secret</th><th></th></tr></thead>
        <tbody>
          {rows.map(e => (
            <tr key={e.id}>
              <td>{e.name}</td>
              <td>{e.tier === 'prod'
                ? <span className="admin-pill off">PROD</span>
                : <span className="admin-pill ok">non-prod</span>}</td>
              <td>{e.db_host || '—'}:{e.db_port}</td>
              <td>{e.db_sid || '—'}</td>
              <td>{e.db_id ? <span className="admin-muted">{e.db_id}</span> : '—'}</td>
              <td>{e.has_password ? '🔒 set' : '—'}</td>
              <td className="admin-actions">
                <button onClick={() => runTest(e.id)} title="Test connection">⚡</button>
                <button onClick={() => setForm({ ...e, db_password: '', system_password: '', weblogic_password: '' })}><Pencil size={14} /></button>
                <button onClick={() => remove(e.id)}><Trash2 size={14} /></button>
                <TestResult result={test[e.id]} />
              </td>
            </tr>
          ))}
          {rows.length === 0 && !loading && <tr><td colSpan={7} className="admin-muted">No environments.</td></tr>}
        </tbody>
      </table>

      {form && (
        <FormModal title={form.id ? 'Edit Environment' : 'New Environment'} onClose={() => setForm(null)} onSubmit={save}>
          <div className="admin-row">
            <Field label="Name (e.g. DEV, UAT, PROD)"><input value={form.name} required onChange={e => setForm({ ...form, name: e.target.value })} /></Field>
            <Field label="Tier">
              <select value={form.tier || 'nonprod'} onChange={e => setForm({ ...form, tier: e.target.value })}>
                <option value="nonprod">non-prod</option>
                <option value="prod">PRODUCTION</option>
              </select>
            </Field>
          </div>
          <div className="admin-row">
            <Field label="DB Host"><input value={form.db_host || ''} onChange={e => setForm({ ...form, db_host: e.target.value })} /></Field>
            <Field label="Port"><input type="number" value={form.db_port} onChange={e => setForm({ ...form, db_port: parseInt(e.target.value) || 1521 })} /></Field>
          </div>
          <div className="admin-row">
            <Field label="SID / Service"><input value={form.db_sid || ''} onChange={e => setForm({ ...form, db_sid: e.target.value })} /></Field>
            <Field label="DB User"><input value={form.db_user || ''} onChange={e => setForm({ ...form, db_user: e.target.value })} /></Field>
          </div>
          <Field label="DB Password (APPS schema)">
            <input type="password" value={form.db_password || ''}
                   placeholder={form.has_password ? '•••• set (leave blank to keep)' : ''}
                   onChange={e => setForm({ ...form, db_password: e.target.value })} />
          </Field>

          <p className="admin-muted" style={{ marginTop: '0.6rem' }}>
            <strong>adop / patching credentials</strong> — used by the EBS Patching Agent to run
            adop and OPatch over SSH. APPS is the DB Password above.
          </p>
          <div className="admin-row">
            <Field label="SYSTEM user"><input value={form.system_user || ''} placeholder="system | ebs_system"
                   onChange={e => setForm({ ...form, system_user: e.target.value })} /></Field>
            <Field label="SYSTEM password">
              <input type="password" value={form.system_password || ''}
                     placeholder={form.has_system_password ? '•••• set (leave blank to keep)' : ''}
                     onChange={e => setForm({ ...form, system_password: e.target.value })} />
            </Field>
          </div>
          <div className="admin-row">
            <Field label="WebLogic user"><input value={form.weblogic_user || ''} placeholder="weblogic"
                   onChange={e => setForm({ ...form, weblogic_user: e.target.value })} /></Field>
            <Field label="WebLogic password">
              <input type="password" value={form.weblogic_password || ''}
                     placeholder={form.has_weblogic_password ? '•••• set (leave blank to keep)' : ''}
                     onChange={e => setForm({ ...form, weblogic_password: e.target.value })} />
            </Field>
          </div>
          <Field label="Apps OS user (optional, e.g. applmgr)">
            <input value={form.apps_os_user || ''} onChange={e => setForm({ ...form, apps_os_user: e.target.value })} />
          </Field>

          <Field label="Linked SSH Server (optional, for live adop/OPatch execution)">
            <select value={form.ssh_server_id || ''} onChange={e => setForm({ ...form, ssh_server_id: e.target.value ? parseInt(e.target.value) : null })}>
              <option value="">— none —</option>
              {servers.map(s => <option key={s.id} value={s.id}>{s.name} ({s.hostname})</option>)}
            </select>
          </Field>
        </FormModal>
      )}
    </div>
  );
}

/* ── LLM Providers ──────────────────────────────────────────────────────────── */

function LlmTab() {
  const { rows, error, loading, reload, setError } = useResource('/admin/llm-credentials');
  const [form, setForm] = useState(null);
  const [test, setTest] = useState({});

  const blank = { provider: 'openai', label: '', model: '', api_key: '', base_url: '', is_default: true, is_active: true };

  const save = async (e) => {
    e.preventDefault();
    const body = { ...form };
    if (form.id && !form.api_key) delete body.api_key;
    try {
      if (form.id) await api.patch(`/admin/llm-credentials/${form.id}`, body);
      else await api.post('/admin/llm-credentials', body);
      setForm(null); reload();
    } catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const remove = async (id) => {
    if (!window.confirm('Delete this provider key?')) return;
    try { await api.delete(`/admin/llm-credentials/${id}`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const runTest = async (id) => {
    setTest({ ...test, [id]: { message: 'Testing…' } });
    try { setTest({ ...test, [id]: (await api.post(`/admin/llm-credentials/${id}/test`)).data }); }
    catch (err) { setTest({ ...test, [id]: { ok: false, message: err.response?.data?.detail || err.message } }); }
  };

  return (
    <div>
      <div className="admin-toolbar">
        <h4>LLM Providers {loading && <span className="admin-muted">· loading…</span>}</h4>
        <button className="admin-btn-primary" onClick={() => setForm({ ...blank })}><Plus size={14} /> New Provider Key</button>
      </div>
      {error && <p className="admin-error">{error}</p>}
      <p className="admin-muted">Keys are encrypted at rest and injected server-side — users never see them.</p>

      <table className="admin-table">
        <thead><tr><th>Provider</th><th>Label</th><th>Model</th><th>Default</th><th>Key</th><th></th></tr></thead>
        <tbody>
          {rows.map(c => (
            <tr key={c.id}>
              <td>{c.provider}</td>
              <td>{c.label || '—'}</td>
              <td>{c.model || '—'}</td>
              <td>{c.is_default ? <span className="admin-pill ok">default</span> : '—'}</td>
              <td>{c.has_api_key ? '🔒 set' : '—'}</td>
              <td className="admin-actions">
                <button onClick={() => runTest(c.id)} title="Test provider">⚡</button>
                <button onClick={() => setForm({ ...c, api_key: '' })}><Pencil size={14} /></button>
                <button onClick={() => remove(c.id)}><Trash2 size={14} /></button>
                <TestResult result={test[c.id]} />
              </td>
            </tr>
          ))}
          {rows.length === 0 && !loading && <tr><td colSpan={6} className="admin-muted">No provider keys.</td></tr>}
        </tbody>
      </table>

      {form && (
        <FormModal title={form.id ? 'Edit Provider Key' : 'New Provider Key'} onClose={() => setForm(null)} onSubmit={save}>
          <Field label="Provider">
            <select value={form.provider} onChange={e => setForm({ ...form, provider: e.target.value })}>
              {PROVIDERS.map(p => <option key={p} value={p}>{p}</option>)}
            </select>
          </Field>
          <Field label="Label"><input value={form.label || ''} onChange={e => setForm({ ...form, label: e.target.value })} /></Field>
          <Field label="Default Model"><input value={form.model || ''} placeholder="e.g. gpt-4o-mini / gemini-2.0-flash" onChange={e => setForm({ ...form, model: e.target.value })} /></Field>
          {form.provider !== 'ollama' && (
            <Field label="API Key">
              <input type="password" value={form.api_key || ''}
                     placeholder={form.has_api_key ? '•••• set (leave blank to keep)' : ''}
                     onChange={e => setForm({ ...form, api_key: e.target.value })} />
            </Field>
          )}
          {(form.provider === 'ollama') && (
            <Field label="Base URL"><input value={form.base_url || ''} placeholder="http://localhost:11434" onChange={e => setForm({ ...form, base_url: e.target.value })} /></Field>
          )}
          <label className="admin-check">
            <input type="checkbox" checked={!!form.is_default} onChange={e => setForm({ ...form, is_default: e.target.checked })} /> Default for this provider
          </label>
        </FormModal>
      )}
    </div>
  );
}

/* ── Agent Routing (per-agent provider + complexity model tiers) ────────────── */

const ROUTING_PROVIDERS = ['', 'ollama', 'openai', 'anthropic', 'gemini'];

function RoutingTab() {
  const { rows, error, loading, reload, setError } = useResource('/admin/agent-policies');
  const [meta, setMeta] = useState({ agents: [], tier_defaults: {}, routing_enabled: true });
  const [form, setForm] = useState(null);

  useEffect(() => {
    api.get('/admin/agent-policies/meta').then(r => setMeta(r.data)).catch(() => {});
  }, []);

  const blank = {
    agent: '', provider: '', small_model: '', large_model: '',
    force_tier: '', routing_enabled: true, is_active: true,
  };

  const save = async (e) => {
    e.preventDefault();
    // Normalise empty strings to null so the backend keeps provider/tier defaults.
    const body = {
      ...form,
      provider: form.provider || null,
      small_model: form.small_model || null,
      large_model: form.large_model || null,
      force_tier: form.force_tier || null,
    };
    try {
      if (form.id) await api.patch(`/admin/agent-policies/${form.id}`, body);
      else await api.post('/admin/agent-policies', body);
      setForm(null); reload();
    } catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const remove = async (id) => {
    if (!window.confirm('Delete this agent policy?')) return;
    try { await api.delete(`/admin/agent-policies/${id}`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  // Show what a policy resolves to, falling back to the provider's tier defaults.
  const tierHint = (p, tier) => {
    const explicit = tier === 'small' ? p.small_model : p.large_model;
    if (explicit) return explicit;
    const prov = p.provider || '(request)';
    const def = meta.tier_defaults[p.provider]?.[tier];
    return def ? `${def} · default` : `${prov} default`;
  };

  // Agents that don't yet have a policy (one policy per agent).
  const used = new Set(rows.map(r => r.agent));
  const freeAgents = meta.agents.filter(a => !used.has(a));

  return (
    <div>
      <div className="admin-toolbar">
        <h4>Agent Routing {loading && <span className="admin-muted">· loading…</span>}</h4>
        <button className="admin-btn-primary" disabled={freeAgents.length === 0}
                onClick={() => setForm({ ...blank, agent: freeAgents[0] || '' })}>
          <Plus size={14} /> New Agent Policy
        </button>
      </div>
      {error && <p className="admin-error">{error}</p>}
      <p className="admin-muted">
        Each agent can use its own provider and a small/large model pair. A heuristic reads the task
        and routes simple requests to the small model, complex ones to the large model. Leave a field
        blank to use the provider default. {meta.routing_enabled ? '' : '⚠️ Routing is globally disabled (LLM_ROUTING_ENABLED=0).'}
      </p>

      <table className="admin-table">
        <thead><tr>
          <th>Agent</th><th>Provider</th><th>Small (simple)</th><th>Large (complex)</th>
          <th>Mode</th><th>Active</th><th></th>
        </tr></thead>
        <tbody>
          {rows.map(p => (
            <tr key={p.id}>
              <td><strong>{p.agent}</strong></td>
              <td>{p.provider || <span className="admin-muted">request</span>}</td>
              <td>{tierHint(p, 'small')}</td>
              <td>{tierHint(p, 'large')}</td>
              <td>
                {!p.routing_enabled ? <span className="admin-pill">off</span>
                  : p.force_tier ? <span className="admin-pill">forced: {p.force_tier}</span>
                  : <span className="admin-pill ok">auto</span>}
              </td>
              <td>{p.is_active ? <span className="admin-pill ok">yes</span> : <span className="admin-pill">no</span>}</td>
              <td className="admin-actions">
                <button onClick={() => setForm({ ...p, provider: p.provider || '', small_model: p.small_model || '', large_model: p.large_model || '', force_tier: p.force_tier || '' })}><Pencil size={14} /></button>
                <button onClick={() => remove(p.id)}><Trash2 size={14} /></button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && !loading && <tr><td colSpan={7} className="admin-muted">No agent policies — all agents use the request/default model.</td></tr>}
        </tbody>
      </table>

      {form && (
        <FormModal title={form.id ? `Edit Policy — ${form.agent}` : 'New Agent Policy'} onClose={() => setForm(null)} onSubmit={save}>
          <Field label="Agent">
            <select value={form.agent} disabled={!!form.id}
                    onChange={e => setForm({ ...form, agent: e.target.value })}>
              {(form.id ? [form.agent] : freeAgents).map(a => <option key={a} value={a}>{a}</option>)}
            </select>
          </Field>
          <Field label="Provider (blank = keep request/default)">
            <select value={form.provider} onChange={e => setForm({ ...form, provider: e.target.value })}>
              {ROUTING_PROVIDERS.map(p => <option key={p} value={p}>{p || '— request/default —'}</option>)}
            </select>
          </Field>
          <Field label="Small model (simple tasks)">
            <input value={form.small_model} placeholder={meta.tier_defaults[form.provider]?.small || 'provider default'}
                   onChange={e => setForm({ ...form, small_model: e.target.value })} />
          </Field>
          <Field label="Large model (complex tasks)">
            <input value={form.large_model} placeholder={meta.tier_defaults[form.provider]?.large || 'provider default'}
                   onChange={e => setForm({ ...form, large_model: e.target.value })} />
          </Field>
          <Field label="Tier selection">
            <select value={form.force_tier} onChange={e => setForm({ ...form, force_tier: e.target.value })}>
              <option value="">Auto (classify task complexity)</option>
              <option value="small">Always small</option>
              <option value="large">Always large</option>
            </select>
          </Field>
          <label className="admin-check">
            <input type="checkbox" checked={!!form.routing_enabled} onChange={e => setForm({ ...form, routing_enabled: e.target.checked })} /> Routing enabled
          </label>
          <label className="admin-check">
            <input type="checkbox" checked={!!form.is_active} onChange={e => setForm({ ...form, is_active: e.target.checked })} /> Active
          </label>
        </FormModal>
      )}
    </div>
  );
}

/* ── Integrations (Git / Confluence) ────────────────────────────────────────── */

function IntegrationsTab() {
  const { rows, error, loading, reload, setError } = useResource('/admin/integrations');
  const [form, setForm] = useState(null);
  const [test, setTest] = useState({});

  const git = rows.filter(r => r.kind === 'git');
  const conf = rows.filter(r => r.kind === 'confluence');

  const runTest = async (id) => {
    setTest(t => ({ ...t, [id]: { message: 'Testing…' } }));
    try { const r = (await api.post(`/admin/integrations/${id}/test`)).data; setTest(t => ({ ...t, [id]: r })); }
    catch (err) { setTest(t => ({ ...t, [id]: { ok: false, message: err.response?.data?.detail || err.message } })); }
  };

  const save = async (e) => {
    e.preventDefault();
    const body = {
      kind: form.kind, name: form.name, host: form.host || null,
      base_url: form.base_url || null, username: form.username || null,
      is_default: !!form.is_default, is_active: form.is_active !== false,
    };
    if (form.secret) body.secret = form.secret;
    try {
      if (form.id) await api.patch(`/admin/integrations/${form.id}`, body);
      else await api.post('/admin/integrations', body);
      setForm(null); reload();
    } catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const remove = async (id) => {
    if (!window.confirm('Delete this credential?')) return;
    try { await api.delete(`/admin/integrations/${id}`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
  };

  const newCred = (kind, count) => setForm({
    kind, name: '', host: '', base_url: '', username: '', secret: '',
    is_default: count === 0, is_active: true,
  });

  const section = (kind, list, label, hint) => (
    <div style={{ marginBottom: '1.5rem' }}>
      <div className="admin-toolbar">
        <h4>{label} {loading && <span className="admin-muted">· loading…</span>}</h4>
        <button className="admin-btn-primary" onClick={() => newCred(kind, list.length)}><Plus size={14} /> New</button>
      </div>
      <p className="admin-muted">{hint}</p>
      <table className="admin-table">
        <thead><tr><th>Name</th><th>Host</th><th>User</th><th>Default</th><th>Secret</th><th></th></tr></thead>
        <tbody>
          {list.map(c => (
            <tr key={c.id}>
              <td>{c.name}</td>
              <td>{c.host || '—'}</td>
              <td>{c.username || '—'}</td>
              <td>{c.is_default ? <span className="admin-pill ok">default</span> : '—'}</td>
              <td>{c.has_secret ? '🔒 set' : '—'}</td>
              <td className="admin-actions">
                <button onClick={() => runTest(c.id)} title="Test credential">⚡</button>
                <button onClick={() => setForm({ ...c, secret: '' })}><Pencil size={14} /></button>
                <button onClick={() => remove(c.id)}><Trash2 size={14} /></button>
                <TestResult result={test[c.id]} />
              </td>
            </tr>
          ))}
          {list.length === 0 && !loading && <tr><td colSpan={6} className="admin-muted">None configured.</td></tr>}
        </tbody>
      </table>
    </div>
  );

  return (
    <div>
      {error && <p className="admin-error">{error}</p>}
      <p className="admin-muted">Tokens are encrypted at rest and injected automatically at deploy time — users are never asked for them.</p>
      {section('git', git, 'Git Repositories', 'A personal access token per Git host, matched to the repository URL (e.g. host = github.com, gitlab.corp.local).')}
      {section('confluence', conf, 'Confluence', 'An API token per Confluence site, matched to the page URL. For Confluence Cloud set the email as Username; for Server/DC leave Username blank and paste a PAT.')}

      {form && (
        <FormModal
          title={`${form.id ? 'Edit' : 'New'} ${form.kind === 'git' ? 'Git' : 'Confluence'} Credential`}
          onClose={() => setForm(null)} onSubmit={save}
        >
          <Field label="Name"><input value={form.name} required onChange={e => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label={form.kind === 'git' ? 'Host (e.g. github.com)' : 'Site host (e.g. mysite.atlassian.net)'}>
            <input value={form.host || ''} onChange={e => setForm({ ...form, host: e.target.value })} />
          </Field>
          <Field label={form.kind === 'git' ? 'Username (optional)' : 'Email — Confluence Cloud (blank for Server PAT)'}>
            <input value={form.username || ''} onChange={e => setForm({ ...form, username: e.target.value })} />
          </Field>
          <Field label={form.kind === 'git' ? 'Personal Access Token' : 'API Token / PAT'}>
            <input type="password" value={form.secret || ''}
                   placeholder={form.has_secret ? '•••• set (leave blank to keep)' : ''}
                   onChange={e => setForm({ ...form, secret: e.target.value })} />
          </Field>
          <label className="admin-check">
            <input type="checkbox" checked={!!form.is_default} onChange={e => setForm({ ...form, is_default: e.target.checked })} />
            Default for {form.kind}
          </label>
        </FormModal>
      )}
    </div>
  );
}


/* ── Monitoring ─────────────────────────────────────────────────────────────── */

function fmtNum(n) { return Number(n || 0).toLocaleString(); }
function fmtBytes(n) {
  n = Number(n || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(2)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}
function ago(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

function MonitoringTab() {
  const [ov, setOv] = useState(null);
  const [users, setUsers] = useState([]);
  const [qual, setQual] = useState(null);
  const [rag, setRag] = useState(null);
  const [error, setError] = useState('');
  const [showTasks, setShowTasks] = useState(false);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const [o, u, q, g] = await Promise.all([
          api.get('/admin/monitoring/overview'),
          api.get('/admin/monitoring/users'),
          api.get('/admin/monitoring/quality'),
          api.get('/admin/monitoring/rag'),
        ]);
        if (!active) return;
        setOv(o.data); setUsers(u.data.users || []); setQual(q.data); setRag(g.data); setError('');
      } catch (e) { if (active) setError(e.response?.data?.detail || e.message); }
    };
    load();
    const id = setInterval(load, 5000);
    return () => { active = false; clearInterval(id); };
  }, []);

  if (!ov) return <p className="admin-muted">{error || 'Loading…'}</p>;

  const cards = [
    { label: 'Connected Users', value: fmtNum(ov.connected_users) },
    { label: 'Tasks Executed', value: fmtNum(ov.total_tasks), sub: 'LLM calls · click to view', onClick: () => setShowTasks(true) },
    { label: 'Open Streams', value: fmtNum(ov.open_streams) },
    { label: 'Total Requests', value: fmtNum(ov.total_requests) },
    { label: 'Total Tokens', value: fmtNum(ov.total_tokens), sub: `${fmtNum(ov.prompt_tokens)} in · ${fmtNum(ov.completion_tokens)} out` },
    { label: 'Tokens (24h)', value: fmtNum(ov.tokens_24h) },
    { label: 'Data Out', value: fmtBytes(ov.bytes_out) },
    { label: 'Data In', value: fmtBytes(ov.bytes_in) },
  ];

  return (
    <div>
      <div className="admin-toolbar">
        <h4>Live Monitoring <span className="admin-muted">· auto-refreshes every 5s</span></h4>
      </div>
      {error && <p className="admin-error">{error}</p>}

      <div className="mon-cards">
        {cards.map(c => (
          <div key={c.label} className={`mon-card${c.onClick ? ' clickable' : ''}`}
               onClick={c.onClick} role={c.onClick ? 'button' : undefined}
               tabIndex={c.onClick ? 0 : undefined}
               onKeyDown={c.onClick ? (e) => { if (e.key === 'Enter') c.onClick(); } : undefined}>
            <div className="mon-card-label">{c.label}</div>
            <div className="mon-card-value">{c.value}</div>
            {c.sub && <div className="mon-card-sub">{c.sub}</div>}
          </div>
        ))}
      </div>

      {showTasks && <TasksModal onClose={() => setShowTasks(false)} />}

      {rag && <RagPanel rag={rag} />}

      {qual && <QualityPanel qual={qual} />}

      <h4 style={{ margin: '1.2rem 0 0.5rem' }}>Connected Users <span className="admin-muted">(active in last 5 min)</span></h4>
      <table className="admin-table">
        <thead><tr>
          <th>User</th><th>Requests</th><th>Tokens (total · in/out)</th>
          <th>Context size</th><th>Data Out</th><th>Last activity</th><th>Last endpoint</th>
        </tr></thead>
        <tbody>
          {users.map(u => (
            <tr key={u.user_id}>
              <td>{u.username}</td>
              <td>{fmtNum(u.requests)}</td>
              <td>{fmtNum(u.total_tokens)} <span className="admin-muted">({fmtNum(u.prompt_tokens)}/{fmtNum(u.completion_tokens)})</span></td>
              <td>{fmtNum(u.context_chars)} ch <span className="admin-muted">· ~{fmtNum(u.est_context_tokens)} tok</span></td>
              <td>{fmtBytes(u.bytes_out)}</td>
              <td>{ago(u.last_seen)}</td>
              <td className="admin-muted">{u.last_endpoint}</td>
            </tr>
          ))}
          {users.length === 0 && <tr><td colSpan={7} className="admin-muted">No active users in the last 5 minutes.</td></tr>}
        </tbody>
      </table>

      {ov.providers?.length > 0 && (
        <>
          <h4 style={{ margin: '1.2rem 0 0.5rem' }}>By Provider</h4>
          <table className="admin-table">
            <thead><tr><th>Provider</th><th>Calls</th><th>Prompt tokens</th><th>Completion tokens</th></tr></thead>
            <tbody>
              {ov.providers.map(p => (
                <tr key={p.provider}>
                  <td>{p.provider}</td><td>{fmtNum(p.calls)}</td>
                  <td>{fmtNum(p.prompt_tokens)}</td><td>{fmtNum(p.completion_tokens)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

function pct(v) { return v == null ? '—' : `${v}%`; }

// Friendly label for the endpoint string recorded against each task.
const TASK_LABELS = {
  'chat.stream': 'Chat', 'chat.stream.cache_hit': 'Chat (cached)',
  'performance.analyze': 'Performance', 'deployment.agent': 'Deployment',
};
function taskLabel(ep) { return TASK_LABELS[ep] || ep; }

// Format a Date as a datetime-local input value (YYYY-MM-DDTHH:mm, local time).
function _localDt(d) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
// Default Tasks window: the last 24 hours.
function _last24h() {
  const now = new Date();
  return { date_from: _localDt(new Date(now.getTime() - 24 * 3600 * 1000)), date_to: _localDt(now) };
}

function TasksModal({ onClose }) {
  const [draft, setDraft] = useState(_last24h);     // pending filter (edited in inputs)
  const [filter, setFilter] = useState(_last24h);   // applied filter (drives the fetch)
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  const [limit, setLimit] = useState(100);

  useEffect(() => {
    let active = true;
    setData(null);
    api.get('/admin/monitoring/tasks' + buildQs({ ...filter, limit }))
      .then(r => { if (active) setData(r.data); })
      .catch(e => { if (active) setError(e.response?.data?.detail || e.message); });
    return () => { active = false; };
  }, [filter, limit]);

  const apply = () => { setLimit(100); setFilter(draft); };
  const resetRange = () => { const d = _last24h(); setDraft(d); setLimit(100); setFilter(d); };
  const tasks = data?.tasks || [];

  return (
    <div className="admin-form-overlay" onMouseDown={onClose}>
      <div className="admin-form mon-tasks-modal" onMouseDown={e => e.stopPropagation()}>
        <div className="admin-form-header">
          <h4>Tasks Executed {data && <span className="admin-muted">· {fmtNum(data.total)} LLM calls in range</span>}</h4>
          <button type="button" className="admin-close" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="admin-form-body">
          {error && <p className="admin-error">{error}</p>}
          <p className="admin-muted">
            Each task is one LLM call. <strong>Input</strong> = prompt tokens, <strong>Output</strong> = completion
            tokens, <strong>Limit</strong> = the output-token cap that applied (— = uncapped / provider default).
          </p>
          <div style={_filterBar}>
            <FilterLabel label="From">
              <input style={_fs} type="datetime-local" value={draft.date_from}
                onChange={e => setDraft(d => ({ ...d, date_from: e.target.value }))} />
            </FilterLabel>
            <FilterLabel label="To">
              <input style={_fs} type="datetime-local" value={draft.date_to}
                onChange={e => setDraft(d => ({ ...d, date_to: e.target.value }))} />
            </FilterLabel>
            <button className="admin-btn-primary" onClick={apply}>Apply</button>
            <button className="admin-btn-ghost" onClick={resetRange}>Last 24h</button>
          </div>
          <table className="admin-table">
            <thead><tr>
              <th>Time</th><th>User</th><th>Task</th><th>Provider</th><th>Model</th>
              <th>Input</th><th>Output</th><th>Total</th><th>Limit</th>
            </tr></thead>
            <tbody>
              {tasks.map(t => (
                <tr key={t.id}>
                  <td className="admin-muted">{ago(t.created_at)}</td>
                  <td>{t.username}</td>
                  <td>{taskLabel(t.endpoint)}</td>
                  <td>{t.provider}</td>
                  <td className="admin-muted">{t.model}</td>
                  <td>{fmtNum(t.prompt_tokens)}</td>
                  <td>{fmtNum(t.completion_tokens)}</td>
                  <td><strong>{fmtNum(t.total_tokens)}</strong></td>
                  <td>{t.token_limit ? fmtNum(t.token_limit) : <span className="admin-muted">—</span>}</td>
                </tr>
              ))}
              {data && tasks.length === 0 && <tr><td colSpan={9} className="admin-muted">No tasks recorded yet.</td></tr>}
              {!data && !error && <tr><td colSpan={9} className="admin-muted">Loading…</td></tr>}
            </tbody>
          </table>
          {data && data.total > tasks.length && (
            <button className="admin-btn-ghost" onClick={() => setLimit(l => l + 100)}>
              Load more ({fmtNum(tasks.length)} of {fmtNum(data.total)})
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function RagPanel({ rag }) {
  const ms = (v) => (v == null ? '—' : `${fmtNum(v)} ms`);
  const num = (v) => (v == null ? '—' : v);
  const cards = [
    { label: 'Retrievals', value: fmtNum(rag.queries), sub: `${fmtNum(rag.hits)} grounded · ${fmtNum(rag.misses)} abstained` },
    { label: 'Grounded Hit Rate', value: pct(rag.hit_rate), sub: 'queries that found KB context' },
    { label: 'Avg Relevance', value: num(rag.avg_relevance), sub: 'cross-encoder top score' },
    { label: 'Avg Chunks / Hit', value: num(rag.avg_chunks), sub: 'context passages injected' },
    { label: 'Cache Hit Rate', value: pct(rag.cache_hit_rate), sub: `${fmtNum(rag.cache_hits)} served from cache` },
    { label: 'Avg Retrieval', value: ms(rag.avg_retrieval_ms), sub: 'embed + rerank time' },
    { label: 'Grounded Answers', value: pct(rag.grounded_rate), sub: `${fmtNum(rag.grounded)} of ${fmtNum(rag.generations)} answers` },
    { label: 'Avg Generation', value: ms(rag.avg_generation_ms), sub: 'time to full answer' },
  ];
  return (
    <div>
      <h4 style={{ margin: '1.4rem 0 0.5rem' }}>
        RAG Retrieval &amp; Generation{' '}
        <span className="admin-muted">· bi-encoder recall → cross-encoder rerank → grounded generation</span>
      </h4>
      <div className="mon-cards">
        {cards.map(c => (
          <div key={c.label} className="mon-card">
            <div className="mon-card-label">{c.label}</div>
            <div className="mon-card-value">{c.value}</div>
            <div className="mon-card-sub">{c.sub}</div>
          </div>
        ))}
      </div>
      {rag.web_fallbacks > 0 && (
        <p className="admin-muted" style={{ marginTop: '0.4rem' }}>
          ⚠️ {fmtNum(rag.web_fallbacks)} retrieval(s) used the live web fallback (unverified) instead of the local KB.
        </p>
      )}
      <p className="admin-muted" style={{ marginTop: '0.4rem', fontSize: '0.72rem' }}>
        <strong>Grounded hit rate</strong> = queries that cleared the relevance floor and injected KB context; the rest
        make the model abstain rather than fabricate. Live counters (Redis-backed).
      </p>
    </div>
  );
}

function QualityPanel({ qual }) {
  const { rlaif: rl, feedback: fb, rlaif_recent: rlR, feedback_recent: fbR } = qual;
  const hrs = qual.recent_window_hours || 24;

  const cards = [
    {
      label: 'RAG Faithfulness (Automated)',
      value: pct(rl.pass_rate),
      sub: `${fmtNum(rl.passed)} passed · ${fmtNum(rl.failed)} flagged of ${fmtNum(rl.total)} audited`,
    },
    {
      label: 'User Satisfaction (Feedback)',
      value: pct(fb.positive_rate),
      sub: `${fmtNum(fb.positive)} 👍 · ${fmtNum(fb.negative)} 👎 of ${fmtNum(fb.total)} rated`,
    },
  ];

  const rows = [
    { label: 'RAG faithfulness audit', span: 'all time', rate: rl.pass_rate, good: rl.passed, bad: rl.failed, total: rl.total },
    { label: 'RAG faithfulness audit', span: `last ${hrs}h`, rate: rlR.pass_rate, good: rlR.passed, bad: rlR.failed, total: rlR.total },
    { label: 'User feedback', span: 'all time', rate: fb.positive_rate, good: fb.positive, bad: fb.negative, total: fb.total },
    { label: 'User feedback', span: `last ${hrs}h`, rate: fbR.positive_rate, good: fbR.positive, bad: fbR.negative, total: fbR.total },
  ];

  return (
    <div>
      <h4 style={{ margin: '1.4rem 0 0.5rem' }}>
        Response Quality &amp; Accuracy{' '}
        <span className="admin-muted">· RLAIF faithfulness audit + human feedback</span>
      </h4>

      <div className="mon-cards">
        {cards.map(c => (
          <div key={c.label} className="mon-card">
            <div className="mon-card-label">{c.label}</div>
            <div className="mon-card-value">{c.value}</div>
            <div className="mon-card-sub">{c.sub}</div>
          </div>
        ))}
      </div>

      <table className="admin-table" style={{ marginTop: '0.6rem' }}>
        <thead><tr>
          <th>Accuracy signal</th><th>Pass / Positive rate</th>
          <th>Passed / 👍</th><th>Flagged / 👎</th><th>Total rated</th>
        </tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td>{r.label} <span className="admin-muted">({r.span})</span></td>
              <td>{pct(r.rate)}</td>
              <td>{fmtNum(r.good)}</td>
              <td>{fmtNum(r.bad)}</td>
              <td>{fmtNum(r.total)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {qual.flags?.length > 0 && (
        <>
          <h4 style={{ margin: '1.1rem 0 0.4rem', fontSize: '0.9rem' }}>
            Recent automated flags{' '}
            <span className="admin-muted">(possible hallucination / inaccuracy)</span>
          </h4>
          <table className="admin-table">
            <thead><tr><th>Msg</th><th>When</th><th>Critique</th></tr></thead>
            <tbody>
              {qual.flags.map(f => (
                <tr key={f.message_id}>
                  <td>#{f.message_id}</td>
                  <td>{ago(f.created_at)}</td>
                  <td className="admin-muted">{f.critique}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}


/* ── Audit (clones · users · agents) ────────────────────────────────────────── */

function buildQs(params) {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v) p.append(k, v); });
  const s = p.toString();
  return s ? `?${s}` : '';
}

function downloadCsv(pathOrFn, filename) {
  return async () => {
    const path = typeof pathOrFn === 'function' ? pathOrFn() : pathOrFn;
    try {
      const res = await api.get(path, { responseType: 'blob' });
      const url = URL.createObjectURL(new Blob([res.data], { type: 'text/csv' }));
      const a = document.createElement('a'); a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    } catch { /* ignore */ }
  };
}

const _fs = {
  background: '#0c1322', border: '1px solid #2a3a55', color: '#e6ecf5',
  borderRadius: 7, padding: '0.35rem 0.5rem', fontSize: '0.82rem',
};
const _filterBar = {
  display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'flex-end',
  padding: '0.6rem 0', marginBottom: '0.5rem',
};
function FilterLabel({ label, children }) {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: '0.74rem', color: '#8899aa' }}>
      {label}
      {children}
    </label>
  );
}

function AuditTab({ currentUser = {} }) {
  const [view, setView] = useState('users');   // users | agents | clones
  const VIEWS = [
    { key: 'users', label: 'User Activity' },
    { key: 'agents', label: 'Agent Invocations' },
    { key: 'clones', label: 'Clones' },
  ];
  return (
    <div>
      <div className="admin-tabs" style={{ padding: 0, border: 'none', marginBottom: '0.8rem', background: 'none' }}>
        {VIEWS.map(v => (
          <button key={v.key} className={`admin-tab ${view === v.key ? 'active' : ''}`} onClick={() => setView(v.key)}>{v.label}</button>
        ))}
      </div>
      {view === 'users' && <UserAuditView />}
      {view === 'agents' && <AgentAuditView />}
      {view === 'clones' && <CloneAuditView currentUser={currentUser} />}
    </div>
  );
}

function UserAuditView() {
  const _blank = { username: '', event_type: '', ip: '', date_from: '', date_to: '' };
  const [draft, setDraft] = useState(_blank);
  const [url, setUrl] = useState('/admin/audit/users');
  const { rows, error, loading, reload } = useResource(url);

  const apply = () => setUrl('/admin/audit/users' + buildQs(draft));
  const reset = () => { setDraft(_blank); setUrl('/admin/audit/users'); };
  const csvUrl = () => '/admin/audit/users.csv' + buildQs(draft);

  const evBadge = (e) =>
    e === 'login_failed' ? <span className="admin-pill off">login failed</span>
      : e === 'logout' ? <span className="admin-pill">logout</span>
        : <span className="admin-pill ok">login</span>;

  return (
    <div>
      <div className="admin-toolbar">
        <h4>User Activity {loading && <span className="admin-muted">· loading…</span>}</h4>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button className="admin-btn-ghost" onClick={reload}>Refresh</button>
          <button className="admin-btn-primary" onClick={downloadCsv(csvUrl, 'user_audit.csv')}><Download size={14} /> Export CSV</button>
        </div>
      </div>
      <div style={_filterBar}>
        <FilterLabel label="Username">
          <input style={_fs} placeholder="partial match" value={draft.username}
            onChange={e => setDraft(d => ({ ...d, username: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="Event type">
          <select style={_fs} value={draft.event_type}
            onChange={e => setDraft(d => ({ ...d, event_type: e.target.value }))}>
            <option value="">All</option>
            <option value="login">Login</option>
            <option value="logout">Logout</option>
            <option value="login_failed">Login failed</option>
          </select>
        </FilterLabel>
        <FilterLabel label="IP address">
          <input style={_fs} placeholder="partial match" value={draft.ip}
            onChange={e => setDraft(d => ({ ...d, ip: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="From">
          <input style={_fs} type="datetime-local" value={draft.date_from}
            onChange={e => setDraft(d => ({ ...d, date_from: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="To">
          <input style={_fs} type="datetime-local" value={draft.date_to}
            onChange={e => setDraft(d => ({ ...d, date_to: e.target.value }))} />
        </FilterLabel>
        <button className="admin-btn-primary" style={{ alignSelf: 'flex-end' }} onClick={apply}>Search</button>
        <button className="admin-btn-ghost" style={{ alignSelf: 'flex-end' }} onClick={reset}>Reset</button>
      </div>
      {error && <p className="admin-error">{error}</p>}
      <p className="admin-muted">Who connected, from which machine and IP address (logins, logouts, failed attempts).</p>
      <table className="admin-table">
        <thead><tr><th>When</th><th>User</th><th>Event</th><th>IP address</th><th>Machine</th><th>User-Agent</th></tr></thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.id}>
              <td className="admin-muted">{r.created_at ? new Date(r.created_at).toLocaleString() : '—'}</td>
              <td>{r.user || '—'}</td>
              <td>{evBadge(r.event_type)}</td>
              <td>{r.ip || '—'}</td>
              <td>{r.machine || '—'}</td>
              <td className="admin-muted" style={{ maxWidth: 300, fontSize: '0.72rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.user_agent}>{r.user_agent || '—'}</td>
            </tr>
          ))}
          {rows.length === 0 && !loading && <tr><td colSpan={6} className="admin-muted">No user activity recorded.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function AgentAuditView() {
  const _blank = { username: '', agent: '', ip: '', date_from: '', date_to: '' };
  const [draft, setDraft] = useState(_blank);
  const [url, setUrl] = useState('/admin/audit/agents');
  const { rows, error, loading, reload } = useResource(url);

  const apply = () => setUrl('/admin/audit/agents' + buildQs(draft));
  const reset = () => { setDraft(_blank); setUrl('/admin/audit/agents'); };
  const csvUrl = () => '/admin/audit/agents.csv' + buildQs(draft);

  return (
    <div>
      <div className="admin-toolbar">
        <h4>Agent Invocations {loading && <span className="admin-muted">· loading…</span>}</h4>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button className="admin-btn-ghost" onClick={reload}>Refresh</button>
          <button className="admin-btn-primary" onClick={downloadCsv(csvUrl, 'agent_audit.csv')}><Download size={14} /> Export CSV</button>
        </div>
      </div>
      <div style={_filterBar}>
        <FilterLabel label="Username">
          <input style={_fs} placeholder="partial match" value={draft.username}
            onChange={e => setDraft(d => ({ ...d, username: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="Agent">
          <select style={_fs} value={draft.agent}
            onChange={e => setDraft(d => ({ ...d, agent: e.target.value }))}>
            <option value="">All</option>
            <option value="chat">Chat</option>
            <option value="deployment">Deployment</option>
            <option value="performance">Performance</option>
            <option value="cloning">Cloning</option>
            <option value="patching">Patching</option>
            <option value="knowledge">Knowledge Base</option>
          </select>
        </FilterLabel>
        <FilterLabel label="IP address">
          <input style={_fs} placeholder="partial match" value={draft.ip}
            onChange={e => setDraft(d => ({ ...d, ip: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="From">
          <input style={_fs} type="datetime-local" value={draft.date_from}
            onChange={e => setDraft(d => ({ ...d, date_from: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="To">
          <input style={_fs} type="datetime-local" value={draft.date_to}
            onChange={e => setDraft(d => ({ ...d, date_to: e.target.value }))} />
        </FilterLabel>
        <button className="admin-btn-primary" style={{ alignSelf: 'flex-end' }} onClick={apply}>Search</button>
        <button className="admin-btn-ghost" style={{ alignSelf: 'flex-end' }} onClick={reset}>Reset</button>
      </div>
      {error && <p className="admin-error">{error}</p>}
      <p className="admin-muted">Who invoked which agent (chat, deployment, performance, cloning, knowledge base), from which IP / machine.</p>
      <table className="admin-table">
        <thead><tr><th>When</th><th>User</th><th>Agent</th><th>IP address</th><th>Machine</th><th>Endpoint</th></tr></thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.id}>
              <td className="admin-muted">{r.created_at ? new Date(r.created_at).toLocaleString() : '—'}</td>
              <td>{r.user || '—'}</td>
              <td><span className="admin-pill admin">{r.agent}</span></td>
              <td>{r.ip || '—'}</td>
              <td>{r.machine || '—'}</td>
              <td className="admin-muted" style={{ fontSize: '0.74rem' }}>{r.method} {r.path}</td>
            </tr>
          ))}
          {rows.length === 0 && !loading && <tr><td colSpan={6} className="admin-muted">No agent invocations recorded.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function CloneAuditView({ currentUser = {} }) {
  const _blank = { username: '', guard_status: '', status: '', source: '', target: '', date_from: '', date_to: '' };
  const [draft, setDraft] = useState(_blank);
  const [url, setUrl] = useState('/admin/audit/clones');
  const { rows, error, loading, reload, setError } = useResource(url);
  const [busy, setBusy] = useState(null);

  const apply = () => setUrl('/admin/audit/clones' + buildQs(draft));
  const reset = () => { setDraft(_blank); setUrl('/admin/audit/clones'); };
  const csvUrl = () => '/admin/audit/clones.csv' + buildQs(draft);

  const badge = (g) =>
    g === 'blocked' ? <span className="admin-pill off">blocked</span>
      : g === 'overridden' ? <span className="admin-pill admin">overridden</span>
        : <span className="admin-pill ok">passed</span>;

  const approve = async (r) => {
    const reason = window.prompt(
      `Approve override for blocked clone #${r.id} (target ${r.target}).\n` +
      `This authorises cloning onto a target the production guard flagged.\n\nEnter an override reason:`);
    if (reason === null) return;
    if (!reason.trim()) { setError('An override reason is required.'); return; }
    setBusy(r.id);
    try { await api.post(`/cloning/${r.id}/override`, { reason: reason.trim() }); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
    finally { setBusy(null); }
  };
  const reject = async (r) => {
    if (!window.confirm(`Reject blocked clone #${r.id} (target ${r.target})?`)) return;
    setBusy(r.id);
    try { await api.post(`/cloning/${r.id}/reject`); reload(); }
    catch (err) { setError(err.response?.data?.detail || err.message); }
    finally { setBusy(null); }
  };

  return (
    <div>
      <div className="admin-toolbar">
        <h4>Clone Audit Trail {loading && <span className="admin-muted">· loading…</span>}</h4>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button className="admin-btn-ghost" onClick={reload}>Refresh</button>
          <button className="admin-btn-primary" onClick={downloadCsv(csvUrl, 'clone_audit.csv')}><Download size={14} /> Export CSV</button>
        </div>
      </div>
      <div style={_filterBar}>
        <FilterLabel label="Requested by">
          <input style={_fs} placeholder="partial match" value={draft.username}
            onChange={e => setDraft(d => ({ ...d, username: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="Source">
          <input style={_fs} placeholder="partial match" value={draft.source}
            onChange={e => setDraft(d => ({ ...d, source: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="Target">
          <input style={_fs} placeholder="partial match" value={draft.target}
            onChange={e => setDraft(d => ({ ...d, target: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="Guard">
          <select style={_fs} value={draft.guard_status}
            onChange={e => setDraft(d => ({ ...d, guard_status: e.target.value }))}>
            <option value="">All</option>
            <option value="passed">Passed</option>
            <option value="blocked">Blocked</option>
            <option value="overridden">Overridden</option>
          </select>
        </FilterLabel>
        <FilterLabel label="Status">
          <select style={_fs} value={draft.status}
            onChange={e => setDraft(d => ({ ...d, status: e.target.value }))}>
            <option value="">All</option>
            <option value="pending">Pending</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
            <option value="rejected">Rejected</option>
          </select>
        </FilterLabel>
        <FilterLabel label="From">
          <input style={_fs} type="datetime-local" value={draft.date_from}
            onChange={e => setDraft(d => ({ ...d, date_from: e.target.value }))} />
        </FilterLabel>
        <FilterLabel label="To">
          <input style={_fs} type="datetime-local" value={draft.date_to}
            onChange={e => setDraft(d => ({ ...d, date_to: e.target.value }))} />
        </FilterLabel>
        <button className="admin-btn-primary" style={{ alignSelf: 'flex-end' }} onClick={apply}>Search</button>
        <button className="admin-btn-ghost" style={{ alignSelf: 'flex-end' }} onClick={reset}>Reset</button>
      </div>
      {error && <p className="admin-error">{error}</p>}
      <p className="admin-muted">Every clone attempt — including blocked targets and administrator overrides.
        A blocked clone needs override approval by a <strong>different</strong> Admin/DBA than the requester (separation of duties).</p>
      <table className="admin-table">
        <thead><tr><th>When</th><th>By</th><th>Source → Target</th><th>Guard</th><th>Reasons / Override</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {rows.map(r => {
            const isOwn = r.requested_by_id && currentUser.id && r.requested_by_id === currentUser.id;
            const pending = r.guard_status === 'blocked' && r.status !== 'rejected';
            return (
            <tr key={r.id}>
              <td className="admin-muted">{r.created_at ? new Date(r.created_at).toLocaleString() : '—'}</td>
              <td>{r.requested_by || '—'}</td>
              <td>{r.source || '?'} → <strong>{r.target || '?'}</strong> <span className="admin-muted">({r.target_sid})</span></td>
              <td>{badge(r.guard_status)}</td>
              <td style={{ maxWidth: 360 }}>
                {r.guard_status === 'overridden' && (
                  <div className="admin-muted">override by <strong>{r.overridden_by}</strong>: "{r.override_reason}"</div>
                )}
                {(r.guard_reasons || []).slice(0, 3).map((x, i) => (
                  <div key={i} className="admin-muted" style={{ fontSize: '0.74rem' }}>• {x}</div>
                ))}
              </td>
              <td>{r.status}</td>
              <td className="admin-actions">
                {pending && (
                  isOwn
                    ? <span className="admin-muted" style={{ fontSize: '0.72rem' }} title="You requested this clone — a different Admin/DBA must approve.">awaits other approver</span>
                    : <>
                        <button className="admin-approve" disabled={busy === r.id} onClick={() => approve(r)}>✓ Override</button>
                        <button className="admin-reject" disabled={busy === r.id} onClick={() => reject(r)}>✗</button>
                      </>
                )}
              </td>
            </tr>
          );})}
          {rows.length === 0 && !loading && <tr><td colSpan={7} className="admin-muted">No clone activity recorded.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}


/* ── Authentication / SSO ───────────────────────────────────────────────────── */

function SsoTab() {
  const [form, setForm] = useState(null);
  const [error, setError] = useState('');
  const [msg, setMsg] = useState('');
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testRes, setTestRes] = useState(null);

  const runTest = async () => {
    setTesting(true); setTestRes(null); setError('');
    try { setTestRes((await api.post('/admin/sso/test')).data); }
    catch (err) { setTestRes({ ok: false, message: err.response?.data?.detail || err.message }); }
    finally { setTesting(false); }
  };

  useEffect(() => {
    api.get('/admin/sso')
      .then(r => setForm({ ...r.data, client_secret: '' }))
      .catch(e => setError(e.response?.data?.detail || e.message));
  }, []);

  const save = async (e) => {
    e.preventDefault();
    setError(''); setMsg(''); setSaving(true);
    const body = {
      enabled: form.enabled,
      signup_enabled: form.signup_enabled,
      tenant_id: form.tenant_id,
      client_id: form.client_id,
      redirect_uri: form.redirect_uri,
      auto_provision: form.auto_provision,
    };
    if (form.client_secret) body.client_secret = form.client_secret;
    try {
      const r = await api.put('/admin/sso', body);
      setForm({ ...r.data, client_secret: '' });
      setMsg('Saved.');
    } catch (err) {
      setError(err.response?.data?.detail || err.message);
    } finally {
      setSaving(false);
    }
  };

  if (!form) return <p className="admin-muted">Loading…</p>;

  return (
    <div>
      <div className="admin-toolbar">
        <h4>Single Sign-On — Microsoft Entra ID (OIDC)</h4>
      </div>
      {error && <p className="admin-error">{error}</p>}
      {msg && <p className="admin-test ok">{msg}</p>}
      <p className="admin-muted">
        Users authenticate with Microsoft and are auto-provisioned on first login.
        The client secret is encrypted at rest. Register the redirect URI below in your Entra app registration.
      </p>

      <form onSubmit={save} style={{ maxWidth: 560 }}>
        <label className="admin-check" style={{ margin: '0.6rem 0 0.2rem' }}>
          <input type="checkbox" checked={!!form.signup_enabled}
                 onChange={e => setForm({ ...form, signup_enabled: e.target.checked })} />
          Allow public self sign-up (show the "Sign up" option on the login screen)
        </label>
        <div style={{ height: '0.5rem' }} />
        <label className="admin-check" style={{ margin: '0.6rem 0' }}>
          <input type="checkbox" checked={!!form.enabled}
                 onChange={e => setForm({ ...form, enabled: e.target.checked })} />
          Enable SSO login
        </label>

        <Field label="Tenant ID (Directory ID)">
          <input value={form.tenant_id || ''} placeholder="e.g. 00000000-0000-0000-0000-000000000000"
                 onChange={e => setForm({ ...form, tenant_id: e.target.value })} />
        </Field>
        <Field label="Client ID (Application ID)">
          <input value={form.client_id || ''} placeholder="e.g. 11111111-1111-1111-1111-111111111111"
                 onChange={e => setForm({ ...form, client_id: e.target.value })} />
        </Field>
        <Field label="Client Secret">
          <input type="password" value={form.client_secret || ''}
                 placeholder={form.has_client_secret ? '•••• set (leave blank to keep)' : 'paste secret value'}
                 onChange={e => setForm({ ...form, client_secret: e.target.value })} />
        </Field>
        <Field label="Redirect URI (register this in Entra)">
          <input value={form.redirect_uri || ''} placeholder="http://localhost:8000/auth/sso/callback"
                 onChange={e => setForm({ ...form, redirect_uri: e.target.value })} />
        </Field>
        <label className="admin-check" style={{ margin: '0.6rem 0' }}>
          <input type="checkbox" checked={!!form.auto_provision}
                 onChange={e => setForm({ ...form, auto_provision: e.target.checked })} />
          Auto-provision new users on first login
        </label>

        <div className="admin-form-footer" style={{ borderTop: 'none', paddingLeft: 0, gap: '0.5rem' }}>
          <button type="submit" className="admin-btn-primary" disabled={saving}>
            {saving ? 'Saving…' : 'Save SSO Settings'}
          </button>
          <button type="button" className="admin-btn-ghost" disabled={testing} onClick={runTest}>
            {testing ? 'Testing…' : '⚡ Test connection'}
          </button>
        </div>
        <p className="admin-muted" style={{ fontSize: '0.72rem', marginTop: '0.3rem' }}>
          Test validates the <strong>saved</strong> tenant + app credentials against Microsoft Entra ID. Save first if you just edited them.
        </p>
        <TestResult result={testRes} />
      </form>
    </div>
  );
}


/* ── Training (local models only) ───────────────────────────────────────────── */

const TRAIN_AGENTS = ['chat', 'deployment', 'performance', 'cloning', 'knowledge_base'];
const TRAIN_SOURCES = [
  { key: 'feedback', label: 'Feedback (RLHF + RLAIF)' },
  { key: 'rag', label: 'RAG documents → Q&A' },
  { key: 'agent_run', label: 'Accepted agent runs' },
];

function TrainingTab() {
  const [agent, setAgent] = useState('chat');
  const [ov, setOv] = useState(null);
  const [bases, setBases] = useState([]);
  const [counts, setCounts] = useState(null);
  const [sources, setSources] = useState(['feedback']);
  const [baseModel, setBaseModel] = useState('');
  const [method, setMethod] = useState('both');
  const [tag, setTag] = useState('');
  const [jobs, setJobs] = useState([]);
  const [error, setError] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  const loadOverview = async () => {
    try { setOv((await api.get('/training/overview')).data); } catch (e) { setError(e.response?.data?.detail || e.message); }
  };
  const loadCounts = async (a) => {
    try { setCounts((await api.get('/training/examples', { params: { agent: a, limit: 1 } })).data.counts); }
    catch { setCounts(null); }
  };
  const loadJobs = async () => { try { setJobs((await api.get('/training/jobs')).data); } catch { /* */ } };

  useEffect(() => {
    loadOverview(); loadJobs();
    api.get('/training/base-models').then(r => setBases(r.data.models || [])).catch(() => {});
  }, []);
  useEffect(() => { loadCounts(agent); setTag(`oraebs-${agent}:v1`); }, [agent]);

  const toggleSource = (k) => setSources(s => s.includes(k) ? s.filter(x => x !== k) : [...s, k]);

  const assemble = async () => {
    setBusy(true); setError(''); setMsg('');
    try {
      const r = (await api.post('/training/assemble', { agent, sources })).data;
      setCounts(r.counts);
      setMsg(`Added ${r.added} examples (${Object.entries(r.by_source).filter(([, n]) => n).map(([k, n]) => `${k}: ${n}`).join(', ') || 'none new'}).`);
      loadOverview();
    } catch (e) { setError(e.response?.data?.detail || e.message); } finally { setBusy(false); }
  };

  const uploadJsonl = async (e) => {
    const f = e.target.files?.[0]; if (!f) return;
    setBusy(true); setError(''); setMsg('');
    try {
      const fd = new FormData(); fd.append('agent', agent); fd.append('file', f);
      const r = (await api.post('/training/examples/upload', fd)).data;
      setCounts(r.counts); setMsg(`Uploaded ${r.added} examples (${r.skipped} skipped).`); loadOverview();
    } catch (err) { setError(err.response?.data?.detail || err.message); }
    finally { setBusy(false); e.target.value = ''; }
  };

  const createJob = async () => {
    setBusy(true); setError(''); setMsg('');
    try {
      await api.post('/training/jobs', { agent, base_model: baseModel || bases[0] || 'llama3.2:1b', method, target_model_tag: tag });
      setMsg('Training job created — download the bundle to run it on a GPU host.'); loadJobs();
    } catch (e) { setError(e.response?.data?.detail || e.message); } finally { setBusy(false); }
  };

  const downloadBundle = async (j) => {
    try {
      const res = await api.get(`/training/jobs/${j.id}/bundle`, { responseType: 'blob' });
      const url = URL.createObjectURL(new Blob([res.data], { type: 'application/zip' }));
      const a = document.createElement('a'); a.href = url; a.download = `oraebs_train_${j.id}_${j.agent}.zip`;
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url); loadJobs();
    } catch (e) { setError(e.response?.data?.detail || e.message); }
  };

  const registerJob = async (j) => {
    const path = window.prompt(`Register the trained GGUF as Ollama model "${j.target_model_tag}".\n` +
      `Enter the GGUF path readable by the Ollama host (e.g. /root/.ollama/models/model.gguf):`);
    if (!path) return;
    setBusy(true); setError(''); setMsg('');
    try {
      const r = (await api.post(`/training/jobs/${j.id}/register`, { gguf_path: path })).data;
      setMsg(`${r.message} — agent '${r.agent}' now uses ${r.model}.`); loadJobs(); loadOverview();
    } catch (e) { setError(e.response?.data?.detail || e.message); } finally { setBusy(false); }
  };

  const clearAgentModel = async (a) => {
    if (!window.confirm(`Reset ${a} back to the default model?`)) return;
    try { await api.delete(`/training/agent-models/${a}`); loadOverview(); } catch (e) { setError(e.response?.data?.detail || e.message); }
  };

  const jobBadge = (s) =>
    s === 'registered' ? <span className="admin-pill ok">registered</span>
      : s === 'bundled' ? <span className="admin-pill admin">bundled</span>
        : s === 'failed' ? <span className="admin-pill off">failed</span>
          : <span className="admin-pill">draft</span>;

  return (
    <div>
      <div className="admin-toolbar"><h4>Local Model Training <span className="admin-muted">· Ollama only</span></h4></div>
      {error && <p className="admin-error">{error}</p>}
      {msg && <p className="admin-test ok">{msg}</p>}
      <p className="admin-muted">Fine-tune a <strong>local</strong> model per agent with your own data (SFT → DPO). Assemble a
        dataset, download a runnable LoRA bundle to train on a GPU host, then register the resulting GGUF and map it to the agent.
        Cloud providers (OpenAI/Anthropic/Gemini) cannot be trained here.</p>

      {/* per-agent overview */}
      <table className="admin-table">
        <thead><tr><th>Agent</th><th>SFT</th><th>DPO</th><th>Sources</th><th>Active model</th><th></th></tr></thead>
        <tbody>
          {(ov?.agents || []).map(a => (
            <tr key={a.agent}>
              <td><strong>{a.agent}</strong></td>
              <td>{a.counts.sft}</td>
              <td>{a.counts.dpo}</td>
              <td className="admin-muted" style={{ fontSize: '0.74rem' }}>
                {Object.entries(a.counts.by_source || {}).map(([k, n]) => `${k}:${n}`).join(' · ') || '—'}</td>
              <td>{a.active_model ? <span className="admin-pill ok">{a.active_model.model}</span> : <span className="admin-muted">default</span>}</td>
              <td className="admin-actions">
                {a.active_model && <button onClick={() => clearAgentModel(a.agent)} title="Reset to default"><Trash2 size={14} /></button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* dataset builder */}
      <h4 style={{ margin: '1.2rem 0 0.4rem' }}>1 · Build dataset</h4>
      <div className="admin-row" style={{ alignItems: 'flex-end', gap: '1rem', flexWrap: 'wrap' }}>
        <Field label="Agent">
          <select value={agent} onChange={e => setAgent(e.target.value)}>
            {TRAIN_AGENTS.map(a => <option key={a} value={a}>{a}</option>)}
          </select>
        </Field>
        <div className="admin-field"><span>Sources</span>
          <div style={{ display: 'flex', gap: '0.8rem', flexWrap: 'wrap', paddingTop: 4 }}>
            {TRAIN_SOURCES.map(s => (
              <label key={s.key} className="admin-check" style={{ margin: 0 }}>
                <input type="checkbox" checked={sources.includes(s.key)} onChange={() => toggleSource(s.key)} /> {s.label}
              </label>
            ))}
          </div>
        </div>
      </div>
      <div style={{ display: 'flex', gap: '0.6rem', alignItems: 'center', marginTop: '0.6rem', flexWrap: 'wrap' }}>
        <button className="admin-btn-primary" disabled={busy || !sources.length} onClick={assemble}>
          <Plus size={14} /> Assemble from sources
        </button>
        <label className="admin-btn-ghost" style={{ cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <Upload size={14} /> Upload JSONL
          <input type="file" accept=".jsonl,.json,.txt" style={{ display: 'none' }} onChange={uploadJsonl} />
        </label>
        {counts && <span className="admin-muted">Dataset for <strong>{agent}</strong>: {counts.sft} SFT · {counts.dpo} DPO ({counts.total} total)</span>}
      </div>

      {/* job */}
      <h4 style={{ margin: '1.2rem 0 0.4rem' }}>2 · Create training job</h4>
      <div className="admin-row" style={{ alignItems: 'flex-end', gap: '1rem', flexWrap: 'wrap' }}>
        <Field label="Base model (local Ollama)">
          <select value={baseModel} onChange={e => setBaseModel(e.target.value)}>
            {bases.length === 0 && <option value="">(no Ollama models found)</option>}
            {bases.map(b => <option key={b} value={b}>{b}</option>)}
          </select>
        </Field>
        <Field label="Method">
          <select value={method} onChange={e => setMethod(e.target.value)}>
            <option value="both">SFT → DPO (both)</option>
            <option value="sft">SFT only</option>
            <option value="dpo">DPO only</option>
          </select>
        </Field>
        <Field label="Target model tag"><input value={tag} onChange={e => setTag(e.target.value)} /></Field>
        <button className="admin-btn-primary" disabled={busy} onClick={createJob}><Plus size={14} /> Create job</button>
      </div>

      {/* jobs */}
      <h4 style={{ margin: '1.2rem 0 0.4rem' }}>3 · Jobs — download bundle, train on GPU, then register</h4>
      <table className="admin-table">
        <thead><tr><th>#</th><th>Agent</th><th>Base → Tag</th><th>Method</th><th>SFT/DPO</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {jobs.map(j => (
            <tr key={j.id}>
              <td>{j.id}</td>
              <td>{j.agent}</td>
              <td className="admin-muted" style={{ fontSize: '0.78rem' }}>{j.base_model} → <strong>{j.target_model_tag}</strong></td>
              <td>{j.method}</td>
              <td>{j.sft_count}/{j.dpo_count}</td>
              <td>{jobBadge(j.status)}</td>
              <td className="admin-actions">
                <button onClick={() => downloadBundle(j)} title="Download training bundle"><Download size={14} /></button>
                <button onClick={() => registerJob(j)} title="Register trained GGUF in Ollama">⬆︎</button>
              </td>
            </tr>
          ))}
          {jobs.length === 0 && <tr><td colSpan={7} className="admin-muted">No training jobs yet.</td></tr>}
        </tbody>
      </table>
      <p className="admin-muted" style={{ fontSize: '0.74rem', marginTop: '0.5rem' }}>
        The bundle contains <code>sft.jsonl</code> / <code>dpo.jsonl</code>, a LoRA <code>train.py</code> and an Ollama
        <code> Modelfile</code>. Run it on a GPU host, convert to GGUF, then register — the agent switches to the fine-tuned
        model automatically (Ollama provider only).
      </p>
    </div>
  );
}


/* ── Form modal shell ───────────────────────────────────────────────────────── */

function FormModal({ title, onClose, onSubmit, children }) {
  return (
    <div className="admin-form-overlay" onMouseDown={onClose}>
      <form className="admin-form" onSubmit={onSubmit} onMouseDown={e => e.stopPropagation()}>
        <div className="admin-form-header">
          <h4>{title}</h4>
          <button type="button" className="admin-close" onClick={onClose}><X size={16} /></button>
        </div>
        <div className="admin-form-body">{children}</div>
        <div className="admin-form-footer">
          <button type="button" className="admin-btn-ghost" onClick={onClose}>Cancel</button>
          <button type="submit" className="admin-btn-primary">Save</button>
        </div>
      </form>
    </div>
  );
}
