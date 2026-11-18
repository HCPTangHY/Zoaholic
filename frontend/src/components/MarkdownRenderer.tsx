import { Children, isValidElement, type ReactNode, useMemo, useState } from 'react';
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import type { Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import { Check, Copy } from 'lucide-react';
import 'katex/dist/katex.min.css';

interface MarkdownRendererProps {
  content: string;
  className?: string;
  tone?: 'default' | 'inverse';
}

type MarkdownTone = NonNullable<MarkdownRendererProps['tone']>;

const TONE_STYLES: Record<MarkdownTone, Record<string, string>> = {
  default: {
    root: 'text-foreground/95',
    heading: 'text-foreground font-semibold tracking-tight',
    link: 'text-primary underline decoration-primary/30 hover:decoration-primary/80 transition-colors',
    inlineCode: 'border border-border bg-muted text-foreground/90 shadow-sm px-1.5 py-0.5 rounded-md mx-0.5 font-mono text-[0.85em]',
    codeShell: 'border border-border bg-[#1e1e1e] text-slate-200 shadow-md my-3 rounded-xl overflow-hidden',
    codeHeader: 'border-b border-white/10 bg-[#2d2d2d] text-slate-400 px-4 py-2 flex items-center justify-between',
    codeButton: 'text-slate-400 hover:text-white hover:bg-white/10 p-1.5 rounded-md transition-all',
    quote: 'border-l-4 border-primary/40 bg-primary/5 text-foreground/80 my-2 pl-3 pr-3 py-1 rounded-r-lg italic',
    hr: 'border-border/60 my-5',
    tableWrap: 'border border-border bg-card shadow-sm my-3 rounded-xl overflow-hidden',
    tableHead: 'bg-muted text-foreground font-semibold',
    tableRow: 'border-t border-border hover:bg-muted/30 transition-colors',
    tableCell: 'text-foreground/90',
    footnoteSection: 'mt-5 pt-3 border-t border-border/50 text-[12.5px] text-muted-foreground/80',
    footnoteRef: 'text-primary text-[0.75em] align-super cursor-pointer hover:underline font-semibold',
    footnoteBackref: 'text-primary text-[0.75em] ml-1 cursor-pointer hover:underline',
    checkbox: 'mr-1.5 accent-primary pointer-events-none align-middle',
    checkboxItem: 'list-none -ml-1'
  },
  inverse: {
    root: 'text-primary-foreground/95',
    heading: 'text-primary-foreground font-semibold tracking-tight',
    link: 'text-primary-foreground underline decoration-primary-foreground/40 hover:decoration-primary-foreground transition-colors',
    inlineCode: 'border border-white/20 bg-black/20 text-primary-foreground shadow-sm px-1.5 py-0.5 rounded-md mx-0.5 font-mono text-[0.85em]',
    codeShell: 'border border-white/10 bg-black/40 text-primary-foreground/90 shadow-md my-3 rounded-xl overflow-hidden',
    codeHeader: 'border-b border-white/10 bg-black/40 text-primary-foreground/70 px-4 py-2 flex items-center justify-between',
    codeButton: 'text-primary-foreground/60 hover:text-primary-foreground hover:bg-white/10 p-1.5 rounded-md transition-all',
    quote: 'border-l-4 border-white/30 bg-white/5 text-primary-foreground/90 my-2 pl-3 pr-3 py-1 rounded-r-lg italic',
    hr: 'border-white/20 my-5',
    tableWrap: 'border border-white/10 bg-black/20 shadow-sm my-3 rounded-xl overflow-hidden',
    tableHead: 'bg-white/10 text-primary-foreground font-semibold',
    tableRow: 'border-t border-white/10 hover:bg-white/5 transition-colors',
    tableCell: 'text-primary-foreground/90',
    footnoteSection: 'mt-5 pt-3 border-t border-white/20 text-[12.5px] text-primary-foreground/60',
    footnoteRef: 'text-primary-foreground text-[0.75em] align-super cursor-pointer hover:underline font-semibold',
    footnoteBackref: 'text-primary-foreground text-[0.75em] ml-1 cursor-pointer hover:underline',
    checkbox: 'mr-1.5 accent-white pointer-events-none align-middle',
    checkboxItem: 'list-none -ml-1'
  }
};

function headingClassName(level: number) {
  if (level === 1) return 'text-xl leading-tight mt-6 mb-3 first:mt-0';
  if (level === 2) return 'text-lg leading-snug mt-5 mb-2.5 border-b border-border/40 pb-1 first:mt-0';
  if (level === 3) return 'text-base leading-snug mt-4 mb-2 first:mt-0';
  return 'text-sm leading-snug mt-3 mb-1.5 opacity-90 first:mt-0';
}

function CodeBlock({ code, language, tone }: { code: string; language?: string; tone: MarkdownTone }) {
  const [copied, setCopied] = useState(false);
  const styles = TONE_STYLES[tone];

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch (error) {
      console.error('Failed to copy code block', error);
    }
  };

  return (
    <div className={styles.codeShell}>
      <div className={styles.codeHeader}>
        <span className="truncate font-mono text-[11px] uppercase tracking-wider opacity-80">{language || 'text'}</span>
        <button
          type="button"
          onClick={handleCopy}
          className={`inline-flex items-center gap-1.5 text-[11px] font-medium ${styles.codeButton}`}
          title="复制代码"
        >
          {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="overflow-x-auto p-4 text-[13px] leading-relaxed font-mono whitespace-pre">
        <code>{code}</code>
      </pre>
    </div>
  );
}

function mergeClassNames(...classNames: Array<string | undefined | false>) {
  return classNames.filter(Boolean).join(' ');
}

function isExternalHref(href?: string) {
  return Boolean(href && /^https?:\/\//i.test(href));
}

function isCodeBlockElement(children: ReactNode) {
  return Children.toArray(children).some(child => isValidElement(child) && child.type === CodeBlock);
}

function languageFromClassName(className?: string) {
  return className?.match(/(?:^|\s)language-([^\s]+)/)?.[1];
}

function codeText(children: ReactNode) {
  return String(children).replace(/\n$/, '');
}

function hasDataAttribute(props: Record<string, unknown>, name: string) {
  return Object.prototype.hasOwnProperty.call(props, name);
}

function createMarkdownComponents(tone: MarkdownTone): Components {
  const styles = TONE_STYLES[tone];
  const listMarkerClass = tone === 'inverse' ? 'marker:text-primary-foreground/50' : 'marker:text-muted-foreground/60';

  // 修改原因：旧实现用正则和 if else 手写解析，无法稳定覆盖锚点、嵌套列表、GFM 和数学公式。
  // 修改方式：把语法解析交给 react-markdown、remark-gfm、remark-math 和 rehype-katex，原始标签统一按纯文本转义显示。
  // ⚠️ 高风险边界：切勿在此启用原始 HTML 解析插件或直接注入富文本节点，否则不可信文本会在管理界面上下文中直接执行。
  return {
    h1({ node, className, ...props }) {
      void node;
      return <h1 className={mergeClassNames(headingClassName(1), styles.heading, className)} {...props} />;
    },
    h2({ node, className, ...props }) {
      void node;
      return <h2 className={mergeClassNames(headingClassName(2), styles.heading, className)} {...props} />;
    },
    h3({ node, className, ...props }) {
      void node;
      return <h3 className={mergeClassNames(headingClassName(3), styles.heading, className)} {...props} />;
    },
    h4({ node, className, ...props }) {
      void node;
      return <h4 className={mergeClassNames(headingClassName(4), styles.heading, className)} {...props} />;
    },
    h5({ node, className, ...props }) {
      void node;
      return <h5 className={mergeClassNames(headingClassName(5), styles.heading, className)} {...props} />;
    },
    h6({ node, className, ...props }) {
      void node;
      return <h6 className={mergeClassNames(headingClassName(6), styles.heading, className)} {...props} />;
    },
    p({ node, className, ...props }) {
      void node;
      return <p className={mergeClassNames('whitespace-pre-wrap break-words text-[14.5px] leading-relaxed my-2.5 first:mt-0 last:mb-0', className)} {...props} />;
    },
    a({ node, className, href, target, rel, ...props }) {
      void node;
      const rest = props as Record<string, unknown>;
      const footnoteRef = hasDataAttribute(rest, 'data-footnote-ref');
      const footnoteBackref = hasDataAttribute(rest, 'data-footnote-backref');
      const externalHref = isExternalHref(href);
      const computedTarget = target ?? (externalHref ? '_blank' : undefined);
      const computedRel = computedTarget === '_blank' ? mergeClassNames(rel, 'noreferrer') : rel;

      return (
        <a
          href={href}
          target={computedTarget}
          rel={computedRel}
          className={mergeClassNames(
            footnoteRef && styles.footnoteRef,
            footnoteBackref && styles.footnoteBackref,
            href && !footnoteRef && !footnoteBackref && `font-medium decoration-1 underline-offset-[3px] break-words ${styles.link}`,
            className
          )}
          {...props}
        />
      );
    },
    code({ node, className, children, ...props }) {
      void node;
      const language = languageFromClassName(className);
      const rawCode = String(children);
      const isBlock = Boolean(language || rawCode.endsWith('\n'));

      if (isBlock) {
        return <CodeBlock code={codeText(children)} language={language} tone={tone} />;
      }

      return <code className={mergeClassNames(styles.inlineCode, className)} {...props}>{children}</code>;
    },
    pre({ node, className, children, ...props }) {
      void node;

      if (isCodeBlockElement(children)) {
        return <>{children}</>;
      }

      return <pre className={mergeClassNames('overflow-x-auto p-4 text-[13px] leading-relaxed font-mono whitespace-pre', className)} {...props}>{children}</pre>;
    },
    blockquote({ node, className, ...props }) {
      void node;
      return <blockquote className={mergeClassNames(styles.quote, className)} {...props} />;
    },
    ul({ node, className, ...props }) {
      void node;
      return <ul className={mergeClassNames('list-disc space-y-1.5 pl-6 my-3 text-[14.5px] leading-relaxed', listMarkerClass, className)} {...props} />;
    },
    ol({ node, className, ...props }) {
      void node;
      return <ol className={mergeClassNames('list-decimal space-y-1.5 pl-6 my-3 text-[14.5px] leading-relaxed', listMarkerClass, className)} {...props} />;
    },
    li({ node, className, ...props }) {
      void node;
      const isTaskListItem = className?.includes('task-list-item');
      return (
        <li
          className={mergeClassNames(
            'break-words pl-1',
            isTaskListItem && styles.checkboxItem,
            isTaskListItem && '[&:has(input:checked)]:line-through [&:has(input:checked)]:opacity-60',
            className
          )}
          {...props}
        />
      );
    },
    input({ node, className, type, ...props }) {
      void node;
      return <input type={type} className={type === 'checkbox' ? mergeClassNames(styles.checkbox, className) : className} readOnly {...props} />;
    },
    hr({ node, className, ...props }) {
      void node;
      return <hr className={mergeClassNames(styles.hr, className)} {...props} />;
    },
    img({ node, className, alt, ...props }) {
      void node;
      return <img alt={alt || 'image'} className={mergeClassNames('max-w-full rounded-lg my-1 max-h-[512px] object-contain', className)} loading="lazy" {...props} />;
    },
    table({ node, className, ...props }) {
      void node;
      return (
        <div className={mergeClassNames('overflow-x-auto', styles.tableWrap)}>
          <table className={mergeClassNames('w-full border-collapse text-left text-[13px]', className)} {...props} />
        </div>
      );
    },
    thead({ node, className, ...props }) {
      void node;
      return <thead className={mergeClassNames(styles.tableHead, className)} {...props} />;
    },
    tr({ node, className, ...props }) {
      void node;
      return <tr className={mergeClassNames(styles.tableRow, className)} {...props} />;
    },
    th({ node, className, ...props }) {
      void node;
      return <th className={mergeClassNames('px-3.5 py-2.5 font-semibold whitespace-nowrap text-[12.5px] uppercase tracking-wider opacity-90', className)} {...props} />;
    },
    td({ node, className, ...props }) {
      void node;
      return <td className={mergeClassNames('px-3.5 py-2 align-top whitespace-pre-wrap', styles.tableCell, className)} {...props} />;
    },
    strong({ node, className, ...props }) {
      void node;
      return <strong className={mergeClassNames('font-semibold', className)} {...props} />;
    },
    em({ node, className, ...props }) {
      void node;
      return <em className={mergeClassNames('italic', className)} {...props} />;
    },
    del({ node, className, ...props }) {
      void node;
      return <del className={mergeClassNames('opacity-70', className)} {...props} />;
    },
    sup({ node, className, ...props }) {
      void node;
      return <sup className={mergeClassNames('align-super', className)} {...props} />;
    },
    section({ node, className, ...props }) {
      void node;
      const rest = props as Record<string, unknown>;
      const isFootnoteSection = hasDataAttribute(rest, 'data-footnotes');
      return <section className={mergeClassNames(isFootnoteSection && styles.footnoteSection, className)} {...props} />;
    }
  };
}

export function MarkdownRenderer({ content, className = '', tone = 'default' }: MarkdownRendererProps) {
  const trimmed = content.trim();
  const components = useMemo(() => createMarkdownComponents(tone), [tone]);

  if (!trimmed) return null;

  return (
    <div className={`markdown-body break-words text-left text-[14.5px] leading-relaxed ${TONE_STYLES[tone].root} ${tone === 'inverse' ? '[&_.katex]:text-primary-foreground/90' : ''} [&_.katex-display]:my-3 [&_.katex-display]:overflow-x-auto ${className}`.trim()}>
      {/* ⚠️ 高风险边界：模型输出与工作区文档属于不可信内容，此处仅允许挂载标准 Markdown 与公式插件，
          严禁启用原始 HTML 渲染能力，所有内嵌标签必须保持纯文本转义状态。 */}
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        components={components}
        urlTransform={defaultUrlTransform}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
