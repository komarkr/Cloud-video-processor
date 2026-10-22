export type TaskStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

export interface User {
  id: string;
  email: string;
  is_admin: boolean;
  created_at: string;
}

export interface Task {
  id: string;
  user_id: string;
  status: TaskStatus;
  operation_type: string;
  params?: Record<string, any> | null;
  input_file: string;
  output_file?: string | null;
  duration_sec?: number | null;
  queue_position?: number | null;
  eta_sec?: number | null;
  error_message?: string | null;
  worker_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  expires_at?: string | null;
  created_at: string;
  updated_at: string;
  progress?: number;
}

export const formatEta = (sec?: number | null): string => {
  if (sec === undefined || sec === null) return 'розраховується...';
  if (sec <= 0) return 'кілька секунд';
  if (sec < 60) return `${sec} с`;
  const mins = Math.round(sec / 60);
  if (mins <= 1) return '1 хв';
  return `${mins} хв`;
};

export const formatQueueInfo = (position?: number | null, etaSec?: number | null): string => {
  const posPart = position ? `місце ${position}` : 'у черзі';
  const etaPart = formatEta(etaSec);
  return `У черзі, ${posPart}, приблизно ${etaPart}`;
};

