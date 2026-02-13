import { useState, useRef, useEffect, KeyboardEvent } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import {
  Send, Settings2, Trash2, RefreshCw, Copy, ChevronDown, ChevronRight,
  Brain, MessageSquare, Zap, MoreVertical, Edit3, CheckCheck, Loader2,
  Terminal, Sparkles, Blocks, Thermometer, X, Key, CheckCircle2, AlertCircle, SlidersHorizontal
} from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import * as Switch from '@radix-ui/react-switch';

// ========== Types ==========
interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  reasoning_content?: string;
  isTyping?: boolean;
}

interface ExternalClient {
  name: string;
  icon: string;
  link: string;
}

export default function Playground() {
  const { token } = useAuthStore();

  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [loadingModels, setLoadingModels] = useState(false);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);

  const [temperature, setTemperature] = useState(0.7);
  const [stream, setStream] = useState(true);

  const [showThinking, setShowThinking] = useState<Record<number, boolean>>({});
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editValue, setEditValue] = useState('');
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);
  const [showExternalClients, setShowExternalClients] = useState(false);

  const [externalClients, setExternalClients] = useState<ExternalClient[]>([]);
  const [activeClient, setActiveClient] = useState<ExternalClient | null>(null);

  const [showMobileSettings, setShowMobileSettings] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const getExternalLink = (template: string) => {
    const address = window.location.origin;
    // 外部客户端链接仍使用管理员 API Key（不是 JWT）。
    // 这里从后端 /auth/me 获取（前端不直接存储 admin_api_key）。
    return template.replace('{key}', '').replace('{address}', address);
  };

  useEffect(() => {
    fetchModels();
    fetchPreferences();
  }, []);

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const fetchPreferences = async () => {
    if (!token) return;
    try {
      const res = await apiFetch('/v1/api_config', {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (res.ok) {
        const data = await res.json();
        const prefs = data.api_config?.preferences || data.preferences || {};

        if (prefs.external_clients && Array.isArray(prefs.external_clients)) {
          setExternalClients(prefs.external_clients);
        }
      }
    } catch (err) {
      console.error('Failed to fetch preferences', err);
    }
  };

  const fetchModels = async () => {
    if (!token) return;
    setLoadingModels(true);
    try {
      const res = await apiFetch('/v1/models', {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (res.ok) {
        const data = await res.json();
        const modelIds = data.data.map((m: any) => m.id || m.name || m);
        setModels(modelIds);
        if (modelIds.length > 0 && !selectedModel) {
          setSelectedModel(modelIds[0]);
        }
      }
    } catch (err) {
      console.error('Failed to fetch models');
    } finally {
      setLoadingModels(false);
    }
  };

  const sendMessage = async (newMessages: ChatMessage[]) => {
    if (!token || !selectedModel) return;

    setIsGenerating(true);
    const msgList = [...newMessages];

    const requestMessages = systemPrompt
      ? [{ role: 'system', content: systemPrompt }, ...msgList]
      : msgList;

    try {
      const res = await apiFetch('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`
        },
        body: JSON.stringify({
          model: selectedModel,
          messages: requestMessages,
          temperature,
          stream
        })
      });

      if (!res.ok) throw new Error('API Request Failed');

      if (stream) {
        const reader = res.body?.getReader();
        const decoder = new TextDecoder();

        const assistantIndex = msgList.length;
        setMessages([...msgList, { role: 'assistant', content: '', reasoning_content: '', isTyping: true }]);

        let fullContent = '';
        let fullReasoning = '';
        let buffer = '';

        while (reader) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed || !trimmed.startsWith('data:')) continue;
            const dataStr = trimmed.slice(5).trim();
            if (dataStr === '[DONE]') continue;

            try {
              const data = JSON.parse(dataStr);
              const delta = data.choices[0]?.delta;

              if (delta?.reasoning_content) {
                fullReasoning += delta.reasoning_content;
              }
              if (delta?.content) {
                fullContent += delta.content;
              }

              setMessages(prev => {
                const updated = [...prev];
                updated[assistantIndex] = {
                  role: 'assistant',
                  content: fullContent,
                  reasoning_content: fullReasoning,
                  isTyping: true
                };
                return updated;
              });
            } catch (e) { }
          }
        }

        setMessages(prev => {
          const updated = [...prev];
          updated[assistantIndex].isTyping = false;
          return updated;
        });

      } else {
        const data = await res.json();
        const assistantMsg = data.choices[0]?.message;
        setMessages([...msgList, { role: 'assistant', content: assistantMsg.content }]);
      }
    } catch (err) {
      console.error(err);
    } finally {
      setIsGenerating(false);
    }
  };

  const handleSend = () => {
    if (!inputValue.trim() || isGenerating) return;
    const userMsg: ChatMessage = { role: 'user', content: inputValue.trim() };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInputValue('');
    if (textareaRef.current) textareaRef.current.style.height = 'auto';
    sendMessage(newMessages);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const retryMessage = (index: number) => {
    if (isGenerating) return;
    const newMessages = messages.slice(0, index);
    setMessages(newMessages);
    sendMessage(newMessages);
  };

  const deleteMessage = (index: number) => {
    setMessages(messages.slice(0, index));
  };

  const copyMessage = (index: number, content: string) => {
    navigator.clipboard.writeText(content);
    setCopiedIndex(index);
    setTimeout(() => setCopiedIndex(null), 2000);
  };

  const startEditing = (index: number, content: string) => {
    setEditingIndex(index);
    setEditValue(content);
  };

  const saveEdit = (index: number) => {
    if (!editValue.trim()) return;
    if (editValue !== messages[index].content) {
      const newMessages = messages.slice(0, index + 1);
      newMessages[index].content = editValue;
      setMessages(newMessages);
      if (newMessages[index].role === 'user') {
        sendMessage(newMessages);
      }
    }
    setEditingIndex(null);
  };

  const toggleThinking = (index: number) => {
    setShowThinking(prev => ({ ...prev, [index]: !prev[index] }));
  };

  const clearChat = () => {
    if (confirm('确定清空所有对话历史吗？')) {
      setMessages([]);
    }
  };

  // Render External Client Iframe
  if (activeClient) {
    return (
      <div className="flex flex-col h-full animate-in fade-in duration-300">
        <div className="h-12 bg-card border-b border-border flex items-center justify-between px-4 flex-shrink-0">
          <div className="flex items-center gap-3">
            <span className="text-lg">{activeClient.icon}</span>
            <span className="font-medium text-foreground">{activeClient.name}</span>
            <span className="text-xs text-muted-foreground bg-muted px-2 py-0.5 rounded">外部客户端</span>
          </div>
          <button
            onClick={() => setActiveClient(null)}
            className="p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors"
            title="关闭并返回"
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <iframe
          src={getExternalLink(activeClient.link)}
          className="flex-1 w-full border-0 bg-white"
          allow="clipboard-read; clipboard-write"
        />
      </div>
    );
  }

  return (
    <div className="flex h-full animate-in fade-in duration-500 font-sans relative">
      {/* Left: Chat Area */}
      <div className="flex-1 flex flex-col min-w-0 bg-background">

        {/* Chat Header */}
        <div className="h-14 border-b border-border flex items-center px-4 md:px-6 justify-between bg-card/80 backdrop-blur-sm z-10 flex-shrink-0">
          <div className="flex items-center gap-2 text-foreground font-bold">
            <Terminal className="w-5 h-5 text-primary" />
            <span className="hidden sm:inline">Console Playground</span>
            <span className="sm:hidden">Playground</span>
          </div>
          <div className="flex items-center gap-2">
            {token ? (
              <span className="hidden sm:flex items-center gap-1 text-xs bg-emerald-500/10 text-emerald-600 dark:text-emerald-500 px-2 py-1 rounded-md border border-emerald-500/20">
                <CheckCircle2 className="w-3 h-3" />
                <span className="font-mono">JWT 已登录</span>
              </span>
            ) : (
              <span className="hidden sm:flex items-center gap-1 text-xs bg-red-500/10 text-red-600 dark:text-red-500 px-2 py-1 rounded-md border border-red-500/20">
                <AlertCircle className="w-3 h-3" /> 未登录
              </span>
            )}
            {isGenerating && <span className="text-xs text-blue-600 dark:text-blue-500 flex items-center gap-1"><Loader2 className="w-3 h-3 animate-spin" /> <span className="hidden sm:inline">生成中</span></span>}
            <button onClick={() => setShowMobileSettings(true)} className="md:hidden p-2 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors">
              <SlidersHorizontal className="w-4 h-4" />
            </button>
            <button onClick={clearChat} className="text-xs text-muted-foreground hover:text-red-500 flex items-center gap-1 transition-colors px-2 py-1 bg-muted rounded-md border border-border">
              <Trash2 className="w-3 h-3" /> <span className="hidden sm:inline">清空</span>
            </button>
          </div>
        </div>

        {/* Message List */}
        <div className="flex-1 overflow-y-auto px-4 md:px-12 py-8 space-y-6">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
              <Sparkles className="w-12 h-12 mb-4 opacity-50" />
              <h2 className="text-xl font-bold text-foreground mb-2">Zoaholic AI Playground</h2>
              <p className="text-sm">在右侧选择模型配置参数，开始测试你的 AI 助手。</p>
            </div>
          ) : (
            messages.map((msg, idx) => (
              <div key={idx} className="flex flex-col max-w-4xl mx-auto w-full group">
                <div className="flex items-center gap-2 mb-2 text-xs font-medium text-muted-foreground">
                  {msg.role === 'user' ? (
                    <span className="flex items-center gap-1 text-emerald-600 dark:text-emerald-500"><MessageSquare className="w-3.5 h-3.5" /> User</span>
                  ) : (
                    <span className="flex items-center gap-1 text-blue-600 dark:text-blue-400"><Brain className="w-3.5 h-3.5" /> {selectedModel || 'Assistant'}</span>
                  )}
                </div>

                <div className={`p-4 rounded-xl border transition-colors ${msg.role === 'user' ? 'bg-muted/50 border-border text-foreground' : 'bg-transparent border-transparent text-foreground'}`}>

                  {msg.reasoning_content && (
                    <div className="mb-4 bg-muted/50 border border-border rounded-lg overflow-hidden">
                      <button
                        onClick={() => toggleThinking(idx)}
                        className="w-full flex items-center gap-2 px-3 py-2 text-xs text-muted-foreground bg-muted hover:text-foreground transition-colors"
                      >
                        <Zap className="w-3.5 h-3.5 text-yellow-500" />
                        <span>思维链 (Reasoning)</span>
                        <ChevronDown className={`w-3.5 h-3.5 ml-auto transition-transform ${showThinking[idx] ? 'rotate-180' : ''}`} />
                      </button>
                      {showThinking[idx] && (
                        <div className="px-4 py-3 text-sm text-muted-foreground font-mono whitespace-pre-wrap border-t border-border">
                          {msg.reasoning_content}
                        </div>
                      )}
                    </div>
                  )}

                  {editingIndex === idx ? (
                    <div className="space-y-2">
                      <textarea
                        value={editValue}
                        onChange={e => setEditValue(e.target.value)}
                        className="w-full bg-background border border-primary text-foreground p-3 rounded-lg text-sm focus:outline-none min-h-[100px]"
                        autoFocus
                      />
                      <div className="flex justify-end gap-2">
                        <button onClick={() => setEditingIndex(null)} className="px-3 py-1.5 text-xs bg-muted text-foreground rounded hover:bg-muted/80">取消</button>
                        <button onClick={() => saveEdit(idx)} className="px-3 py-1.5 text-xs bg-primary text-primary-foreground rounded hover:bg-primary/90">保存{msg.role === 'user' ? '并重发' : ''}</button>
                      </div>
                    </div>
                  ) : (
                    <div className="text-sm leading-relaxed whitespace-pre-wrap">
                      {msg.content}
                      {msg.isTyping && <span className="inline-block w-2 h-4 bg-muted-foreground ml-1 animate-pulse align-middle" />}
                    </div>
                  )}
                </div>

                {editingIndex !== idx && !msg.isTyping && (
                  <div className="flex items-center gap-1 mt-1 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button onClick={() => copyMessage(idx, msg.content)} className="p-1.5 text-muted-foreground hover:text-foreground rounded-md hover:bg-muted transition-colors" title="复制">
                      {copiedIndex === idx ? <CheckCheck className="w-3.5 h-3.5 text-emerald-500" /> : <Copy className="w-3.5 h-3.5" />}
                    </button>
                    <button onClick={() => startEditing(idx, msg.content)} className="p-1.5 text-muted-foreground hover:text-foreground rounded-md hover:bg-muted transition-colors" title="编辑">
                      <Edit3 className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => retryMessage(idx)} className="p-1.5 text-muted-foreground hover:text-blue-500 rounded-md hover:bg-muted transition-colors" title={msg.role === 'user' ? '重发此消息' : '重新生成'}>
                      <RefreshCw className="w-3.5 h-3.5" />
                    </button>
                    <DropdownMenu.Root>
                      <DropdownMenu.Trigger asChild>
                        <button className="p-1.5 text-muted-foreground hover:text-foreground rounded-md hover:bg-muted transition-colors">
                          <MoreVertical className="w-3.5 h-3.5" />
                        </button>
                      </DropdownMenu.Trigger>
                      <DropdownMenu.Portal>
                        <DropdownMenu.Content className="min-w-[120px] bg-card border border-border rounded-md shadow-xl p-1 z-50 text-sm">
                          <DropdownMenu.Item onClick={() => deleteMessage(idx)} className="flex items-center gap-2 px-2 py-1.5 text-red-500 hover:bg-red-500/10 rounded cursor-pointer outline-none">
                            <Trash2 className="w-3.5 h-3.5" /> 删除以下所有
                          </DropdownMenu.Item>
                        </DropdownMenu.Content>
                      </DropdownMenu.Portal>
                    </DropdownMenu.Root>
                  </div>
                )}
              </div>
            ))
          )}
          <div ref={messagesEndRef} className="h-4" />
        </div>

        {/* Input Area */}
        <div className="p-4 md:px-12 bg-card border-t border-border flex-shrink-0">
          <div className="max-w-4xl mx-auto relative bg-muted border border-border focus-within:border-primary rounded-xl overflow-hidden transition-colors">
            <textarea
              ref={textareaRef}
              value={inputValue}
              onChange={e => {
                setInputValue(e.target.value);
                e.target.style.height = 'auto';
                e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`;
              }}
              onKeyDown={handleKeyDown}
              placeholder={token ? "输入消息 (Shift + Enter 换行)..." : "请先登录..."}
              disabled={!token || isGenerating}
              className="w-full bg-transparent text-foreground p-4 pr-12 text-sm max-h-[200px] resize-none focus:outline-none placeholder:text-muted-foreground disabled:opacity-50"
              rows={1}
            />
            <button
              onClick={handleSend}
              disabled={!inputValue.trim() || isGenerating || !token}
              className="absolute right-3 bottom-3 p-2 bg-primary hover:bg-primary/90 text-primary-foreground rounded-lg disabled:opacity-50 disabled:hover:bg-primary transition-all shadow-md"
            >
              {isGenerating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            </button>
          </div>
          <div className="max-w-4xl mx-auto mt-2 text-xs text-muted-foreground flex justify-between">
            <span>使用 Shift + Enter 换行</span>
            <span>已启用 {stream ? '流式输出' : '非流式输出'}</span>
          </div>
        </div>
      </div>


      {/* Right: Parameters Panel */}
      <div className="w-80 bg-card border-l border-border flex-shrink-0 flex-col hidden md:flex h-full">
        <div className="h-14 border-b border-border flex items-center px-4 font-medium text-foreground gap-2 flex-shrink-0">
          <Settings2 className="w-4 h-4 text-primary" />
          控制台参数
        </div>

        <div className="flex-1 overflow-y-auto p-4 space-y-6">

          {/* System Prompt */}
          <div className="space-y-2">
            <label className="text-xs font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-1.5"><Brain className="w-3.5 h-3.5" /> System Prompt</label>
            <textarea
              value={systemPrompt}
              onChange={e => setSystemPrompt(e.target.value)}
              placeholder="你是一个有帮助的 AI 助手..."
              className="w-full bg-muted border border-border focus:border-primary p-3 rounded-lg text-sm text-foreground h-24 resize-none outline-none transition-colors"
            />
          </div>

          {/* Model Selection */}
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">Model</label>
              <button onClick={fetchModels} className={`text-muted-foreground hover:text-primary ${loadingModels ? 'animate-spin' : ''}`} title="刷新模型列表">
                <RefreshCw className="w-3 h-3" />
              </button>
            </div>
            <select
              value={selectedModel}
              onChange={e => setSelectedModel(e.target.value)}
              className="w-full bg-muted border border-border focus:border-primary px-3 py-2 rounded-lg text-sm text-foreground outline-none"
            >
              <option value="" disabled>选择模型...</option>
              {models.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </div>

          {/* Temperature */}
          <div className="space-y-4 pt-2">
            <div>
              <div className="flex justify-between items-center mb-2">
                <label className="text-xs font-semibold text-muted-foreground flex items-center gap-1"><Thermometer className="w-3.5 h-3.5" /> Temperature</label>
                <span className="text-xs font-mono bg-muted px-1.5 py-0.5 rounded text-foreground">{temperature}</span>
              </div>
              <input
                type="range"
                min="0" max="2" step="0.1"
                value={temperature}
                onChange={e => setTemperature(parseFloat(e.target.value))}
                className="w-full accent-primary"
              />
            </div>
          </div>

          {/* Stream Switch */}
          <div className="flex items-center justify-between py-2 border-t border-b border-border">
            <span className="text-sm font-medium text-foreground">Stream Output</span>
            <Switch.Root checked={stream} onCheckedChange={setStream} className="w-11 h-6 bg-muted rounded-full data-[state=checked]:bg-primary transition-colors">
              <Switch.Thumb className="block w-5 h-5 bg-white rounded-full transition-transform translate-x-0.5 data-[state=checked]:translate-x-[22px]" />
            </Switch.Root>
          </div>

          {/* External Clients Accordion */}
          <div className="border border-border rounded-lg bg-muted/50 overflow-hidden">
            <button
              onClick={() => setShowExternalClients(!showExternalClients)}
              className="w-full flex items-center justify-between p-3 text-sm font-medium text-foreground hover:bg-muted transition-colors"
            >
              <span className="flex items-center gap-1.5"><Blocks className="w-4 h-4 text-emerald-500" /> 第三方客户端</span>
              {showExternalClients ? <ChevronDown className="w-4 h-4 text-muted-foreground" /> : <ChevronRight className="w-4 h-4 text-muted-foreground" />}
            </button>
            {showExternalClients && (
              <div className="p-2 space-y-1 bg-background">
                {externalClients.map((client, idx) => (
                  <button
                    key={idx}
                    onClick={() => setActiveClient(client)}
                    className="w-full flex items-center justify-between p-2 rounded-md hover:bg-muted text-sm text-muted-foreground hover:text-foreground transition-colors text-left"
                  >
                    <span className="flex items-center gap-2">
                      <span className="text-base leading-none">{client.icon}</span>
                      <span>{client.name}</span>
                    </span>
                  </button>
                ))}
                <div className="text-xs text-muted-foreground px-2 pt-2 border-t border-border mt-2">
                  点击将在当前页面内嵌显示第三方客户端
                </div>
              </div>
            )}
          </div>

        </div>
      </div>

      {/* Mobile Settings Dialog */}
      <Dialog.Root open={showMobileSettings} onOpenChange={setShowMobileSettings}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40" />
          <Dialog.Content className="fixed bottom-0 left-0 right-0 bg-background border-t border-border rounded-t-2xl z-50 max-h-[80vh] overflow-y-auto animate-in slide-in-from-bottom duration-300">
            <div className="p-4 border-b border-border flex items-center justify-between">
              <Dialog.Title className="text-lg font-bold text-foreground flex items-center gap-2">
                <Settings2 className="w-5 h-5 text-primary" /> 参数配置
              </Dialog.Title>
              <Dialog.Close className="p-1 text-muted-foreground hover:text-foreground">
                <X className="w-5 h-5" />
              </Dialog.Close>
            </div>
            <div className="p-4 space-y-5">
              {/* Auth Status */}
              <div className={`p-3 rounded-lg flex items-center gap-2 ${token ? 'bg-emerald-500/10 border border-emerald-500/20' : 'bg-red-500/10 border border-red-500/20'}`}>
                {token ? (
                  <><CheckCircle2 className="w-4 h-4 text-emerald-500" /><span className="text-sm text-emerald-600 dark:text-emerald-400">已登录（JWT）</span></>
                ) : (
                  <><AlertCircle className="w-4 h-4 text-red-500" /><span className="text-sm text-red-600 dark:text-red-400">未登录，请先登录</span></>
                )}
              </div>

              {/* System Prompt */}
              <div className="space-y-2">
                <label className="text-xs font-semibold text-muted-foreground uppercase">System Prompt</label>
                <textarea
                  value={systemPrompt}
                  onChange={e => setSystemPrompt(e.target.value)}
                  placeholder="你是一个有帮助的 AI 助手..."
                  className="w-full bg-muted border border-border p-3 rounded-lg text-sm text-foreground h-20 resize-none outline-none"
                />
              </div>

              {/* Model Selection */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <label className="text-xs font-semibold text-muted-foreground uppercase">Model</label>
                  <button onClick={fetchModels} className={`text-muted-foreground hover:text-primary ${loadingModels ? 'animate-spin' : ''}`}>
                    <RefreshCw className="w-3.5 h-3.5" />
                  </button>
                </div>
                <select
                  value={selectedModel}
                  onChange={e => setSelectedModel(e.target.value)}
                  className="w-full bg-muted border border-border px-3 py-2.5 rounded-lg text-sm text-foreground outline-none"
                >
                  <option value="" disabled>选择模型...</option>
                  {models.map(m => <option key={m} value={m}>{m}</option>)}
                </select>
              </div>

              {/* Temperature */}
              <div>
                <div className="flex justify-between items-center mb-2">
                  <label className="text-xs font-semibold text-muted-foreground uppercase">Temperature</label>
                  <span className="text-xs font-mono bg-muted px-1.5 py-0.5 rounded text-foreground">{temperature}</span>
                </div>
                <input
                  type="range"
                  min="0" max="2" step="0.1"
                  value={temperature}
                  onChange={e => setTemperature(parseFloat(e.target.value))}
                  className="w-full accent-primary"
                />
              </div>

              {/* Stream Switch */}
              <div className="flex items-center justify-between py-2">
                <span className="text-sm font-medium text-foreground">Stream Output</span>
                <Switch.Root checked={stream} onCheckedChange={setStream} className="w-11 h-6 bg-muted rounded-full data-[state=checked]:bg-primary">
                  <Switch.Thumb className="block w-5 h-5 bg-white rounded-full transition-transform translate-x-0.5 data-[state=checked]:translate-x-[22px]" />
                </Switch.Root>
              </div>

              {/* Third-party Clients */}
              <div className="border border-border rounded-lg overflow-hidden">
                <button
                  onClick={() => setShowExternalClients(!showExternalClients)}
                  className="w-full flex items-center justify-between p-3 text-sm font-medium text-foreground bg-muted"
                >
                  <span className="flex items-center gap-1.5"><Blocks className="w-4 h-4 text-emerald-500" /> 第三方客户端</span>
                  {showExternalClients ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                </button>
                {showExternalClients && (
                  <div className="p-2 space-y-1">
                    {externalClients.map((client, idx) => (
                      <button
                        key={idx}
                        onClick={() => { setActiveClient(client); setShowMobileSettings(false); }}
                        className="w-full flex items-center gap-2 p-2 rounded-md hover:bg-muted text-sm text-muted-foreground hover:text-foreground text-left"
                      >
                        <span className="text-base">{client.icon}</span>
                        <span>{client.name}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

    </div>
  );
}
