import axios from 'axios';

const api = axios.create({
  baseURL: 'http://127.0.0.1:8000',
});

// Add a request interceptor to attach auth token + LLM provider config
api.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('session_token');
    if (token) {
      config.headers['Authorization'] = `Bearer ${token}`;
    }

    // Attach LLM provider config so the backend uses the user's chosen model
    try {
      const llm = JSON.parse(localStorage.getItem('llm_config') || '{}');
      if (llm.provider) config.headers['X-LLM-Provider'] = llm.provider;
      if (llm.model)    config.headers['X-LLM-Model']    = llm.model;
      if (llm.api_key)  config.headers['X-LLM-Api-Key']  = llm.api_key;
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
