import React, { useEffect, useState, useRef } from 'react';
import { tasksApi } from '../api';
import type { Task } from '../types';

interface TaskListPageProps {
  onGoToNewTask: () => void;
}

export const TaskListPage: React.FC<TaskListPageProps> = ({ onGoToNewTask }) => {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const activeSseMap = useRef<Map<string, EventSource>>(new Map());

  const fetchTasks = async () => {
    try {
      setError(null);
      const data = await tasksApi.list();
      setTasks(data);
    } catch (err: any) {
      setError('Не вдалося завантажити список задач.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTasks();
    const interval = setInterval(() => {
      // Periodic background refresh for queued/running tasks
      tasksApi.list().then((updated) => {
        setTasks((prev) => {
          return updated.map((u) => {
            const existing = prev.find((p) => p.id === u.id);
            return {
              ...u,
              progress: existing?.progress !== undefined ? existing.progress : (u.status === 'done' ? 100 : 0),
            };
          });
        });
      }).catch(() => {});
    }, 3000);

    return () => {
      clearInterval(interval);
      // Close any open SSE connections
      activeSseMap.current.forEach((es) => es.close());
      activeSseMap.current.clear();
    };
  }, []);

  // Set up live SSE streams for running / queued tasks
  useEffect(() => {
    const activeTasks = tasks.filter((t) => t.status === 'queued' || t.status === 'running');

    activeTasks.forEach((task) => {
      if (!activeSseMap.current.has(task.id)) {
        try {
          const es = new EventSource(`/api/tasks/${task.id}/events`, { withCredentials: true });
          activeSseMap.current.set(task.id, es);

          es.onmessage = (event) => {
            try {
              const payload = JSON.parse(event.data);
              setTasks((prevTasks) =>
                prevTasks.map((t) => {
                  if (t.id === task.id) {
                    return {
                      ...t,
                      status: payload.status,
                      progress: payload.progress !== undefined ? payload.progress : t.progress,
                    };
                  }
                  return t;
                })
              );

              if (payload.status === 'done' || payload.status === 'failed' || payload.status === 'cancelled') {
                es.close();
                activeSseMap.current.delete(task.id);
                // Refresh full task record to get output_file and finished_at
                tasksApi.get(task.id).then((fresh) => {
                  setTasks((curr) => curr.map((c) => (c.id === task.id ? { ...fresh, progress: 100 } : c)));
                });
              }
            } catch (e) {
              console.error('Failed to parse SSE payload', e);
            }
          };

          es.onerror = () => {
            es.close();
            activeSseMap.current.delete(task.id);
          };
        } catch (e) {
          console.error('Could not initiate SSE for task', task.id, e);
        }
      }
    });
  }, [tasks]);

  const handleCancel = async (id: string) => {
    try {
      await tasksApi.cancel(id);
      fetchTasks();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Не вдалося скасувати задачу.');
    }
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm('Ви впевнені, що бажаєте видалити цю задачу та її файли?')) {
      return;
    }
    try {
      // Close SSE if active
      if (activeSseMap.current.has(id)) {
        activeSseMap.current.get(id)?.close();
        activeSseMap.current.delete(id);
      }
      await tasksApi.delete(id);
      setTasks((prev) => prev.filter((t) => t.id !== id));
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Не вдалося видалити задачу.');
    }
  };

  const formatOperation = (task: Task) => {
    const op = task.operation_type;
    const p = task.params || {};
    if (op === 'scale') {
      return `📉 Зміна масштабу (${p.resolution || 720}p)`;
    }
    if (op === 'watermark') {
      return `🏷️ Водяний знак (${p.position || 'bottom_right'})`;
    }
    if (op === 'audio') {
      return p.action === 'mute' ? '🔇 Вимкнення звуку' : '🎧 Видобування аудіо (MP3)';
    }
    return '🔄 Конвертація в MP4';
  };

  const formatDate = (dateStr: string) => {
    try {
      const d = new Date(dateStr);
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) +
        ' ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    } catch {
      return dateStr;
    }
  };

  return (
    <div className="container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '1.5rem', marginBottom: '1rem' }}>
        <div>
          <h2>Мої задачі</h2>
          <p style={{ color: '#94a3b8', fontSize: '0.9rem', marginBottom: 0 }}>
            Відстежуйте стан, прогрес обробки та завантажуйте готові файли.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <button className="nav-btn" onClick={fetchTasks} title="Оновити список">
            🔄 Оновити
          </button>
          <button className="nav-btn active" onClick={onGoToNewTask}>
            ➕ Нова задача
          </button>
        </div>
      </div>

      {error && (
        <div className="file-meta-box error">
          ⚠️ {error}
        </div>
      )}

      {loading ? (
        <article className="card-container" style={{ textAlign: 'center', padding: '3rem' }}>
          <span aria-busy="true">Завантаження задач...</span>
        </article>
      ) : tasks.length === 0 ? (
        <article className="card-container" style={{ textAlign: 'center', padding: '3rem' }}>
          <div style={{ fontSize: '2.5rem', marginBottom: '1rem' }}>🎬</div>
          <h3>У вас ще немає задач</h3>
          <p style={{ color: '#94a3b8', maxWidth: '400px', margin: '0 auto 1.5rem' }}>
            Завантажте відео для конвертації, зміни масштабу, накладання водяного знака або видобування аудіо.
          </p>
          <button onClick={onGoToNewTask} style={{ display: 'inline-block' }}>
            Створити першу задачу
          </button>
        </article>
      ) : (
        <article className="card-container" style={{ padding: '1rem', overflowX: 'auto' }}>
          <table role="grid" style={{ marginBottom: 0 }}>
            <thead>
              <tr>
                <th scope="col">Час</th>
                <th scope="col">Операція</th>
                <th scope="col">Статус</th>
                <th scope="col" style={{ width: '180px' }}>Прогрес</th>
                <th scope="col" style={{ textAlign: 'right' }}>Дії</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => {
                const progressVal = task.status === 'done' ? 100 : (task.progress || 0);

                return (
                  <tr key={task.id}>
                    <td>
                      <div style={{ fontWeight: 600, fontSize: '0.9rem' }}>{formatDate(task.created_at)}</div>
                      <div style={{ fontSize: '0.75rem', color: '#64748b' }}>
                        ID: {task.id.slice(0, 8)}...
                      </div>
                    </td>
                    <td>
                      <div style={{ fontWeight: 500 }}>{formatOperation(task)}</div>
                      {task.worker_id && (
                        <div style={{ fontSize: '0.75rem', color: '#64748b' }}>
                          Воркер: {task.worker_id}
                        </div>
                      )}
                    </td>
                    <td>
                      <span className={`status-badge ${task.status}`}>
                        {task.status === 'queued' && '⏳ В черзі'}
                        {task.status === 'running' && '⚙️ Обробка'}
                        {task.status === 'done' && '✅ Готово'}
                        {task.status === 'failed' && '❌ Помилка'}
                        {task.status === 'cancelled' && '🚫 Скасовано'}
                      </span>
                      {task.error_message && (
                        <div style={{ fontSize: '0.75rem', color: '#f87171', marginTop: '0.2rem', maxWidth: '200px' }} title={task.error_message}>
                          {task.error_message.slice(0, 60)}...
                        </div>
                      )}
                    </td>
                    <td>
                      <div className="progress-wrapper">
                        <progress className="custom-progress" value={progressVal} max="100" />
                        <span className="progress-text">{progressVal}%</span>
                      </div>
                    </td>
                    <td style={{ textAlign: 'right' }}>
                      <div className="action-btn-group" style={{ justifyContent: 'flex-end' }}>
                        {/* Download button for completed task */}
                        {task.status === 'done' && (
                          <a
                            href={tasksApi.getDownloadUrl(task.id)}
                            download
                            className="action-btn download"
                            title="Завантажити готовий файл"
                          >
                            ⬇️ Скачати
                          </a>
                        )}

                        {/* Cancel button for active task */}
                        {(task.status === 'queued' || task.status === 'running') && (
                          <button
                            type="button"
                            className="action-btn cancel"
                            onClick={() => handleCancel(task.id)}
                            title="Скасувати обробку"
                          >
                            ⏹️ Скасувати
                          </button>
                        )}

                        {/* Delete button */}
                        <button
                          type="button"
                          className="action-btn delete"
                          onClick={() => handleDelete(task.id)}
                          title="Видалити задачу"
                        >
                          🗑️
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </article>
      )}
    </div>
  );
};
