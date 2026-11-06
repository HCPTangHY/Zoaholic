import { useState, useRef, useEffect, KeyboardEvent, ChangeEvent, DragEvent } from 'react';
import { useAuthStore } from '../store/authStore';
import { apiFetch } from '../lib/api';
import { MarkdownRenderer } from '../components/MarkdownRenderer';
import {
  PlaygroundAttachment, MAX_PLAYGROUND_ATTACHMENT_COUNT, MAX_PLAYGROUND_ATTACHMENT_SIZE,
  decodeAttachmentText, formatAttachmentSize, isSupportedPlaygroundAttachment, readPlaygroundAttachment
} from '../lib/playgroundAttachments';
import {
  Send, Settings2, Trash2, RefreshCw, Copy, ChevronDown, ChevronRight,
  Brain, MessageSquare, Zap, MoreVertical, Edit3, CheckCheck, Loader2,
  Sparkles, Blocks, Thermometer, X, CheckCircle2, AlertCircle,
  SlidersHorizontal, Paperclip, Image as ImageIcon, FileText, Eye, Key
} from 'lucide-react';
import * as Dialog from '@radix-ui/react-dialog';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import * as Switch from '@radix-ui/react-switch';

// ========== Types ==========
interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  attachments?: PlaygroundAttachment[];
  reasoning_content?: string;
  isTyping?: boolean;
}

type RequestContentItem =
  | { type: 'text'; text: string }
  | { type: 'image_url'; image_url: { url: string } }
  | { type: 'file'; file: { url: string; mime_type: string; filename: string } };

interface ExternalClient {
  name: string;
  icon: string;
  link: string;
}

interface PlaygroundKeyOption {
  index: number;
  name: string | null;
  masked_key: string;
  api: string;
}

export default function Playground() {
  const { token } = useAuthStore();

  const [models, setModels] = useState<string[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [loadingModels, setLoadingModels] = useState(false);

  const [playgroundKeys, setPlaygroundKeys] = useState<PlaygroundKeyOption[]>([]);
  const [selectedPlaygroundKey, setSelectedPlaygroundKey] = useState('');
  const [loadingPlaygroundKeys, setLoadingPlaygroundKeys] = useState(false);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputValue, setInputValue] = useState('');
  const [systemPrompt, setSystemPrompt] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);

  const [temperature, setTemperature] = useState(0.7);
  const [stream, setStream] = useState(true);

  const [markdownRendering, setMarkdownRendering] = useState(true);

  const [showThinking, setShowThinking] = useState<Record<number, boolean>>({});
  const [editingIndex, setEditingIndex] = useState<number | null>(null);
  const [editValue, setEditValue] = useState('');
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);
  const [showExternalClients, setShowExternalClients] = useState(false);

  const [externalClients, setExternalClients] = useState<ExternalClient[]>([]);
  const [activeClient, setActiveClient] = useState<ExternalClient | null>(null);

  const [showMobileSettings, setShowMobileSettings] = useState(false);

  const [pendingAttachments, setPendingAttachments] = useState<PlaygroundAttachment[]>([]);
  const [attachmentError, setAttachmentError] = useState<string | null>(null);
  const [previewAttachment, setPreviewAttachment] = useState<PlaygroundAttachment | null>(null);
  const [isDraggingFiles, setIsDraggingFiles] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragCounterRef = useRef(0);

  const containsDraggedFiles = (dataTransfer?: DataTransfer | null) => Array.from(dataTransfer?.types || []).includes('Files');

  const getExternalLink = (template: string) => {
    const address = window.location.origin;
    return template.replace('{key}', '').replace('{address}', address);
  };

  useEffect(() => {
    if (!token) return;
    // 修改原因：Key 下拉现在读取全局用户 api_keys，不再随模型变化刷新。
    // 修改方式：登录 token 可用后只拉取偏好和 Key 列表，模型列表交给独立 effect 按当前身份刷新。
    // 目的：避免模型切换影响用户身份选择，同时保留管理员 token 拉取 Key 列表的安全边界。
    fetchPreferences();
    fetchPlaygroundKeys();
  }, [token]);

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    fetchModels();
  }, [token, selectedPlaygroundKey]);

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
      // 修改原因：Playground 的 Key 下拉现在代表全局用户 api_key，模型列表也要按所选用户权限返回。
      // 修改方式：复用 Authorization 构造函数，用所选 api 替换原始登录 token，不再发送额外的 Key header。
      // 目的：让模型刷新和聊天请求始终模拟同一个用户身份。
      const selectedApi = getSelectedPlaygroundApi();
      const res = await fetch('/v1/models', {
        headers: { Authorization: `Bearer ${selectedApi || token}` }
      });
      if (res.ok) {
        const data = await res.json();
        const modelIds = data.data.map((m: any) => m.id || m.name || m);
        setModels(modelIds);
        setSelectedModel(prev => {
          if (modelIds.length === 0) return '';
          return prev && modelIds.includes(prev) ? prev : modelIds[0];
        });
      }
    } catch (err) {
      console.error('Failed to fetch models');
    } finally {
      setLoadingModels(false);
    }
  };

  const getSelectedPlaygroundApi = () => {
    return playgroundKeys.find(item => String(item.index) === selectedPlaygroundKey)?.api || '';
  };

  const buildPlaygroundAuthHeaders = () => {
    // 修改原因：选择具体 Key 后，Playground 要以该全局用户身份请求 chat 和 models。
    // 修改方式：从已加载的 Key 选项中取完整 api 值，替换 Authorization Bearer token；未选择时继续使用原始登录 token。
    // 目的：让后端按普通用户鉴权路径处理请求，不再依赖额外的 Playground 专用请求头。
    const selectedApi = getSelectedPlaygroundApi();
    return { Authorization: `Bearer ${selectedApi || token}` };
  };

  const fetchPlaygroundKeys = async () => {
    if (!token) {
      setPlaygroundKeys([]);
      setSelectedPlaygroundKey('');
      return;
    }
    setLoadingPlaygroundKeys(true);
    try {
      const res = await apiFetch('/v1/playground/keys', {
        headers: { Authorization: `Bearer ${token}` }
      });
      if (!res.ok) {
        setPlaygroundKeys([]);
        setSelectedPlaygroundKey('');
        return;
      }
      const data = await res.json();
      const keys = Array.isArray(data.keys) ? data.keys : [];
      // 修改原因：Key 列表现在来自全局 api_keys，只需要在刷新后校验当前索引是否仍存在。
      // 修改方式：按后端返回的 index 保留有效选择，失效时回到“自动”。
      // 目的：避免向请求头写入已经不存在的用户 api_key。
      setPlaygroundKeys(keys);
      setSelectedPlaygroundKey(prev => keys.some((item: PlaygroundKeyOption) => String(item.index) === prev) ? prev : '');
    } catch (err) {
      console.error('Failed to fetch playground keys');
      setPlaygroundKeys([]);
      setSelectedPlaygroundKey('');
    } finally {
      setLoadingPlaygroundKeys(false);
    }
  };

  const getPlaygroundKeyLabel = (keyOption: PlaygroundKeyOption) => {
    // 修改原因：Key 选项现在表示用户身份，前端只需要展示用户名称和脱敏后的全局 api_key。
    // 修改方式：有 name 时显示 name (masked_key)，无 name 时只显示 masked_key。
    // 目的：让下拉框含义与 api.yaml 的 api_keys 数组保持一致。
    return keyOption.name ? `${keyOption.name} (${keyOption.masked_key})` : keyOption.masked_key;
  };

  const buildPlaygroundRequestHeaders = () => {
    return {
      'Content-Type': 'application/json',
      ...buildPlaygroundAuthHeaders()
    };
  };

  const serializeMessage = (message: ChatMessage) => {
    if (!message.attachments?.length) {
      return { role: message.role, content: message.content };
    }

    const content: RequestContentItem[] = [];
    if (message.content.trim()) {
      content.push({ type: 'text', text: message.content.trim() });
    }

    for (const attachment of message.attachments) {
      if (attachment.kind === 'image') {
        content.push({
          type: 'image_url',
          image_url: { url: attachment.dataUrl }
        });
      } else {
        content.push({
          type: 'file',
          file: {
            url: attachment.dataUrl,
            mime_type: attachment.mimeType,
            filename: attachment.name
          }
        });
      }
    }

    return {
      role: message.role,
      content: content.length ? content : [{ type: 'text', text: '' }]
    };
  };

  const buildAttachmentError = (message: string) => `附件校验失败：${message}`;

  const openAttachmentPreview = (attachment: PlaygroundAttachment) => setPreviewAttachment(attachment);

  const openFilePicker = () => {
    if (isGenerating || !token) return;
    fileInputRef.current?.click();
  };

  const removePendingAttachment = (attachmentId: string) => {
    setPendingAttachments(prev => prev.filter(item => item.id !== attachmentId));
  };

  const appendAttachments = async (files: File[]) => {
    if (!files.length) return;

    setAttachmentError(null);

    if (pendingAttachments.length + files.length > MAX_PLAYGROUND_ATTACHMENT_COUNT) {
      setAttachmentError(buildAttachmentError(`最多上传 ${MAX_PLAYGROUND_ATTACHMENT_COUNT} 个附件`));
      return;
    }

    const invalidFile = files.find(file => !isSupportedPlaygroundAttachment(file));
    if (invalidFile) {
      setAttachmentError(buildAttachmentError(`暂不支持 ${invalidFile.name}，目前支持图片、PDF、TXT、Markdown、JSON、CSV、XML、YAML`));
      return;
    }

    const oversizedFile = files.find(file => file.size > MAX_PLAYGROUND_ATTACHMENT_SIZE);
    if (oversizedFile) {
      setAttachmentError(buildAttachmentError(`${oversizedFile.name} 超过 ${(MAX_PLAYGROUND_ATTACHMENT_SIZE / 1024 / 1024).toFixed(0)} MB 限制`));
      return;
    }

    try {
      const nextAttachments = await Promise.all(files.map(file => readPlaygroundAttachment(file)));
      setPendingAttachments(prev => [...prev, ...nextAttachments]);
    } catch (error) {
      setAttachmentError(buildAttachmentError(error instanceof Error ? error.message : '附件读取失败'));
    }
  };

  const handleAttachmentSelect = async (e: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    e.target.value = '';
    await appendAttachments(files);
  };

  const resetDragState = () => {
    dragCounterRef.current = 0;
    setIsDraggingFiles(false);
  };

  useEffect(() => {
    const clearDragState = () => resetDragState();

    window.addEventListener('drop', clearDragState);
    window.addEventListener('dragend', clearDragState);

    return () => {
      window.removeEventListener('drop', clearDragState);
      window.removeEventListener('dragend', clearDragState);
    };
  }, []);

  const handleComposerDragEnter = (e: DragEvent<HTMLDivElement>) => {
    if (!token || isGenerating || !containsDraggedFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current += 1;
    setIsDraggingFiles(true);
  };

  const handleComposerDragOver = (e: DragEvent<HTMLDivElement>) => {
    if (!token || isGenerating || !containsDraggedFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = 'copy';
  };

  const handleComposerDragLeave = (e: DragEvent<HTMLDivElement>) => {
    if (!token || isGenerating || !containsDraggedFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current = Math.max(0, dragCounterRef.current - 1);
    if (dragCounterRef.current === 0) {
      setIsDraggingFiles(false);
    }
  };

  const handleComposerDrop = async (e: DragEvent<HTMLDivElement>) => {
    if (!token || isGenerating || !containsDraggedFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.stopPropagation();
    resetDragState();
    await appendAttachments(Array.from(e.dataTransfer.files || []));
  };

  const getAttachmentIcon = (attachment: PlaygroundAttachment) => {
    if (attachment.kind === 'image') {
      return <ImageIcon className="w-3 h-3" />;
    }
    return <FileText className="w-3 h-3" />;
  };

  const sendMessage = async (newMessages: ChatMessage[]) => {
    if (!token || !selectedModel) return;

    setIsGenerating(true);
    const msgList = [...newMessages];

    // 本地错误提示不应作为 system 消息发给模型。
    const chatMessages = msgList.filter(message => message.role !== 'system').map(serializeMessage);
    const requestMessages = systemPrompt
      ? [{ role: 'system', content: systemPrompt }, ...chatMessages]
      : chatMessages;

    const appendSystemError = (text: string) => {
      const msg = (text || '未知错误').toString();
      setMessages(prev => [...prev, { role: 'system', content: `请求失败：${msg}` }]);
    };

    const parseErrorFromResponse = async (res: Response) => {
      const rawText = await res.text().catch(() => '');
      let details = rawText;
      try {
        details = JSON.stringify(JSON.parse(rawText), null, 2);
      } catch {
        // 非 JSON 响应（如代理错误页面）保留原文。
      }
      return [`HTTP ${res.status} ${res.statusText}`.trim(), details].filter(Boolean).join('\n\n');
    };

    try {
      const chatApi = getSelectedPlaygroundApi();
      const res = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${chatApi || token}`
        },
        body: JSON.stringify({
          model: selectedModel,
          messages: requestMessages,
          temperature,
          stream
        })
      });

      if (!res.ok) {
        const errText = await parseErrorFromResponse(res);
        appendSystemError(errText);
        return;
      }

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

              if (data?.error || data?.choices?.[0]?.error) {
                const errMsg = JSON.stringify(data, null, 2);

                try { await reader.cancel(); } catch { }
                setMessages(prev => {
                  const updated = [...prev];
                  if (updated[assistantIndex]) {
                    updated[assistantIndex] = {
                      role: 'assistant',
                      content: fullContent,
                      reasoning_content: fullReasoning,
                      isTyping: false,
                    };
                  }
                  updated.push({ role: 'system', content: `请求失败：${errMsg}` });
                  return updated;
                });
                return;
              }

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
        if (data?.error || data?.choices?.[0]?.error) {
          appendSystemError(JSON.stringify(data, null, 2));
          return;
        }
        const assistantMsg = data.choices[0]?.message;
        setMessages([...msgList, { role: 'assistant', content: assistantMsg.content }]);
      }
    } catch (err) {
      console.error(err);
      appendSystemError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsGenerating(false);
    }
  };

  const handleSend = () => {
    const trimmedInput = inputValue.trim();
    if ((!trimmedInput && pendingAttachments.length === 0) || isGenerating) return;

    const userMsg: ChatMessage = {
      role: 'user',
      content: trimmedInput,
      attachments: pendingAttachments.length ? pendingAttachments : undefined
    };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInputValue('');
    setPendingAttachments([]);
    resetDragState();
    setAttachmentError(null);
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

  // ========== Sidebar Settings Panel (shared between desktop & mobile) ==========
  const SettingsPanel = ({ isMobile = false }: { isMobile?: boolean }) => (
    <div className={`space-y-4 ${isMobile ? '' : 'text-[13px]'}`}>
      {/* System Prompt */}
      <div>
        <label className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-1 mb-1.5">
          <Brain className="w-3 h-3" /> System Prompt
        </label>
        <textarea
          value={systemPrompt}
          onChange={e => setSystemPrompt(e.target.value)}
          placeholder="你是一个有帮助的 AI 助手..."
          className="w-full bg-muted/60 border border-border focus:border-primary/60 p-2.5 rounded-lg text-[13px] text-foreground h-20 resize-none outline-none transition-colors placeholder:text-muted-foreground/60"
        />
      </div>

      {/* Key Selection */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-1">
            <Key className="w-3 h-3" /> Key
          </label>
          {loadingPlaygroundKeys && <Loader2 className="w-3 h-3 text-muted-foreground animate-spin" />}
        </div>
        <select
          value={selectedPlaygroundKey}
          onChange={e => setSelectedPlaygroundKey(e.target.value)}
          className="w-full bg-muted/60 border border-border focus:border-primary/60 px-2.5 py-2 rounded-lg text-[13px] text-foreground outline-none transition-colors"
        >
          <option value="">自动</option>
          {playgroundKeys.map(keyOption => (
            <option key={keyOption.index} value={String(keyOption.index)}>
              {getPlaygroundKeyLabel(keyOption)}
            </option>
          ))}
        </select>
      </div>

      {/* Model Selection */}
      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider">Model</label>
          <button onClick={fetchModels} className={`text-muted-foreground hover:text-primary transition-colors ${loadingModels ? 'animate-spin' : ''}`} title="刷新模型列表">
            <RefreshCw className="w-3 h-3" />
          </button>
        </div>
        <select
          value={selectedModel}
          onChange={e => setSelectedModel(e.target.value)}
          className="w-full bg-muted/60 border border-border focus:border-primary/60 px-2.5 py-2 rounded-lg text-[13px] text-foreground outline-none transition-colors"
        >
          <option value="" disabled>选择模型...</option>
          {models.map(m => <option key={m} value={m}>{m}</option>)}
        </select>
      </div>

      {/* Temperature */}
      <div>
        <div className="flex justify-between items-center mb-1.5">
          <label className="text-[11px] font-semibold text-muted-foreground uppercase tracking-wider flex items-center gap-1">
            <Thermometer className="w-3 h-3" /> Temperature
          </label>
          <span className="text-[11px] font-mono bg-muted/80 px-1.5 py-0.5 rounded text-foreground/80">{temperature}</span>
        </div>
        <input
          type="range"
          min="0" max="2" step="0.1"
          value={temperature}
          onChange={e => setTemperature(parseFloat(e.target.value))}
          className="w-full accent-primary h-1.5"
        />
      </div>

      {/* Toggle Switches */}
      <div className="space-y-0">
        <div className="flex items-center justify-between py-2.5 border-t border-border">
          <span className="text-[13px] text-foreground/80">Stream</span>
          <Switch.Root checked={stream} onCheckedChange={setStream} className="w-9 h-5 bg-muted rounded-full data-[state=checked]:bg-primary transition-colors">
            <Switch.Thumb className="block w-4 h-4 bg-white rounded-full transition-transform translate-x-0.5 data-[state=checked]:translate-x-[18px] shadow-sm" />
          </Switch.Root>
        </div>
        <div className="flex items-center justify-between py-2.5 border-t border-border">
          <span className="text-[13px] text-foreground/80">Markdown</span>
          <Switch.Root checked={markdownRendering} onCheckedChange={setMarkdownRendering} className="w-9 h-5 bg-muted rounded-full data-[state=checked]:bg-primary transition-colors">
            <Switch.Thumb className="block w-4 h-4 bg-white rounded-full transition-transform translate-x-0.5 data-[state=checked]:translate-x-[18px] shadow-sm" />
          </Switch.Root>
        </div>
      </div>

      {/* External Clients */}
      {externalClients.length > 0 && (
        <div className="border border-border rounded-lg overflow-hidden">
          <button
            onClick={() => setShowExternalClients(!showExternalClients)}
            className="w-full flex items-center justify-between px-3 py-2 text-[13px] font-medium text-foreground/80 hover:bg-muted/50 transition-colors"
          >
            <span className="flex items-center gap-1.5"><Blocks className="w-3.5 h-3.5 text-emerald-500" /> 第三方客户端</span>
            {showExternalClients ? <ChevronDown className="w-3.5 h-3.5 text-muted-foreground" /> : <ChevronRight className="w-3.5 h-3.5 text-muted-foreground" />}
          </button>
          {showExternalClients && (
            <div className="px-1.5 pb-1.5 space-y-0.5 bg-muted/20">
              {externalClients.map((client, idx) => (
                <button
                  key={idx}
                  onClick={() => { setActiveClient(client); if (isMobile) setShowMobileSettings(false); }}
                  className="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md hover:bg-muted text-[13px] text-muted-foreground hover:text-foreground transition-colors text-left"
                >
                  <span className="text-sm leading-none">{client.icon}</span>
                  <span>{client.name}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );

  // Render External Client Iframe
  if (activeClient) {
    return (
      <div className="flex flex-col h-full animate-in fade-in duration-300">
        <div className="h-11 bg-card border-b border-border flex items-center justify-between px-4 flex-shrink-0">
          <div className="flex items-center gap-2.5">
            <span className="text-base">{activeClient.icon}</span>
            <span className="font-medium text-sm text-foreground">{activeClient.name}</span>
            <span className="text-[10px] text-muted-foreground bg-muted px-1.5 py-0.5 rounded">外部客户端</span>
          </div>
          <button
            onClick={() => setActiveClient(null)}
            className="p-1 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors"
            title="关闭并返回"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <iframe
          src={getExternalLink(activeClient.link)}
          className="flex-1 w-full border-0 bg-background"
          allow="clipboard-read; clipboard-write"
        />
      </div>
    );
  }

  return (
    <div
      className="relative flex h-full animate-in fade-in duration-300 font-sans rounded-xl overflow-hidden border border-border bg-background shadow-sm"
      onDragEnter={handleComposerDragEnter}
      onDragOver={handleComposerDragOver}
      onDragLeave={handleComposerDragLeave}
      onDrop={handleComposerDrop}
    >
      {isDraggingFiles ? (
        <div className="pointer-events-none absolute inset-0 z-30 flex items-center justify-center bg-background/70 backdrop-blur-sm">
          <div className="mx-6 flex max-w-md flex-col items-center gap-2.5 rounded-2xl border border-primary/20 bg-background/95 px-6 py-6 text-center shadow-xl">
            <div className="flex h-12 w-12 items-center justify-center rounded-xl bg-primary/10 text-primary">
              <Paperclip className="h-5 w-5" />
            </div>
            <div className="text-base font-semibold text-foreground">拖拽上传附件</div>
            <div className="text-xs text-muted-foreground leading-relaxed">支持图片、PDF、TXT、Markdown、JSON、CSV、XML、YAML</div>
          </div>
        </div>
      ) : null}

      {/* Left: Chat Area */}
      <div className="flex-1 flex flex-col min-w-0">

        {/* Chat Header */}
        <div className="h-11 border-b border-border flex items-center px-3 md:px-5 justify-between bg-background/80 backdrop-blur-sm z-10 flex-shrink-0">
          <div className="flex items-center gap-2 text-foreground">
            <Sparkles className="w-4 h-4 text-primary" />
            <span className="font-semibold text-sm">Playground</span>
            {isGenerating && (
              <span className="text-[11px] text-primary flex items-center gap-1 ml-1">
                <Loader2 className="w-3 h-3 animate-spin" />
                <span className="hidden sm:inline">生成中</span>
              </span>
            )}
          </div>
          <div className="flex items-center gap-1">
            {token ? (
              <span className="hidden sm:flex items-center gap-1 text-[10px] text-emerald-600 dark:text-emerald-500 px-1.5 py-0.5 rounded bg-emerald-500/8">
                <CheckCircle2 className="w-2.5 h-2.5" />
                <span className="font-mono">已连接</span>
              </span>
            ) : (
              <span className="hidden sm:flex items-center gap-1 text-[10px] text-red-500 px-1.5 py-0.5 rounded bg-red-500/8">
                <AlertCircle className="w-2.5 h-2.5" /> 未登录
              </span>
            )}
            <button onClick={() => setShowMobileSettings(true)} className="md:hidden p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors">
              <SlidersHorizontal className="w-3.5 h-3.5" />
            </button>
            <button onClick={clearChat} className="p-1.5 text-muted-foreground hover:text-red-500 rounded-md hover:bg-muted transition-colors" title="清空对话">
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>

        {/* Message List */}
        <div className="flex-1 overflow-y-auto px-3 md:px-8 py-5 space-y-4">
          {messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-full text-muted-foreground">
              <Sparkles className="w-10 h-10 mb-3 opacity-30" />
              <h2 className="text-lg font-semibold text-foreground/70 mb-1">AI Playground</h2>
              <p className="text-xs text-muted-foreground/70">选择模型，开始对话</p>
            </div>
          ) : (
            messages.map((msg, idx) => (
              <div
                key={idx}
                className={`flex flex-col max-w-3xl mx-auto w-full group ${
                  msg.role === 'user'
                    ? 'items-end'
                    : msg.role === 'system'
                      ? 'items-center'
                      : 'items-start'
                }`}
              >
                {/* Role label */}
                <div className="flex items-center gap-1.5 mb-1 text-[11px] font-medium text-muted-foreground/70">
                  {msg.role === 'user' ? (
                    <span className="flex items-center gap-1 text-foreground/50"><MessageSquare className="w-3 h-3" /> You</span>
                  ) : msg.role === 'system' ? (
                    <span className="flex items-center gap-1 text-red-500/70"><AlertCircle className="w-3 h-3" /> System</span>
                  ) : (
                    <span className="flex items-center gap-1 text-primary/70"><Brain className="w-3 h-3" /> {selectedModel || 'Assistant'}</span>
                  )}
                </div>

                {/* Message bubble */}
                <div className={`w-fit max-w-[90%] px-3.5 py-2.5 rounded-2xl transition-colors ${
                  msg.role === 'user'
                    ? 'bg-primary text-primary-foreground rounded-tr-[4px]'
                    : msg.role === 'system'
                      ? 'bg-red-500/8 border border-red-500/15 text-foreground rounded-tl-[4px]'
                      : 'bg-muted/50 border border-border/60 text-foreground rounded-tl-[4px] shadow-sm'
                }`}>

                  {/* Reasoning / Thinking */}
                  {msg.reasoning_content && (
                    <div className="mb-2.5 rounded-lg overflow-hidden border border-border/50">
                      <button
                        onClick={() => toggleThinking(idx)}
                        className="w-full flex items-center gap-1.5 px-2.5 py-1.5 text-[11px] text-muted-foreground bg-muted/60 hover:bg-muted transition-colors"
                      >
                        <Zap className="w-3 h-3 text-amber-500" />
                        <span>Reasoning</span>
                        <ChevronDown className={`w-3 h-3 ml-auto transition-transform ${showThinking[idx] ? 'rotate-180' : ''}`} />
                      </button>
                      {showThinking[idx] && (
                        <div className="px-3 py-2 text-[12px] text-muted-foreground/80 font-mono whitespace-pre-wrap border-t border-border/50 bg-muted/20 leading-relaxed">
                          {msg.reasoning_content}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Editing mode */}
                  {editingIndex === idx ? (
                    <div className="space-y-2">
                      <textarea
                        value={editValue}
                        onChange={e => setEditValue(e.target.value)}
                        className="w-full bg-background border border-primary/40 text-foreground p-2.5 rounded-lg text-[13px] focus:outline-none min-h-[80px]"
                        autoFocus
                      />
                      <div className="flex justify-end gap-1.5">
                        <button onClick={() => setEditingIndex(null)} className="px-2.5 py-1 text-[11px] bg-muted text-foreground rounded-md hover:bg-muted/80">取消</button>
                        <button onClick={() => saveEdit(idx)} className="px-2.5 py-1 text-[11px] bg-primary text-primary-foreground rounded-md hover:bg-primary/90">保存{msg.role === 'user' ? '并重发' : ''}</button>
                      </div>
                      {msg.attachments?.length ? (
                        <div className="text-[11px] text-muted-foreground">
                          当前消息包含 {msg.attachments.length} 个附件
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <div className="space-y-2">
                      {msg.content ? (markdownRendering && msg.role !== 'system' ? (
                        <div className="text-[14.5px] leading-relaxed">
                          <MarkdownRenderer content={msg.content} tone={msg.role === 'user' ? 'inverse' : 'default'} className={msg.role === 'user' ? 'my-0' : 'my-1'} />
                          {msg.isTyping ? <span className="inline-block w-1.5 h-3.5 bg-current opacity-60 animate-pulse align-middle ml-0.5" /> : null}
                        </div>
                      ) : (
                        <div className={`text-[14.5px] leading-relaxed whitespace-pre-wrap break-words ${msg.role === 'user' ? 'my-0' : 'my-1'}`}>
                          {msg.content}
                          {msg.isTyping ? <span className="inline-block w-1.5 h-3.5 bg-current opacity-60 ml-0.5 animate-pulse align-middle" /> : null}
                        </div>
                      )) : msg.isTyping ? (
                        <div className="text-[13.5px]">
                          <span className="inline-block w-1.5 h-3.5 bg-current opacity-60 animate-pulse align-middle" />
                        </div>
                      ) : null}

                      {/* Inline attachments */}
                      {msg.attachments?.length ? (
                        <div className="flex flex-wrap gap-1.5 pt-1">
                          {msg.attachments.map(attachment => (
                            <button
                              key={attachment.id}
                              type="button"
                              onClick={() => openAttachmentPreview(attachment)}
                              className="flex items-center gap-1.5 max-w-[200px] text-left rounded-lg border border-border/60 bg-background/50 px-2 py-1 text-[11px] hover:border-primary/30 hover:bg-background/80 transition-colors"
                              title="预览附件"
                            >
                              {attachment.kind === 'image' ? (
                                <img src={attachment.dataUrl} alt={attachment.name} className="w-6 h-6 rounded object-cover bg-muted flex-shrink-0" />
                              ) : (
                                <span className="text-muted-foreground flex-shrink-0">{getAttachmentIcon(attachment)}</span>
                              )}
                              <span className="truncate text-foreground/70">{attachment.name}</span>
                              <Eye className="w-2.5 h-2.5 text-muted-foreground/50 flex-shrink-0" />
                            </button>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  )}
                </div>

                {/* Action buttons */}
                {editingIndex !== idx && !msg.isTyping && (
                  <div className="flex items-center gap-0.5 mt-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
                    <button onClick={() => copyMessage(idx, msg.content)} className="p-1 text-muted-foreground/50 hover:text-foreground rounded transition-colors" title="复制">
                      {copiedIndex === idx ? <CheckCheck className="w-3 h-3 text-emerald-500" /> : <Copy className="w-3 h-3" />}
                    </button>
                    <button onClick={() => startEditing(idx, msg.content)} className="p-1 text-muted-foreground/50 hover:text-foreground rounded transition-colors" title="编辑">
                      <Edit3 className="w-3 h-3" />
                    </button>
                    <button onClick={() => retryMessage(idx)} className="p-1 text-muted-foreground/50 hover:text-primary rounded transition-colors" title={msg.role === 'user' ? '重发' : '重新生成'}>
                      <RefreshCw className="w-3 h-3" />
                    </button>
                    <DropdownMenu.Root>
                      <DropdownMenu.Trigger asChild>
                        <button className="p-1 text-muted-foreground/50 hover:text-foreground rounded transition-colors">
                          <MoreVertical className="w-3 h-3" />
                        </button>
                      </DropdownMenu.Trigger>
                      <DropdownMenu.Portal>
                        <DropdownMenu.Content className="min-w-[100px] bg-card border border-border rounded-lg shadow-xl p-0.5 z-50 text-[12px]">
                          <DropdownMenu.Item onClick={() => deleteMessage(idx)} className="flex items-center gap-1.5 px-2.5 py-1.5 text-red-500 hover:bg-red-500/8 rounded cursor-pointer outline-none">
                            <Trash2 className="w-3 h-3" /> 删除以下
                          </DropdownMenu.Item>
                        </DropdownMenu.Content>
                      </DropdownMenu.Portal>
                    </DropdownMenu.Root>
                  </div>
                )}
              </div>
            ))
          )}
          <div ref={messagesEndRef} className="h-2" />
        </div>

        {/* Input Area */}
        <div className="px-3 md:px-8 pb-3 pt-2 bg-background/80 backdrop-blur-sm border-t border-border/50 flex-shrink-0">
          <div className="max-w-3xl mx-auto">
            <div className="bg-muted/40 border border-border/60 focus-within:border-primary/40 rounded-xl overflow-hidden transition-colors">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept="image/*,.pdf,.txt,.md,.markdown,.json,.csv,.xml,.yaml,.yml"
                className="hidden"
                onChange={handleAttachmentSelect}
              />

              {/* Pending attachments - compact inline pills */}
              {pendingAttachments.length ? (
                <div className="px-3 pt-2.5 flex flex-wrap gap-1.5">
                  {pendingAttachments.map(attachment => (
                    <div key={attachment.id} className="flex items-center gap-1.5 bg-background/70 border border-border/50 rounded-lg pl-2 pr-1 py-1 text-[11px]">
                      {attachment.kind === 'image' ? (
                        <img src={attachment.dataUrl} alt={attachment.name} className="w-5 h-5 rounded object-cover bg-muted" />
                      ) : (
                        <span className="text-muted-foreground">{getAttachmentIcon(attachment)}</span>
                      )}
                      <span className="truncate max-w-[100px] text-foreground/70">{attachment.name}</span>
                      <button
                        type="button"
                        onClick={() => openAttachmentPreview(attachment)}
                        className="p-0.5 text-muted-foreground/50 hover:text-foreground rounded transition-colors"
                      >
                        <Eye className="w-3 h-3" />
                      </button>
                      <button
                        type="button"
                        onClick={() => removePendingAttachment(attachment.id)}
                        className="p-0.5 text-muted-foreground/50 hover:text-red-500 rounded transition-colors"
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </div>
                  ))}
                </div>
              ) : null}

              {attachmentError ? (
                <div className="px-3 pt-2 text-[11px] text-red-500">{attachmentError}</div>
              ) : null}

              <div className="relative">
                <textarea
                  ref={textareaRef}
                  value={inputValue}
                  onChange={e => {
                    setInputValue(e.target.value);
                    e.target.style.height = 'auto';
                    e.target.style.height = `${Math.min(e.target.scrollHeight, 160)}px`;
                  }}
                  onKeyDown={handleKeyDown}
                  placeholder={token ? "输入消息... (Shift+Enter 换行)" : "请先登录..."}
                  disabled={!token || isGenerating}
                  className="w-full bg-transparent text-foreground py-2.5 pl-10 pr-10 text-[13px] max-h-[160px] resize-none focus:outline-none placeholder:text-muted-foreground/50 disabled:opacity-40"
                  rows={1}
                />

                <button
                  onClick={openFilePicker}
                  disabled={!token || isGenerating || pendingAttachments.length >= MAX_PLAYGROUND_ATTACHMENT_COUNT}
                  className="absolute left-2 bottom-2 p-1.5 text-muted-foreground/50 hover:text-foreground rounded-md disabled:opacity-30 transition-colors"
                  title="上传附件"
                >
                  <Paperclip className="w-3.5 h-3.5" />
                </button>

                <button
                  onClick={handleSend}
                  disabled={(!inputValue.trim() && pendingAttachments.length === 0) || isGenerating || !token}
                  className="absolute right-2 bottom-2 p-1.5 bg-primary hover:bg-primary/90 text-primary-foreground rounded-lg disabled:opacity-30 disabled:hover:bg-primary transition-all"
                >
                  {isGenerating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                </button>
              </div>
            </div>
            <div className="flex justify-between mt-1 px-0.5 text-[10px] text-muted-foreground/40">
              <span>支持附件拖拽上传</span>
              <span>{stream ? '流式' : '非流式'}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Attachment Preview Dialog */}
      <Dialog.Root open={Boolean(previewAttachment)} onOpenChange={open => { if (!open) setPreviewAttachment(null); }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40" />
          <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[min(92vw,900px)] max-h-[85vh] -translate-x-1/2 -translate-y-1/2 overflow-hidden rounded-xl border border-border bg-background shadow-2xl">
            {previewAttachment ? (
              <>
                <div className="flex items-start justify-between gap-4 border-b border-border px-4 py-3">
                  <div className="min-w-0">
                    <Dialog.Title className="text-sm font-semibold text-foreground truncate">{previewAttachment.name}</Dialog.Title>
                    <div className="text-[11px] text-muted-foreground mt-0.5">
                      {previewAttachment.mimeType} · {formatAttachmentSize(previewAttachment.size)}
                    </div>
                  </div>
                  <Dialog.Close className="rounded-md p-1 text-muted-foreground hover:text-foreground hover:bg-muted transition-colors">
                    <X className="w-4 h-4" />
                  </Dialog.Close>
                </div>

                <div className="max-h-[calc(85vh-60px)] overflow-auto p-4 bg-muted/10">
                  {previewAttachment.kind === 'image' ? (
                    <img src={previewAttachment.dataUrl} alt={previewAttachment.name} className="max-w-full max-h-[70vh] mx-auto rounded-lg border border-border bg-background object-contain" />
                  ) : previewAttachment.mimeType === 'application/pdf' ? (
                    <iframe
                      src={previewAttachment.dataUrl}
                      title={previewAttachment.name}
                      className="w-full h-[70vh] rounded-lg border border-border bg-background"
                    />
                  ) : (
                    <pre className="whitespace-pre-wrap break-words rounded-lg border border-border bg-background p-3 text-[12px] text-foreground font-mono leading-relaxed">
                      {previewAttachment.previewText || decodeAttachmentText(previewAttachment.dataUrl)}
                    </pre>
                  )}
                </div>
              </>
            ) : null}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

      {/* Right: Parameters Panel (Desktop) */}
      <div className="w-64 bg-muted/10 border-l border-border flex-shrink-0 flex-col hidden md:flex h-full">
        <div className="h-11 border-b border-border flex items-center px-3 gap-1.5 flex-shrink-0 text-[13px] font-medium text-foreground/80">
          <Settings2 className="w-3.5 h-3.5 text-primary" />
          参数
        </div>
        <div className="flex-1 overflow-y-auto p-3">
          <SettingsPanel />
        </div>
      </div>

      {/* Mobile Settings Dialog */}
      <Dialog.Root open={showMobileSettings} onOpenChange={setShowMobileSettings}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 bg-black/60 z-40" />
          <Dialog.Content className="fixed bottom-0 left-0 right-0 bg-background border-t border-border rounded-t-2xl z-50 max-h-[75vh] overflow-y-auto animate-in slide-in-from-bottom duration-300">
            <div className="p-3 border-b border-border flex items-center justify-between">
              <Dialog.Title className="text-sm font-semibold text-foreground flex items-center gap-2">
                <Settings2 className="w-4 h-4 text-primary" /> 参数配置
              </Dialog.Title>
              <Dialog.Close className="p-1 text-muted-foreground hover:text-foreground">
                <X className="w-4 h-4" />
              </Dialog.Close>
            </div>
            <div className="p-4">
              {/* Auth Status */}
              <div className={`p-2.5 rounded-lg flex items-center gap-2 mb-4 text-[13px] ${token ? 'bg-emerald-500/8 border border-emerald-500/15' : 'bg-red-500/8 border border-red-500/15'}`}>
                {token ? (
                  <><CheckCircle2 className="w-3.5 h-3.5 text-emerald-500" /><span className="text-emerald-600 dark:text-emerald-400">已登录</span></>
                ) : (
                  <><AlertCircle className="w-3.5 h-3.5 text-red-500" /><span className="text-red-600 dark:text-red-400">未登录</span></>
                )}
              </div>
              <SettingsPanel isMobile />
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>

    </div>
  );
}

