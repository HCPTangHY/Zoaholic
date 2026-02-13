import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import Channels from './pages/Channels';
import Playground from './pages/Playground';
import Admin from './pages/Admin';
import Settings from './pages/Settings';
import Logs from './pages/Logs';
import Plugins from './pages/Plugins';
import Layout from './components/Layout';
import Login from './pages/Login';
import Setup from './pages/Setup';
import { useAuthStore } from './store/authStore';

// 导入 themeStore 以确保主题初始化代码执行
import './store/themeStore';

function App() {
  const { isAuthenticated } = useAuthStore();

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/setup" element={<Setup />} />
        <Route path="/login" element={isAuthenticated ? <Navigate to="/" /> : <Login />} />

        <Route path="/" element={isAuthenticated ? <Layout /> : <Navigate to="/login" />}>
          <Route index element={<Dashboard />} />
          <Route path="channels" element={<Channels />} />
          <Route path="playground" element={<Playground />} />
          <Route path="plugins" element={<Plugins />} />
          <Route path="admin" element={<Admin />} />
          <Route path="settings" element={<Settings />} />
          <Route path="logs" element={<Logs />} />
          <Route path="*" element={<Navigate to="/" />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}

export default App;
