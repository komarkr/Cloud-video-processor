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
  error_message?: string | null;
  worker_id?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  expires_at?: string | null;
  created_at: string;
  updated_at: string;
  progress?: number;
}
