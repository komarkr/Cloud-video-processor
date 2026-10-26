import React, { useEffect, useState, useRef } from 'react';
import { tasksApi } from '../api';
import type { Task } from '../types';
import { formatEta, formatQueueInfo } from '../types';
import { TaskDetailModal } from '../components/TaskDetailModal';

interface TaskListPageProps {
  onGoToNewTask: () => void;
}

export const TaskListPage: React.FC<TaskListPageProps> = ({ onGoToNewTask }) => {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const activeSseMap = useRef<Map<string, EventSource>>(new Map());

  const fetchTasks = async () => {
    try {
      setError(null);
      const data = await tasksApi.list();
      setTasks((prev) => {
        return data.map((d) => {
          const existing = prev.find((p) => p.id === d.id);
          return {
            ...d,
            progress:
              existing?.progress !== undefined
                ? existing.progress
                : d.status === 'done'
                ? 100
                : 0,
          };
        });
      });
    } catch (err: any) {
      setError('Не вдалося завантажити список задач.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTasks();

    // Polling fallback every 2 seconds for active tasks
    const interval = setInterval(() => {
      tasksApi
        .list()
        .then((updated) => {
          setTasks((prev) => {
            return updated.map((u) => {
              const existing = prev.find((p) => p.id === u.id);
              return {
                ...u,
                progress:
                  existing?.progress !== undefined
                    ? existing.progress
                    : u.status === 'done'
                    ? 100
                    : 0,
              };
            });
          });
        })
        .catch(() => {});
    }, 2000);

    return () => {
      clearInterval(interval);
      // Close all active SSE connections on unmount
      activeSseMap.current.forEach((es) => es.close());
      activeSseMap.current.clear();
    };
  }, []);

  // Set up live SSE streams for queued and running tasks
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
                    const updated = {
                      ...t,
                      status: payload.status,
                      progress: payload.progress !== undefined ? payload.progress : t.progress,
                      queue_position: payload.queue_position !== undefined ? payload.queue_position : t.queue_position,
                      eta_sec: payload.eta_sec !== undefined ? payload.eta_sec : t.eta_sec,
                    };
                    // Keep modal in sync if open
                    if (selectedTask && selectedTask.id === task.id) {
                      setSelectedTask(updated);
                    }
                    return updated;
                  }
                  return t;
                })
              );

              if (payload.status === 'done' || payload.status === 'failed' || payload.status === 'cancelled') {
                es.close();
                activeSseMap.current.delete(task.id);
                // Refresh task record for output_file, finished_at, error_message
                tasksApi.get(task.id).then((fresh) => {
                  setTasks((curr) =>
                    curr.map((c) => (c.id === task.id ? { ...fresh, progress: 100 } : c))
                  );
                  if (selectedTask && selectedTask.id === task.id) {
                    setSelectedTask({ ...fresh, progress: 100 });
                  }
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
  }, [tasks, selectedTask]);

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
      if (activeSseMap.current.has(id)) {
        activeSseMap.current.get(id)?.close();
        activeSseMap.current.delete(id);
      }
      await tasksApi.delete(id);
      setTasks((prev) => prev.filter((t) => t.id !== id));
      if (selectedTask && selectedTask.id === id) {
        setSelectedTask(null);
      }
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Не вдалося видалити задачу.');
    }
  };

  const formatOperation = (task: Task) => {
    const op = task.operation_type;
    const p = task.params || {};
    if (op === 'scale') {
      return `Зміна масштабу (${p.resolution || 720}p)`;
    }
    if (op === 'watermark') {
      return `Водяний знак (${p.position || 'bottom_right'})`;
    }
    if (op === 'audio') {
      return p.action === 'mute' ? 'Вимкнення звуку' : 'Видобування аудіо (MP3)';
    }
    return 'Конвертація в MP4';
  };

  const formatDate = (dateStr: string) => {
    try {
      const d = new Date(dateStr);
      return (
        d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) +
        ' ' +
        d.toLocaleDateString([], { month: 'short', day: 'numeric' })
      );
    } catch {
      return dateStr;
    }
  };

  return (
    <div className="container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '1.5rem', marginBottom: '1rem', flexWrap: 'wrap', gap: '1rem' }}>
        <div>
          <h2>Мої задачі</h2>
          <p style={{ color: '#94a3b8', fontSize: '0.9rem', marginBottom: 0 }}>
            Відстежуйте стан, прогрес обробки у реальному часі та переглядайте результат.
          </p>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
          <button className="nav-btn" onClick={fetchTasks} title="Оновити список">
            Оновити
          </button>
          <button className="nav-btn active" onClick={onGoToNewTask}>
            Нова задача
          </button>
        </div>
      </div>

      {error && (
        <div className="file-meta-box error">
          {error}
        </div>
      )}

      {loading ? (
        <article className="card-container" style={{ textAlign: 'center', padding: '3rem' }}>
          <span aria-busy="true">Завантаження задач...</span>
        </article>
      ) : tasks.length === 0 ? (
        <article className="card-container" style={{ textAlign: 'center', padding: '3rem' }}>
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
                        {task.status === 'queued' && (
                          task.queue_position
                            ? formatQueueInfo(task.queue_position, task.eta_sec)
                            : 'У черзі'
                        )}
                        {task.status === 'running' && 'Виконується'}
                        {task.status === 'done' && 'Готово'}
                        {task.status === 'failed' && 'Помилка'}
                        {task.status === 'cancelled' && 'Скасовано'}
                      </span>
                      {task.status === 'queued' && task.queue_position && (
                        <div style={{ fontSize: '0.75rem', color: '#94a3b8', marginTop: '0.25rem' }}>
                          Місце #{task.queue_position} • ETA: {formatEta(task.eta_sec)}
                        </div>
                      )}
                      {task.error_message && (
                        <div
                          style={{ fontSize: '0.75rem', color: '#f87171', marginTop: '0.2rem', maxWidth: '180px', cursor: 'pointer' }}
                          title={task.error_message}
                          onClick={() => setSelectedTask(task)}
                        >
                          {task.error_message.slice(0, 45)}... (деталі)
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
                        {/* Details / Preview modal button */}
                        <button
                          type="button"
                          className="action-btn"
                          style={{ background: 'rgba(56, 189, 248, 0.15)', color: '#38bdf8', border: '1px solid rgba(56, 189, 248, 0.3)' }}
                          onClick={() => setSelectedTask(task)}
                          title="Переглянути деталі та медіа"
                        >
                          {task.status === 'done' ? 'Перегляд' : 'Деталі'}
                        </button>

                        {/* Download button for completed task */}
                        {task.status === 'done' && (
                          <a
                            href={tasksApi.getDownloadUrl(task.id)}
                            download
                            className="action-btn download"
                            title="Завантажити готовий файл"
                          >
                            Скачати
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
                            Скасувати
                          </button>
                        )}

                        {/* Delete button */}
                        <button
                          type="button"
                          className="action-btn delete"
                          onClick={() => handleDelete(task.id)}
                          title="Видалити задачу"
                        >
                          Видалити
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

      {/* Task Details & Video Preview Modal */}
      {selectedTask && (
        <TaskDetailModal
          task={selectedTask}
          onClose={() => setSelectedTask(null)}
          onCancel={handleCancel}
          onDelete={handleDelete}
        />
      )}
    </div>
  );
};
