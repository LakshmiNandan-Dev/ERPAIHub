import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../api';
import './Auth.css';

export default function Auth({ setAuthToken }) {
  const [isLogin, setIsLogin] = useState(true);
  const [formData, setFormData] = useState({ username: '', email: '', password: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleInputChange = (e) => {
    setFormData({ ...formData, [e.target.name]: e.target.value });
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (isLogin) {
        const response = await api.post('/auth/login', {
          username: formData.username,
          password: formData.password
        });
        const { session_token, user } = response.data;
        localStorage.setItem('session_token', session_token);
        localStorage.setItem('user', JSON.stringify(user));
        setAuthToken(session_token);
        navigate('/');
      } else {
        await api.post('/auth/register', formData);
        setIsLogin(true);
        setError('Registration successful! Please log in.');
      }
    } catch (err) {
      setError(err.response?.data?.detail || 'An error occurred. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-container">
      <div className="auth-card glass">
        <div className="auth-header">
          <h2>{isLogin ? 'Welcome Back' : 'Create Account'}</h2>
          <p>{isLogin ? 'Enter your details to access your agents.' : 'Sign up to start building your AI workforce.'}</p>
        </div>

        {error && <div className={`auth-alert ${isLogin && error.includes('successful') ? 'success' : 'error'}`}>{error}</div>}

        <form onSubmit={handleSubmit} className="auth-form">
          <div className="form-group">
            <label>Username</label>
            <input 
              type="text" 
              name="username" 
              value={formData.username} 
              onChange={handleInputChange} 
              placeholder="e.g. jdoe" 
              required 
            />
          </div>

          {!isLogin && (
            <div className="form-group">
              <label>Email</label>
              <input 
                type="email" 
                name="email" 
                value={formData.email} 
                onChange={handleInputChange} 
                placeholder="e.g. jdoe@example.com" 
                required 
              />
            </div>
          )}

          <div className="form-group">
            <label>Password</label>
            <input 
              type="password" 
              name="password" 
              value={formData.password} 
              onChange={handleInputChange} 
              placeholder="••••••••" 
              required 
            />
          </div>

          <button type="submit" className="btn-primary w-full" disabled={loading}>
            {loading ? 'Processing...' : (isLogin ? 'Sign In' : 'Sign Up')}
          </button>
        </form>

        <div className="auth-footer">
          <p>
            {isLogin ? "Don't have an account? " : "Already have an account? "}
            <span className="auth-link" onClick={() => {setIsLogin(!isLogin); setError('');}}>
              {isLogin ? 'Sign up' : 'Log in'}
            </span>
          </p>
        </div>
      </div>
    </div>
  );
}
