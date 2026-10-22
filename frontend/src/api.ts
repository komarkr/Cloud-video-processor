import axios from 'axios';
import type { User, Task } from './types';

export const api = axios.create({
  baseURL: '',
  withCredentials: true,
});

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response && error.response.status === 401) {
      const url = error.config?.url || '';
      if (!url.includes('/login') && !url.includes('/register') && !url.includes('/me')) {
        window.dispatchEvent(new CustomEvent('auth:unauthorized'));
      }
    }
    return Promise.reject(error);
  }
);

export const authApi = {
  async getMe(): Promise<User> {
    const res = await api.get('/api/me');
    return res.data;
  },
  async login(email: string, password: string): Promise<User> {
    const res = await api.post('/api/login', { email, password });
    return res.data.user;
  },
  async register(email: string, password: string): Promise<User> {
    const res = await api.post('/api/register', { email, password });
    return res.data.user;
  },
  async logout(): Promise<void> {
    await api.post('/api/logout');
  },
};

export const tasksApi = {
  async list(): Promise<Task[]> {
    const res = await api.get('/api/tasks');
    return res.data;
  },
  async get(id: string): Promise<Task> {
    const res = await api.get(`/api/tasks/${id}`);
    return res.data;
  },
  async create(
    formData: FormData,
    onUploadProgress?: (progressEvent: any) => void
  ): Promise<Task> {
    const res = await api.post('/api/tasks', formData, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress,
    });
    return res.data;
  },
  async cancel(id: string): Promise<void> {
    await api.post(`/api/tasks/${id}/cancel`);
  },
  async delete(id: string): Promise<void> {
    await api.delete(`/api/tasks/${id}`);
  },
  getDownloadUrl(id: string): string {
    return `/api/tasks/${id}/download`;
  },
};
