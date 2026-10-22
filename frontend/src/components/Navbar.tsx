import React from 'react';
import type { User } from '../types';

interface NavbarProps {
  user: User | null;
  activeTab: 'tasks' | 'new-task' | 'admin';
  setActiveTab: (tab: 'tasks' | 'new-task' | 'admin') => void;
  onLogout: () => void;
}

export const Navbar: React.FC<NavbarProps> = ({
  user,
  activeTab,
  setActiveTab,
  onLogout,
}) => {
  return (
    <header className="app-header">
      <div className="container" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <a href="#" className="brand-container" onClick={(e) => { e.preventDefault(); setActiveTab('tasks'); }}>
          <div className="brand-icon">▶</div>
          <span>Cloud Video Processor</span>
        </a>

        {user && (
          <nav className="nav-links">
            <button
              className={`nav-btn ${activeTab === 'tasks' ? 'active' : ''}`}
              onClick={() => setActiveTab('tasks')}
            >
              📋 Мої задачі
            </button>
            <button
              className={`nav-btn ${activeTab === 'new-task' ? 'active' : ''}`}
              onClick={() => setActiveTab('new-task')}
            >
              ➕ Нова задача
            </button>
            {user.is_admin && (
              <button
                className={`nav-btn ${activeTab === 'admin' ? 'active' : ''}`}
                style={{
                  borderColor: 'rgba(239, 68, 68, 0.4)',
                  color: activeTab === 'admin' ? '#fff' : '#f87171',
                  background: activeTab === 'admin' ? 'rgba(239, 68, 68, 0.2)' : undefined,
                }}
                onClick={() => setActiveTab('admin')}
              >
                ⚡ Адмін-панель
              </button>
            )}
            <div className="user-tag">
              <span>{user.email}</span>
              {user.is_admin && <span className="admin-badge">Admin</span>}
            </div>
            <button className="nav-btn" onClick={onLogout} title="Вийти з акаунту">
              🚪 Вийти
            </button>
          </nav>
        )}
      </div>
    </header>
  );
};
