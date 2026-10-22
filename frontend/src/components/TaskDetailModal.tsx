import React from 'react';
import type { Task } from '../types';
import { formatEta, formatQueueInfo } from '../types';
import { tasksApi } from '../api';

interface TaskDetailModalProps {
  task: Task | null;
  onClose: () => void;
  onCancel: (id: string) => void;
  onDelete: (id: string) => void;
}

export const TaskDetailModal: React.FC<TaskDetailModalProps> = ({
  task,
  onClose,
  onCancel,
  onDelete,
}) => {
  if (!task) return null;

  const isAudioExtract =
    task.operation_type === 'extract_audio' ||
    (task.operation_type === 'audio' && task.params?.action !== 'mute' && task.params?.action !== 'remove') ||
    (task.output_file && task.output_file.endsWith('.mp3'));

  const downloadUrl = tasksApi.getDownloadUrl(task.id);

  const calculateDuration = () => {
    if (!task.started_at || !task.finished_at) return null;
    const start = new Date(task.started_at).getTime();
    const finish = new Date(task.finished_at).getTime();
    const diffSec = Math.max(0, Math.round((finish - start) / 1000));
    if (diffSec < 60) return `${diffSec} с`;
    return `${Math.floor(diffSec / 60)} хв ${diffSec % 60} с`;
  };

  const formatDate = (dateStr?: string | null) => {
    if (!dateStr) return '—';
    try {
      const d = new Date(dateStr);
      return d.toLocaleString('uk-UA', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
    } catch {
      return dateStr;
    }
  };

  return (
    <dialog open style={{ backdropFilter: 'blur(8px)', zIndex: 1000, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <article className="card-container" style={{ maxWidth: '640px', width: '90%', maxHeight: '90vh', overflowY: 'auto', margin: 'auto' }}>
        <header style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderBottom: '1px solid rgba(255,255,255,0.1)', paddingBottom: '1rem', marginBottom: '1.25rem' }}>
          <div>
            <h3 style={{ margin: 0 }}>Деталі задачі</h3>
            <span style={{ fontSize: '0.8rem', color: '#94a3b8' }}>ID: {task.id}</span>
          </div>
          <button
            type="button"
            className="action-btn"
            style={{ background: 'transparent', border: 'none', fontSize: '1.5rem', color: '#94a3b8', cursor: 'pointer', padding: '0 0.5rem' }}
            onClick={onClose}
          >
            ✕
          </button>
        </header>

        {/* Status Badge */}
        <div style={{ marginBottom: '1.25rem', display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
          <strong>Статус:</strong>
          <span className={`status-badge ${task.status}`}>
            {task.status === 'queued' && (
              task.queue_position
                ? `⏳ ${formatQueueInfo(task.queue_position, task.eta_sec)}`
                : '⏳ У черзі'
            )}
            {task.status === 'running' && '⚙️ Виконується'}
            {task.status === 'done' && '✅ Готово'}
            {task.status === 'failed' && '❌ Помилка'}
            {task.status === 'cancelled' && '🚫 Скасовано'}
          </span>
          {task.status === 'running' && (
            <span style={{ fontSize: '0.85rem', color: '#38bdf8' }}>
              Прогрес: {task.progress || 0}%
            </span>
          )}
        </div>

        {/* Queued notification banner */}
        {task.status === 'queued' && (
          <div className="file-meta-box" style={{ marginBottom: '1.25rem', borderColor: 'rgba(56, 189, 248, 0.4)' }}>
            <h4 style={{ color: '#38bdf8', fontSize: '0.95rem', margin: '0 0 0.4rem' }}>
              ℹ️ Статус очікування черги:
            </h4>
            <div style={{ fontSize: '0.95rem', color: '#f8fafc', fontWeight: 500 }}>
              {formatQueueInfo(task.queue_position, task.eta_sec)}
            </div>
            {task.eta_sec !== undefined && task.eta_sec !== null && (
              <div style={{ fontSize: '0.8rem', color: '#94a3b8', marginTop: '0.2rem' }}>
                Орієнтовний розрахунковий час очікування: {task.eta_sec} с
              </div>
            )}
          </div>
        )}

        {/* Error message box if failed */}
        {task.status === 'failed' && task.error_message && (
          <div className="file-meta-box error" style={{ marginBottom: '1.25rem' }}>
            <h4 style={{ color: '#f87171', fontSize: '0.95rem', margin: '0 0 0.5rem' }}>
              ⚠️ Текст помилки обробки:
            </h4>
            <pre style={{ margin: 0, padding: '0.75rem', background: 'rgba(0,0,0,0.4)', borderRadius: '6px', fontSize: '0.8rem', whiteSpace: 'pre-wrap', wordBreak: 'break-all', color: '#fca5a5' }}>
              {task.error_message}
            </pre>
          </div>
        )}

        {/* Video / Audio Preview if Done */}
        {task.status === 'done' && (
          <div style={{ marginBottom: '1.5rem', background: 'rgba(0,0,0,0.3)', padding: '1rem', borderRadius: '10px' }}>
            <h4 style={{ margin: '0 0 0.75rem', fontSize: '0.95rem', color: '#38bdf8' }}>
              🎬 Попередній перегляд результату:
            </h4>
            {isAudioExtract ? (
              <audio controls src={downloadUrl} style={{ width: '100%' }}>
                Ваш браузер не підтримує аудіо елемент.
              </audio>
            ) : (
              <video
                controls
                src={downloadUrl}
                style={{ width: '100%', maxHeight: '360px', borderRadius: '8px', background: '#000' }}
              >
                Ваш браузер не підтримує відео елемент.
              </video>
            )}
          </div>
        )}

        {/* Metadata Details Grid */}
        <div className="file-meta-box" style={{ marginBottom: '1.5rem' }}>
          <div className="meta-row">
            <span>Операція:</span>
            <strong>{task.operation_type}</strong>
          </div>
          {task.params && (
            <div className="meta-row">
              <span>Параметри:</span>
              <code style={{ fontSize: '0.8rem', background: 'rgba(255,255,255,0.05)', padding: '0.1rem 0.4rem', borderRadius: '4px' }}>
                {JSON.stringify(task.params)}
              </code>
            </div>
          )}
          {task.worker_id && (
            <div className="meta-row">
              <span>Воркер (Host):</span>
              <strong>{task.worker_id}</strong>
            </div>
          )}
          <div className="meta-row">
            <span>Створено:</span>
            <strong>{formatDate(task.created_at)}</strong>
          </div>
          {task.queue_position && (
            <div className="meta-row">
              <span>Місце у черзі:</span>
              <strong style={{ color: '#38bdf8' }}>#{task.queue_position}</strong>
            </div>
          )}
          {task.eta_sec !== undefined && task.eta_sec !== null && (
            <div className="meta-row">
              <span>Орієнтовне очікування (ETA):</span>
              <strong style={{ color: '#38bdf8' }}>{formatEta(task.eta_sec)} ({task.eta_sec} с)</strong>
            </div>
          )}
          {task.started_at && (
            <div className="meta-row">
              <span>Початок обробки:</span>
              <strong>{formatDate(task.started_at)}</strong>
            </div>
          )}
          {task.finished_at && (
            <div className="meta-row">
              <span>Завершено:</span>
              <strong>{formatDate(task.finished_at)}</strong>
            </div>
          )}
          {calculateDuration() && (
            <div className="meta-row">
              <span>Тривалість виконання:</span>
              <strong style={{ color: '#38bdf8' }}>{calculateDuration()}</strong>
            </div>
          )}
          {task.expires_at && (
            <div className="meta-row">
              <span>Термін дії файлу (expires_at):</span>
              <strong style={{ color: '#f59e0b' }}>{formatDate(task.expires_at)}</strong>
            </div>
          )}
        </div>

        {/* Footer Actions */}
        <footer style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.6rem', borderTop: '1px solid rgba(255,255,255,0.1)', paddingTop: '1rem' }}>
          {task.status === 'done' && (
            <a href={downloadUrl} download className="action-btn download">
              ⬇️ Скачати файл
            </a>
          )}
          {(task.status === 'queued' || task.status === 'running') && (
            <button
              type="button"
              className="action-btn cancel"
              onClick={() => {
                onCancel(task.id);
                onClose();
              }}
            >
              ⏹️ Скасувати
            </button>
          )}
          <button
            type="button"
            className="action-btn delete"
            onClick={() => {
              onDelete(task.id);
              onClose();
            }}
          >
            🗑️ Видалити
          </button>
          <button type="button" className="nav-btn" onClick={onClose}>
            Закрити
          </button>
        </footer>
      </article>
    </dialog>
  );
};
