import React, { useState } from 'react';
import { authApi } from '../api';
import type { User } from '../types';

interface AuthPageProps {
  onAuthSuccess: (user: User) => void;
}

export const AuthPage: React.FC<AuthPageProps> = ({ onAuthSuccess }) => {
  const [isRegister, setIsRegister] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    if (!email || !password) {
      setError('Будь ласка, заповніть усі поля.');
      return;
    }

    if (isRegister && password.length < 6) {
      setError('Пароль повинен містити щонайменше 6 символів.');
      return;
    }

    setLoading(true);
    try {
      const user = isRegister
        ? await authApi.register(email, password)
        : await authApi.login(email, password);
      onAuthSuccess(user);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      if (typeof detail === 'string') {
        setError(detail);
      } else if (Array.isArray(detail)) {
        setError(detail.map((d: any) => d.msg).join(', '));
      } else {
        setError('Помилка автентифікації. Перевірте введені дані.');
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="container" style={{ maxWidth: '480px', marginTop: '3rem' }}>
      <article className="card-container">
        <header style={{ textAlign: 'center', marginBottom: '1.5rem' }}>
          <div className="brand-icon" style={{ margin: '0 auto 1rem', width: '48px', height: '48px', fontSize: '1.5rem' }}>
            ▶
          </div>
          <h2>{isRegister ? 'Реєстрація' : 'Вхід у систему'}</h2>
          <p style={{ color: '#94a3b8', fontSize: '0.9rem' }}>
            {isRegister
              ? 'Створіть обліковий запис для обробки відео'
              : 'Увійдіть для доступу до хмарного відеопроцесора'}
          </p>
        </header>

        {error && (
          <div className="file-meta-box error" style={{ marginBottom: '1.25rem' }}>
            ⚠️ {error}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <label htmlFor="auth-email">
            Електронна пошта
            <input
              id="auth-email"
              type="email"
              placeholder="user@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              disabled={loading}
            />
          </label>

          <label htmlFor="auth-password">
            Пароль
            <input
              id="auth-password"
              type="password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              disabled={loading}
            />
          </label>

          <button type="submit" aria-busy={loading} disabled={loading} style={{ width: '100%', marginTop: '1rem' }}>
            {loading ? 'Зачекайте...' : isRegister ? 'Зареєструватися' : 'Увійти'}
          </button>
        </form>

        <footer style={{ textAlign: 'center', marginTop: '1.5rem', borderTop: '1px solid rgba(255,255,255,0.08)', paddingTop: '1rem' }}>
          <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>
            {isRegister ? 'Вже маєте обліковий запис?' : 'Ще не маєте акаунту?'}
          </span>{' '}
          <a
            href="#"
            style={{ fontSize: '0.85rem', fontWeight: 600, color: '#38bdf8' }}
            onClick={(e) => {
              e.preventDefault();
              setIsRegister(!isRegister);
              setError(null);
            }}
          >
            {isRegister ? 'Увійти' : 'Зареєструватися'}
          </a>
        </footer>
      </article>
    </div>
  );
};
