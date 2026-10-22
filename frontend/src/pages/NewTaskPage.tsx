import React, { useState, useRef } from 'react';
import { tasksApi } from '../api';

interface NewTaskPageProps {
  onTaskCreated: () => void;
}

export const NewTaskPage: React.FC<NewTaskPageProps> = ({ onTaskCreated }) => {
  const [file, setFile] = useState<File | null>(null);
  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [operationType, setOperationType] = useState<string>('convert');

  // Operation parameters
  const [scaleResolution, setScaleResolution] = useState<string>('720');
  const [watermarkPosition, setWatermarkPosition] = useState<string>('bottom_right');
  const [watermarkOpacity, setWatermarkOpacity] = useState<number>(0.8);
  const [audioAction, setAudioAction] = useState<string>('extract');

  // Metadata & Validation
  const [videoMeta, setVideoMeta] = useState<{ duration: number; width: number; height: number } | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleVideoSelect = (selectedFile: File) => {
    setValidationError(null);
    setSubmitError(null);
    setVideoMeta(null);

    // 1. Client-side size check (<= 100 MB)
    const maxBytes = 100 * 1024 * 1024;
    if (selectedFile.size > maxBytes) {
      setValidationError(
        `Розмір файлу (${(selectedFile.size / (1024 * 1024)).toFixed(1)} МБ) перевищує максимальний ліміт 100 МБ.`
      );
      setFile(null);
      return;
    }

    // 2. Client-side video metadata check using <video> element
    const video = document.createElement('video');
    video.preload = 'metadata';
    const objectUrl = URL.createObjectURL(selectedFile);
    video.src = objectUrl;

    video.onloadedmetadata = () => {
      URL.revokeObjectURL(objectUrl);
      const duration = video.duration;
      const width = video.videoWidth;
      const height = video.videoHeight;

      if (duration > 120) {
        setValidationError(
          `Тривалість відео (${duration.toFixed(1)} с) перевищує дозволений ліміт 120 секунд.`
        );
        setFile(null);
      } else if (width > 1920 || height > 1080) {
        setValidationError(
          `Роздільна здатність (${width}x${height}) перевищує дозволену 1920x1080.`
        );
        setFile(null);
      } else {
        setVideoMeta({ duration, width, height });
        setFile(selectedFile);
      }
    };

    video.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      setValidationError('Не вдалося прочитати метадані відео. Перевірте формат файлу.');
      setFile(null);
    };
  };

  const handleLogoSelect = (selectedLogo: File) => {
    setValidationError(null);
    if (!selectedLogo.name.toLowerCase().endsWith('.png') && selectedLogo.type !== 'image/png') {
      setValidationError('Водяний знак має бути у форматі PNG.');
      setLogoFile(null);
      return;
    }
    if (selectedLogo.size > 2 * 1024 * 1024) {
      setValidationError('Розмір файлу водяного знака не повинен перевищувати 2 МБ.');
      setLogoFile(null);
      return;
    }
    setLogoFile(selectedLogo);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) {
      setValidationError('Будь ласка, виберіть відеофайл.');
      return;
    }

    if (operationType === 'watermark' && !logoFile) {
      setValidationError('Для операції водяного знака необхідно вибрати PNG логотип.');
      return;
    }

    setSubmitting(true);
    setUploadProgress(0);
    setSubmitError(null);

    const formData = new FormData();
    formData.append('file', file);
    formData.append('operation_type', operationType);

    // Build operation-specific parameters
    let params: Record<string, any> = {};
    if (operationType === 'scale') {
      params = { resolution: parseInt(scaleResolution, 10) };
    } else if (operationType === 'watermark') {
      params = {
        position: watermarkPosition,
        opacity: watermarkOpacity,
      };
      if (logoFile) {
        formData.append('logo', logoFile);
      }
    } else if (operationType === 'audio') {
      params = { action: audioAction };
    } else {
      params = { format: 'mp4' };
    }

    formData.append('params', JSON.stringify(params));

    try {
      await tasksApi.create(formData, (progressEvent) => {
        if (progressEvent.total) {
          const pct = Math.round((progressEvent.loaded * 100) / progressEvent.total);
          setUploadProgress(pct);
        }
      });
      onTaskCreated();
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      if (typeof detail === 'string') {
        setSubmitError(detail);
      } else {
        setSubmitError('Не вдалося завантажити задачу. Спробуйте пізніше.');
      }
      setSubmitting(false);
      setUploadProgress(null);
    }
  };

  return (
    <div className="container" style={{ maxWidth: '680px' }}>
      <article className="card-container">
        <header style={{ marginBottom: '1.5rem' }}>
          <h2>Створення нової задачі</h2>
          <p style={{ color: '#94a3b8', fontSize: '0.9rem' }}>
            Завантажте відео та виберіть бажану операцію обробки.
          </p>
        </header>

        {validationError && (
          <div className="file-meta-box error">
            ⚠️ <strong>Помилка валідації:</strong> {validationError}
          </div>
        )}

        {submitError && (
          <div className="file-meta-box error">
            ❌ <strong>Помилка сервера:</strong> {submitError}
          </div>
        )}

        <form onSubmit={handleSubmit}>
          {/* File Picker */}
          <label htmlFor="video-file-input">
            <strong>1. Виберіть відео</strong> (до 100 МБ, тривалість $\le$ 120 с, до 1080p)
            <input
              id="video-file-input"
              ref={fileInputRef}
              type="file"
              accept="video/mp4,video/quicktime,video/x-matroska,video/webm,video/x-msvideo"
              onChange={(e) => {
                if (e.target.files && e.target.files[0]) {
                  handleVideoSelect(e.target.files[0]);
                }
              }}
              disabled={submitting}
              required
            />
          </label>

          {/* Client-side metadata preview */}
          {file && videoMeta && (
            <div className="file-meta-box">
              <div className="meta-row">
                <span>📁 Назва файлу:</span>
                <strong>{file.name}</strong>
              </div>
              <div className="meta-row">
                <span>⏱️ Тривалість:</span>
                <strong>
                  {videoMeta.duration >= 60
                    ? `${Math.floor(videoMeta.duration / 60)} хв ${Math.round(videoMeta.duration % 60)} с`
                    : `${videoMeta.duration.toFixed(1)} с`}
                </strong>
              </div>
              <div className="meta-row">
                <span>📐 Роздільна здатність:</span>
                <strong>{videoMeta.width} × {videoMeta.height} px</strong>
              </div>
              <div className="meta-row">
                <span>💾 Розмір:</span>
                <strong>{(file.size / (1024 * 1024)).toFixed(2)} МБ</strong>
              </div>
            </div>
          )}

          {/* Operation Selector */}
          <label htmlFor="operation-select">
            <strong>2. Операція обробки</strong>
            <select
              id="operation-select"
              value={operationType}
              onChange={(e) => setOperationType(e.target.value)}
              disabled={submitting}
            >
              <option value="convert">🔄 Конвертація в MP4 (H.264 + AAC)</option>
              <option value="scale">📉 Зміна роздільної здатності (Scale)</option>
              <option value="watermark">🏷️ Водяний знак (Watermark)</option>
              <option value="audio">🎵 Обробка аудіо (Extract / Mute)</option>
            </select>
          </label>

          {/* Scale parameters */}
          {operationType === 'scale' && (
            <div style={{ marginTop: '1rem', background: 'rgba(255,255,255,0.03)', padding: '1rem', borderRadius: '8px' }}>
              <label htmlFor="scale-res-select">
                Бажана висота кадру:
                <select
                  id="scale-res-select"
                  value={scaleResolution}
                  onChange={(e) => setScaleResolution(e.target.value)}
                  disabled={submitting}
                >
                  <option value="1080">1080p (Full HD)</option>
                  <option value="720">720p (HD)</option>
                  <option value="480">480p (SD)</option>
                  <option value="360">360p (Економний)</option>
                </select>
              </label>
            </div>
          )}

          {/* Watermark parameters */}
          {operationType === 'watermark' && (
            <div style={{ marginTop: '1rem', background: 'rgba(255,255,255,0.03)', padding: '1rem', borderRadius: '8px' }}>
              <label htmlFor="logo-file-input">
                PNG логотип водяного знака ($\le$ 2 МБ):
                <input
                  id="logo-file-input"
                  type="file"
                  accept="image/png"
                  onChange={(e) => {
                    if (e.target.files && e.target.files[0]) {
                      handleLogoSelect(e.target.files[0]);
                    }
                  }}
                  disabled={submitting}
                  required
                />
              </label>

              <div className="grid">
                <label htmlFor="watermark-pos-select">
                  Розташування:
                  <select
                    id="watermark-pos-select"
                    value={watermarkPosition}
                    onChange={(e) => setWatermarkPosition(e.target.value)}
                    disabled={submitting}
                  >
                    <option value="bottom_right">Нижній правий кут</option>
                    <option value="bottom_left">Нижній лівий кут</option>
                    <option value="top_right">Верхній правий кут</option>
                    <option value="top_left">Верхній лівий кут</option>
                    <option value="center">По центру</option>
                  </select>
                </label>

                <label htmlFor="watermark-opacity-range">
                  Прозорість ({Math.round(watermarkOpacity * 100)}%):
                  <input
                    id="watermark-opacity-range"
                    type="range"
                    min="0.1"
                    max="1.0"
                    step="0.05"
                    value={watermarkOpacity}
                    onChange={(e) => setWatermarkOpacity(parseFloat(e.target.value))}
                    disabled={submitting}
                  />
                </label>
              </div>
            </div>
          )}

          {/* Audio parameters */}
          {operationType === 'audio' && (
            <div style={{ marginTop: '1rem', background: 'rgba(255,255,255,0.03)', padding: '1rem', borderRadius: '8px' }}>
              <label htmlFor="audio-action-select">
                Дія над звуковою доріжкою:
                <select
                  id="audio-action-select"
                  value={audioAction}
                  onChange={(e) => setAudioAction(e.target.value)}
                  disabled={submitting}
                >
                  <option value="extract">🎧 Видобути аудіодоріжку в MP3 (libmp3lame)</option>
                  <option value="mute">🔇 Вимкнути звук / Mute (відео без звуку у MP4)</option>
                </select>
              </label>
            </div>
          )}

          {/* Upload Progress Bar */}
          {uploadProgress !== null && (
            <div style={{ marginTop: '1.5rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.85rem', marginBottom: '0.3rem' }}>
                <span>Завантаження на сервер...</span>
                <strong>{uploadProgress}%</strong>
              </div>
              <progress value={uploadProgress} max="100" style={{ width: '100%' }} />
            </div>
          )}

          {/* Submit button */}
          <button
            type="submit"
            aria-busy={submitting}
            disabled={submitting || !file || !!validationError}
            style={{ width: '100%', marginTop: '1.5rem' }}
          >
            {submitting ? 'Завантаження...' : '🚀 Запустити обробку'}
          </button>
        </form>
      </article>
    </div>
  );
};
