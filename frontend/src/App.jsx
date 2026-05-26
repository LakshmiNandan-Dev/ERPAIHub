import { useState, useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import Auth from './components/Auth';
import ChatLayout from './components/Chat/ChatLayout';

function App() {
  const [authToken, setAuthToken] = useState(localStorage.getItem('session_token'));

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
