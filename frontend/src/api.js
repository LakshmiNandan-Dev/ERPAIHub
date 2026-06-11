import axios from 'axios';

// Resolve the API base URL. Default to the same host the browser loaded the app
// from, on the API port (8000) — so remote-server deployments work without the
// hardcoded 127.0.0.1. Override at build time with VITE_API_URL (e.g. behind a
// reverse proxy or custom domain/port).
const API_BASE_URL =
  import.meta.env.VITE_API_URL ||
  `${window.location.protocol}//${window.location.hostname}:8000`;

const api = axios.create({
  baseURL: API_BASE_URL,
});

// Add a request interceptor to attach auth token + LLM provider config
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('session_token');
    if (token) {
      config.headers['Authorization'] = `Bearer ${token}`;
    }

    // Attach LLM provider selection so the backend uses the chosen model.
    // The API key itself is NOT sent from the browser — it is resolved
    // server-side from the admin-managed (encrypted) credential store.
    try {
      const llm = JSON.parse(localStorage.getItem('llm_config') || '{}');
      if (llm.provider) config.headers['X-LLM-Provider'] = llm.provider;
      if (llm.model)    config.headers['X-LLM-Model']    = llm.model;
      if (llm.base_url) config.headers['X-LLM-Base-Url'] = llm.base_url;
    } catch { /* ignore */ }

    return config;
  },
  (error) => Promise.reject(error)
);

// Add a response interceptor to handle 401 Unauthorized errors automatically (e.g. database resets)
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response && error.response.status === 401) {
      localStorage.removeItem('session_token');
      localStorage.removeItem('user');
      window.location.href = '/auth';
    }
    return Promise.reject(error);
  }
);

export default api;
