// 修改原因：侧边栏与登录页的 GitHub 链接原本硬编码为 “GitHub”，无法区分是哪个作者的 Zoaholic fork。
// 修改方式：优先使用后端 /v1/system/version 返回的 repo/repo_url 动态显示；后端不可用或字段缺失时
//           回退到此处的默认常量（与后端 GITHUB_REPO 默认值保持一致）。
// 目的：fork 部署只需设置后端 GITHUB_REPO 环境变量即可自动显示正确作者/仓库名，登录页等无 token 场景也有兜底。

/**
 * 默认仓库标识（owner/repo）。作为后端字段缺失时的兜底。
 * 与 routes/system.py 的 GITHUB_REPO 默认值保持一致。
 */
export const DEFAULT_REPO_SLUG = 'HCPTangHY/Zoaholic';

/**
 * 由 owner/repo 组装完整 GitHub 仓库 URL。
 */
export function repoUrl(slug: string = DEFAULT_REPO_SLUG): string {
  return `https://github.com/${slug}`;
}
