import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: '../static', // 直接打包到 FastAPI 的 static 目录
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // API（网关端点）
      '/v1': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // 管理控制台登录（JWT）
      '/auth': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // 初始化向导
      '/setup': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
});