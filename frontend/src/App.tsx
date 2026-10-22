import { useEffect, useState } from 'react';
import { authApi } from './api';
import type { User } from './types';
import { Navbar } from './components/Navbar';
import { AuthPage } from './pages/AuthPage';
import { TaskListPage } from './pages/TaskListPage';
import { NewTaskPage } from './pages/NewTaskPage';

export function App() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [activeTab, setActiveTab] = useState<'tasks' | 'new-task'>('tasks');

  useEffect(() => {
    // Check initial session
    authApi
      .getMe()
      .then((me) => setUser(me))
      .catch(() => setUser(null))
      .finally(() => setLoading(false));

    // Handle 401 Unauthorized interceptor event
    const handleUnauthorized = () => {
      setUser(null);
    };

    window.addEventListener('auth:unauthorized', handleUnauthorized);
    return () => {
      window.removeEventListener('auth:unauthorized', handleUnauthorized);
    };
  }, []);

  const handleLogout = async () => {
    try {
      await authApi.logout();
    } catch (e) {
      console.error('Logout error', e);
    } finally {
      setUser(null);
    }
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', minHeight: '100vh', justifyContent: 'center', alignItems: 'center' }}>
        <article aria-busy="true">Завантаження сервісу...</article>
      </div>
    );
  }

  if (!user) {
    return <AuthPage onAuthSuccess={(authenticatedUser) => setUser(authenticatedUser)} />;
  }

  return (
    <div>
      <Navbar
        user={user}
        activeTab={activeTab}
        setActiveTab={setActiveTab}
        onLogout={handleLogout}
      />
      <main>
        {activeTab === 'tasks' && (
          <TaskListPage onGoToNewTask={() => setActiveTab('new-task')} />
        )}
        {activeTab === 'new-task' && (
          <NewTaskPage onTaskCreated={() => setActiveTab('tasks')} />
        )}
      </main>
    </div>
  );
}

export default App;
