import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端端口 8090、前端 4090：**刻意落在 frpc 的 8020-8040/4020-4040 范围之外**。
// 该范围会被转发到公网，而这套系统带鉴权之前不能公网可达；
// 迁到范围外之后，即便本机在跑开发服务，公网也访问不到。
const BACKEND = process.env.VITE_BACKEND_ORIGIN || 'http://127.0.0.1:8090'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '0.0.0.0',
    port: 4090,
    // 开发时把 /api 转发到后端，浏览器看到的始终是同一个源（localhost:4090），
    // 于是 httpOnly cookie 能正常收发。**不要**改成在前端里写后端绝对地址：
    // 那样 cookie 就成了第三方 cookie，登录会静默失败。
    proxy: {
      '/api': {
        target: BACKEND,
        changeOrigin: false // 保持 Host 不变，否则后端下发的 cookie 域会对不上
      }
    }
  },
  build: {
    // 构建产物由 FastAPI 直接托管，走同源。
    outDir: 'dist',
    emptyOutDir: true
  }
})
