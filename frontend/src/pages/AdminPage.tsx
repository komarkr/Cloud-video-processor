import React, { useEffect, useState } from 'react';
import { adminApi } from '../api';
import type { AdminOverview, User } from '../types';

interface AdminPageProps {
  user: User;
  onGoBack: () => void;
}

export const AdminPage: React.FC<AdminPageProps> = ({ user, onGoBack }) => {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);

  const fetchOverview = async (showLoading = false) => {
    if (showLoading) setLoading(true);
    try {
      const data = await adminApi.getOverview();
      setOverview(data);
      setError(null);
    } catch (err: any) {
      if (err.response?.status === 403) {
        setError('403 Forbidden: У вас немає прав адміністратора для перегляду цієї сторінки.');
      } else {
        setError('Не вдалося оновити стан кластера. Перевірте зʼєднання з сервером.');
      }
    } finally {
      if (showLoading) setLoading(false);
    }
  };

  useEffect(() => {
    fetchOverview(true);

    const interval = setInterval(() => {
      fetchOverview(false);
    }, 3000);

    return () => clearInterval(interval);
  }, []);

  const handleCancelTask = async (taskId: string) => {
    setCancellingId(taskId);
    setActionMsg(null);
    try {
      await adminApi.cancelTask(taskId);
      setActionMsg(`Задачу ${taskId.slice(0, 8)}... успішно скасовано.`);
      await fetchOverview(false);
    } catch (err: any) {
      setActionMsg(`Помилка скасування задачі: ${err.response?.data?.detail || err.message}`);
    } finally {
      setCancellingId(null);
    }
  };

  if (!user.is_admin) {
    return (
      <div className="container" style={{ padding: '3rem 1rem', maxWidth: '600px' }}>
        <article className="card-container" style={{ textAlign: 'center', borderColor: 'rgba(239, 68, 68, 0.4)' }}>
          <h2 style={{ color: '#f87171' }}>403 Доступ заборонено</h2>
          <p style={{ color: '#94a3b8', marginBottom: '1.5rem' }}>
            Ця сторінка доступна виключно для користувачів із правами адміністратора системи.
          </p>
          <button onClick={onGoBack} className="nav-btn active">
            Повернутися до моїх задач
          </button>
        </article>
      </div>
    );
  }

  const getMetricColor = (val: number) => {
    if (val > 80) return '#ef4444';
    if (val > 50) return '#f59e0b';
    return '#10b981';
  };

  const formatDate = (dateStr?: string | null) => {
    if (!dateStr) return '—';
    try {
      const d = new Date(dateStr);
      return d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch {
      return dateStr;
    }
  };

  return (
    <div className="container" style={{ padding: '1.5rem 1rem' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '1.5rem', flexWrap: 'wrap', gap: '1rem' }}>
        <div>
          <h2 style={{ margin: 0, display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
            <span>Адмін-панель кластера</span>
            <span style={{ fontSize: '0.75rem', background: 'rgba(56, 189, 248, 0.15)', color: '#38bdf8', padding: '0.2rem 0.6rem', borderRadius: '999px', border: '1px solid rgba(56, 189, 248, 0.3)' }}>
              Live 3s
            </span>
          </h2>
          <p style={{ color: '#94a3b8', margin: '0.3rem 0 0', fontSize: '0.9rem' }}>
            Моніторинг воркерів, черги задач, навантаження CPU/RAM та подій автомасштабування.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '0.6rem' }}>
          <button className="nav-btn" onClick={() => fetchOverview(false)} title="Оновити дані негайно">
            Оновити
          </button>
          <button className="nav-btn" onClick={onGoBack}>
            ← До моїх задач
          </button>
        </div>
      </div>

      {error && (
        <div className="file-meta-box error" style={{ marginBottom: '1.5rem' }}>
          {error}
        </div>
      )}

      {actionMsg && (
        <div className="file-meta-box" style={{ marginBottom: '1.5rem', borderColor: 'rgba(56, 189, 248, 0.4)', color: '#38bdf8' }}>
          {actionMsg}
        </div>
      )}

      {loading && !overview ? (
        <article className="card-container" style={{ textAlign: 'center', padding: '3rem' }}>
          <span aria-busy="true">Завантаження статистики кластера...</span>
        </article>
      ) : overview ? (
        <>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: '1rem', marginBottom: '1.5rem' }}>
            <div className="card-container" style={{ padding: '1rem', textAlign: 'center' }}>
              <span style={{ fontSize: '0.8rem', color: '#94a3b8' }}>Всього задач</span>
              <div style={{ fontSize: '1.8rem', fontWeight: 700, color: '#f8fafc' }}>
                {overview.status_counts.total}
              </div>
            </div>
            <div className="card-container" style={{ padding: '1rem', textAlign: 'center', borderColor: overview.queue_length > 0 ? 'rgba(245, 158, 11, 0.5)' : undefined }}>
              <span style={{ fontSize: '0.8rem', color: '#fbbf24' }}>У черзі</span>
              <div style={{ fontSize: '1.8rem', fontWeight: 700, color: '#fbbf24' }}>
                {overview.queue_length}
              </div>
            </div>
            <div className="card-container" style={{ padding: '1rem', textAlign: 'center' }}>
              <span style={{ fontSize: '0.8rem', color: '#38bdf8' }}>Виконуються</span>
              <div style={{ fontSize: '1.8rem', fontWeight: 700, color: '#38bdf8' }}>
                {overview.status_counts.running}
              </div>
            </div>
            <div className="card-container" style={{ padding: '1rem', textAlign: 'center' }}>
              <span style={{ fontSize: '0.8rem', color: '#4ade80' }}>Готово</span>
              <div style={{ fontSize: '1.8rem', fontWeight: 700, color: '#4ade80' }}>
                {overview.status_counts.done}
              </div>
            </div>
            <div className="card-container" style={{ padding: '1rem', textAlign: 'center' }}>
              <span style={{ fontSize: '0.8rem', color: '#f87171' }}>Помилок</span>
              <div style={{ fontSize: '1.8rem', fontWeight: 700, color: '#f87171' }}>
                {overview.status_counts.failed}
              </div>
            </div>
            <div className="card-container" style={{ padding: '1rem', textAlign: 'center' }}>
              <span style={{ fontSize: '0.8rem', color: '#94a3b8' }}>Скасовано</span>
              <div style={{ fontSize: '1.8rem', fontWeight: 700, color: '#94a3b8' }}>
                {overview.status_counts.cancelled}
              </div>
            </div>
            <div className="card-container" style={{ padding: '1rem', textAlign: 'center', borderColor: 'rgba(168, 85, 247, 0.4)' }}>
              <span style={{ fontSize: '0.8rem', color: '#c084fc' }}>Активні воркери</span>
              <div style={{ fontSize: '1.8rem', fontWeight: 700, color: '#c084fc' }}>
                {overview.workers.length}
              </div>
            </div>
          </div>

          <article className="card-container" style={{ padding: '1.25rem', marginBottom: '1.5rem' }}>
            <h3 style={{ margin: '0 0 1rem', fontSize: '1.1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <span>Активні воркери (Heartbeat)</span>
              <span style={{ fontSize: '0.8rem', color: '#94a3b8', fontWeight: 400 }}>
                ({overview.workers.length} онлайн)
              </span>
            </h3>

            {overview.workers.length === 0 ? (
              <p style={{ color: '#94a3b8', margin: 0 }}>Немає активних сигналів heartbeat від воркерів.</p>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table role="grid" style={{ marginBottom: 0 }}>
                  <thead>
                    <tr>
                      <th scope="col">Воркер (Host)</th>
                      <th scope="col" style={{ width: '180px' }}>CPU %</th>
                      <th scope="col" style={{ width: '180px' }}>RAM %</th>
                      <th scope="col">Поточна задача</th>
                      <th scope="col">Востаннє бачили</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.workers.map((w) => (
                      <tr key={w.hostname}>
                        <td>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                            <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: '#10b981', display: 'inline-block' }} />
                            <strong>{w.hostname}</strong>
                          </div>
                        </td>
                        <td>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
                            <div style={{ flex: 1, background: 'rgba(255,255,255,0.1)', height: '8px', borderRadius: '4px', overflow: 'hidden' }}>
                              <div
                                style={{
                                  width: `${Math.min(100, Math.max(0, w.cpu))}%`,
                                  height: '100%',
                                  background: getMetricColor(w.cpu),
                                  transition: 'width 0.3s ease',
                                }}
                              />
                            </div>
                            <span style={{ fontSize: '0.85rem', fontWeight: 600, color: getMetricColor(w.cpu), width: '42px', textAlign: 'right' }}>
                              {w.cpu}%
                            </span>
                          </div>
                        </td>
                        <td>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '0.6rem' }}>
                            <div style={{ flex: 1, background: 'rgba(255,255,255,0.1)', height: '8px', borderRadius: '4px', overflow: 'hidden' }}>
                              <div
                                style={{
                                  width: `${Math.min(100, Math.max(0, w.ram))}%`,
                                  height: '100%',
                                  background: getMetricColor(w.ram),
                                  transition: 'width 0.3s ease',
                                }}
                              />
                            </div>
                            <span style={{ fontSize: '0.85rem', fontWeight: 600, color: getMetricColor(w.ram), width: '42px', textAlign: 'right' }}>
                              {w.ram}%
                            </span>
                          </div>
                        </td>
                        <td>
                          {w.current_task ? (
                            <span style={{ fontSize: '0.85rem', color: '#38bdf8' }}>
                              В процесі: {w.current_task.slice(0, 8)}...
                            </span>
                          ) : (
                            <span style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                              Вільний (idle)
                            </span>
                          )}
                        </td>
                        <td>
                          <span style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                            {w.last_seen}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </article>

          <article className="card-container" style={{ padding: '1.25rem', marginBottom: '1.5rem' }}>
            <h3 style={{ margin: '0 0 1rem', fontSize: '1.1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <span>Журнал автомасштабування (Autoscaler Log)</span>
            </h3>

            {overview.autoscaler_events.length === 0 ? (
              <p style={{ color: '#94a3b8', margin: 0 }}>Подій масштабування ще не зафіксовано.</p>
            ) : (
              <div style={{ overflowX: 'auto', maxHeight: '240px' }}>
                <table role="grid" style={{ marginBottom: 0 }}>
                  <thead>
                    <tr>
                      <th scope="col">Час</th>
                      <th scope="col">Дія</th>
                      <th scope="col">Кількість воркерів</th>
                      <th scope="col">Черга задач</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.autoscaler_events.map((ev, idx) => (
                      <tr key={idx}>
                        <td style={{ fontSize: '0.85rem', color: '#94a3b8' }}>{formatDate(ev.timestamp)}</td>
                        <td>
                          <span
                            className="status-badge"
                            style={{
                              background: ev.action.includes('scale_up') ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.15)',
                              color: ev.action.includes('scale_up') ? '#10b981' : '#f87171',
                              border: `1px solid ${ev.action.includes('scale_up') ? 'rgba(16, 185, 129, 0.3)' : 'rgba(239, 68, 68, 0.3)'}`,
                            }}
                          >
                            {ev.action.includes('scale_up') ? 'Scale Up' : 'Scale Down'}
                          </span>
                        </td>
                        <td style={{ fontWeight: 600 }}>{ev.workers} воркер(и)</td>
                        <td style={{ color: '#94a3b8' }}>{ev.queued !== undefined ? ev.queued : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </article>

          <article className="card-container" style={{ padding: '1.25rem' }}>
            <h3 style={{ margin: '0 0 1rem', fontSize: '1.1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <span>Останні 50 задач кластера</span>
            </h3>

            {overview.recent_tasks.length === 0 ? (
              <p style={{ color: '#94a3b8', margin: 0 }}>Задач ще не створено.</p>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table role="grid" style={{ marginBottom: 0 }}>
                  <thead>
                    <tr>
                      <th scope="col">Час</th>
                      <th scope="col">Користувач</th>
                      <th scope="col">Операція</th>
                      <th scope="col">Статус</th>
                      <th scope="col" style={{ width: '140px' }}>Прогрес</th>
                      <th scope="col">Воркер</th>
                      <th scope="col">Тривалість</th>
                      <th scope="col" style={{ textAlign: 'right' }}>Дії</th>
                    </tr>
                  </thead>
                  <tbody>
                    {overview.recent_tasks.map((task) => (
                      <tr key={task.id}>
                        <td>
                          <div style={{ fontSize: '0.85rem', fontWeight: 600 }}>{formatDate(task.created_at)}</div>
                          <div style={{ fontSize: '0.75rem', color: '#64748b' }}>ID: {task.id.slice(0, 8)}...</div>
                        </td>
                        <td style={{ fontSize: '0.85rem' }}>{task.user_email}</td>
                        <td style={{ fontSize: '0.85rem', fontWeight: 500 }}>{task.operation_type}</td>
                        <td>
                          <span className={`status-badge ${task.status}`}>
                            {task.status === 'queued' && 'У черзі'}
                            {task.status === 'running' && 'Обробка'}
                            {task.status === 'done' && 'Готово'}
                            {task.status === 'failed' && 'Помилка'}
                            {task.status === 'cancelled' && 'Скасовано'}
                          </span>
                        </td>
                        <td>
                          <div className="progress-wrapper">
                            <progress className="custom-progress" value={task.progress} max="100" />
                            <span className="progress-text">{task.progress}%</span>
                          </div>
                        </td>
                        <td style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                          {task.worker_id || '—'}
                        </td>
                        <td style={{ fontSize: '0.85rem' }}>
                          {task.duration_text || '—'}
                        </td>
                        <td style={{ textAlign: 'right' }}>
                          {(task.status === 'queued' || task.status === 'running') && (
                            <button
                              type="button"
                              className="action-btn cancel"
                              disabled={cancellingId === task.id}
                              onClick={() => handleCancelTask(task.id)}
                              title="Примусово скасувати задачу"
                            >
                              {cancellingId === task.id ? '...' : 'Скасувати'}
                            </button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </article>
        </>
      ) : null}
    </div>
  );
};
