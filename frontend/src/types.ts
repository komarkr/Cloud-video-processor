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

export interface StatusCounts {
  queued: number;
  running: number;
  done: number;
  failed: number;
  cancelled: number;
  total: number;
}

export interface AdminWorker {
  hostname: string;
  cpu: number;
  ram: number;
  current_task?: string | null;
  last_seen: string;
  timestamp?: string | null;
}

export interface AdminTask {
  id: string;
  user_email: string;
  operation_type: string;
  status: TaskStatus;
  progress: number;
  worker_id?: string | null;
  duration_sec?: number | null;
  duration_text?: string | null;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error_message?: string | null;
}

export interface AutoscalerEvent {
  action: string;
  workers: number;
  queued?: number;
  timestamp: string;
}

export interface AdminOverview {
  status: string;
  status_counts: StatusCounts;
  queue_length: number;
  active_workers_count: number;
  workers: AdminWorker[];
  recent_tasks: AdminTask[];
  autoscaler_events: AutoscalerEvent[];
}


