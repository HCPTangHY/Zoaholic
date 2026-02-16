import React, { useState } from 'react';
import { Outlet, Link, useLocation } from 'react-router-dom';
import { LayoutDashboard, Server, Terminal, Key, Settings as SettingsIcon, LogOut, FileText, Puzzle, Sun, Moon, Laptop, Menu, X } from 'lucide-react';
import { useAuthStore } from '../store/authStore';
import { useThemeStore } from '../store/themeStore';

export default function Layout() {
  const { logout } = useAuthStore();
  const { theme, setTheme } = useThemeStore();
  const location = useLocation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);

  const navItems = [
    { id: '/', label: '仪表盘', icon: <LayoutDashboard className="w-5 h-5" /> },
    { id: '/channels', label: '渠道配置', icon: <Server className="w-5 h-5" /> },
    { id: '/playground', label: '测试工坊', icon: <Terminal className="w-5 h-5" /> },
    { id: '/plugins', label: '插件管理', icon: <Puzzle className="w-5 h-5" /> },
    { id: '/logs', label: '系统日志', icon: <FileText className="w-5 h-5" /> },
    { id: '/admin', label: '密钥管理', icon: <Key className="w-5 h-5" /> },
    { id: '/settings', label: '系统设置', icon: <SettingsIcon className="w-5 h-5" /> },
  ];

  const handleNavClick = () => {
    setMobileMenuOpen(false);
  };

  const NavContent = () => (
    <>
      <nav className="flex-1 p-4 space-y-1">
        {navItems.map(item => (
          <Link
            key={item.id}
            to={item.id}
            onClick={handleNavClick}
            className={`flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-medium transition-all ${
              location.pathname === item.id 
                ? 'bg-primary text-primary-foreground shadow-md' 
                : 'text-muted-foreground hover:text-foreground hover:bg-muted'
            }`}
          >
            {item.icon}
            {item.label}
          </Link>
        ))}
      </nav>

      <div className="p-4 border-t border-border space-y-1">
        {/* Theme Switcher */}
        <div className="flex items-center bg-muted/70 p-1 rounded-lg mb-2">
          <button onClick={() => setTheme('light')} className={`flex-1 flex justify-center py-1.5 rounded-md text-xs font-medium transition-colors ${theme === 'light' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <Sun className="w-4 h-4" />
          </button>
          <button onClick={() => setTheme('system')} className={`flex-1 flex justify-center py-1.5 rounded-md text-xs font-medium transition-colors ${theme === 'system' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <Laptop className="w-4 h-4" />
          </button>
          <button onClick={() => setTheme('dark')} className={`flex-1 flex justify-center py-1.5 rounded-md text-xs font-medium transition-colors ${theme === 'dark' ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}>
            <Moon className="w-4 h-4" />
          </button>
        </div>

        <button 
          onClick={logout}
          className="flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-medium text-red-600 dark:text-red-500 hover:bg-red-50 dark:hover:bg-red-500/10 w-full transition-colors"
        >
          <LogOut className="w-5 h-5" />
          退出登录
        </button>
      </div>
    </>
  );

  return (
    <div className="flex h-screen bg-background text-foreground font-sans transition-colors duration-300">
      {/* Desktop Sidebar */}
      <aside className="w-64 bg-card border-r border-border flex-col hidden md:flex">
        <div className="h-16 flex items-center px-6 border-b border-border">
          <div className="flex items-center gap-2">
            <img src="/zoaholic.png" alt="Zoaholic" className="w-8 h-8 rounded-lg shadow-lg" />
            <span className="font-bold text-lg tracking-tight">Zoaholic</span>
          </div>
        </div>
        <NavContent />
      </aside>

      {/* Mobile Menu Overlay */}
      {mobileMenuOpen && (
        <div 
          className="fixed inset-0 bg-black/50 z-40 md:hidden"
          onClick={() => setMobileMenuOpen(false)}
        />
      )}

      {/* Mobile Sidebar */}
      <aside className={`fixed inset-y-0 left-0 w-64 bg-card border-r border-border flex flex-col z-50 transform transition-transform duration-300 ease-in-out md:hidden ${
        mobileMenuOpen ? 'translate-x-0' : '-translate-x-full'
      }`}>
        <div className="h-16 flex items-center justify-between px-6 border-b border-border">
          <div className="flex items-center gap-2">
            <img src="/zoaholic.png" alt="Zoaholic" className="w-8 h-8 rounded-lg shadow-lg" />
            <span className="font-bold text-lg tracking-tight">Zoaholic</span>
          </div>
          <button
            onClick={() => setMobileMenuOpen(false)}
            className="p-2 rounded-lg hover:bg-muted transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <NavContent />
      </aside>

      {/* Main Content */}
      <div className="flex-1 flex flex-col min-w-0">
        <header className="h-16 border-b border-border flex items-center px-4 md:px-8 bg-background/80 flex-shrink-0 backdrop-blur-sm">
          {/* Mobile Menu Button */}
          <button
            onClick={() => setMobileMenuOpen(true)}
            className="p-2 rounded-lg hover:bg-muted transition-colors md:hidden mr-2"
          >
            <Menu className="w-5 h-5" />
          </button>
          
          <h2 className="text-lg font-semibold text-foreground flex items-center gap-2">
            {navItems.find(item => item.id === location.pathname)?.label || 'Zoaholic'}
          </h2>
        </header>
        <main className="flex-1 overflow-auto p-4 md:p-8 bg-muted/20">
          <div className="max-w-6xl mx-auto h-full">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
