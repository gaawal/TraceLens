import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const proxyTarget = env.VITE_TRACELENS_PROXY_TARGET || 'http://127.0.0.1:8000';
  const apiProxy = {
    target: proxyTarget,
    changeOrigin: true,
    xfwd: true,
    timeout: 0,
    proxyTimeout: 0,
  };

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      fs: { strict: false },
      proxy: { '/api': apiProxy },
    },
    preview: {
      host: '0.0.0.0',
      port: 4173,
      proxy: { '/api': apiProxy },
    },
  };
});
