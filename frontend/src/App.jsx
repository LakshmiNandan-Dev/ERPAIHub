import { useState, useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import Auth from './components/Auth';
import ChatLayout from './components/Chat/ChatLayout';

function App() {
  // Capture the SSO callback (?sso_token / ?sso_error) on first load, then
  // clean the URL so the params don't linger.
  const [authToken, setAuthToken] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    const ssoToken = params.get('sso_token');
    const ssoError = params.get('sso_error');
    if (ssoToken) localStorage.setItem('session_token', ssoToken);
    if (ssoError) sessionStorage.setItem('sso_error', ssoError);
    if (ssoToken || ssoError) window.history.replaceState({}, '', '/');
    return localStorage.getItem('session_token');
  });

  useEffect(() => {
    // Sync state if token changes in local storage
    const token = localStorage.getItem('session_token');
    if (token !== authToken) {
      setAuthToken(token);
    }
  }, []);

  return (
    <Router>
      <Routes>
        <Route 
          path="/auth" 
          element={!authToken ? <Auth setAuthToken={setAuthToken} /> : <Navigate to="/" />} 
        />
        <Route 
          path="/" 
          element={authToken ? <ChatLayout setAuthToken={setAuthToken} /> : <Navigate to="/auth" />} 
        />
      </Routes>
    </Router>
  );
}

export default App;
