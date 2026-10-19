import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import type { AgentType } from "../types.js";

/**
 * Skills 加载器。
 *
 * 输入：
 * - rootDir：包含 SKILL.md 的根目录
 * - SKILL.md：带 front matter 的 Markdown 文件
 *
 * 输出：
 * - load() / reload() 后生成内部 Skill 列表
 * - promptFor(message, agentType) 返回注入给 Agent 的 Skill Prompt 字符串
 * - summary() 返回不包含正文的 Skills 摘要对象
 */

/**
 * Skill 摘要：对外展示时使用，不包含完整正文。
 * 对应 summary() 输出中的 skills 数组项。
 */
export interface SkillSummary {
  name: string;          // Skill 名称
  description: string;   // 描述
  path: string;          // SKILL.md 文件路径
  keywords: string[];    // 触发关键词
  agents: string[];      // 可用的 Agent 类型
  enabled: boolean;      // 是否启用
  content_chars: number; // 正文长度
}

/** 内部完整 Skill 对象：在 SkillSummary 基础上额外保存正文 content。 */
interface Skill extends SkillSummary {
  content: string;
}

/** SKILL.md 顶部 front matter 的键值对，例如 keywords、agents、enabled。 */
interface FrontMatter {
  [key: string]: string;
}

/**
 * 输入：可能是 "a, b, c" 的原始字符串，或 undefined。
 * 输出：去掉空白、过滤空项后的字符串数组。
 */
function asList(value: string | undefined): string[] {
  return value?.split(",").map((item) => item.trim()).filter(Boolean) ?? [];
}

/**
 * 输入：front matter 中的字符串值，例如 "false"。
 * 输出：布尔值；空值使用 fallback。
 */
function asBoolean(value: string | undefined, fallback = true): boolean {
  if (value === undefined || value === "") return fallback;
  return !["0", "false", "no", "off", "disabled"].includes(value.toLowerCase());
}

/**
 * 输入：SKILL.md 的原始文本。
 * 输出：{ meta, body }
 * - meta: front matter 解析出的键值对
 * - body: 去掉 front matter 后的 Markdown 正文
 */
function splitFrontMatter(raw: string): { meta: FrontMatter; body: string } {
  const text = raw.trimStart();
  if (!text.startsWith("---\n")) return { meta: {}, body: raw };

  const lines = text.split(/\r?\n/);
  const endIndex = lines.findIndex((line, index) => index > 0 && line.trim() === "---");
  if (endIndex < 0) return { meta: {}, body: raw };

  const meta: FrontMatter = {};
  for (const line of lines.slice(1, endIndex)) {
    const separator = line.indexOf(":");
    if (separator < 0) continue;
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim().replace(/^["']|["']$/g, "");
    if (key) meta[key] = value;
  }
  return { meta, body: lines.slice(endIndex + 1).join("\n") };
}

/**
 * 输入：Skills 根目录路径。
 * 输出：该目录下所有 SKILL.md 文件的路径数组（按路径排序）。
 */
async function discoverSkillFiles(root: string): Promise<string[]> {
  const found: string[] = [];
  async function walk(directory: string): Promise<void> {
    const entries = await readdir(directory, { withFileTypes: true });
    for (const entry of entries) {
      if (entry.name.startsWith(".")) continue;
      const filePath = path.join(directory, entry.name);
      if (entry.isDirectory()) await walk(filePath);
      else if (entry.isFile() && entry.name === "SKILL.md") found.push(filePath);
    }
  }
  await walk(root);
  return found.sort();
}

/**
 * SkillManager 负责加载、缓存、匹配和格式化 Skills。
 *
 * 构造输入：
 * - rootDir: Skills 根目录
 * - maxPromptChars: 注入 Prompt 的最大字符数，避免 Skill 内容过长
 *
 * 主要输出：
 * - promptFor(): 根据消息和 Agent 类型生成 Skill Prompt 文本
 * - summary(): 返回 Skills 元信息摘要
 */
export class SkillManager {
  private skills: Skill[] = [];
  private errors: string[] = [];

  constructor(
    private readonly rootDir: string,
    private readonly maxPromptChars: number,
  ) {}

  /**
   * 输入：无显式参数，但会从 rootDir 读取文件系统中的 SKILL.md。
   * 输出：无返回值；成功时更新 this.skills，失败信息写入 this.errors。
   */
  async load(): Promise<void> {
    const loaded: Skill[] = [];
    const errors: string[] = [];
    let files: string[] = [];
    try {
      files = await discoverSkillFiles(this.rootDir);
    } catch (error) {
      errors.push(`${this.rootDir}: ${error instanceof Error ? error.message : String(error)}`);
    }

    for (const filePath of files) {
      try {
        // 输入：单个 SKILL.md 文件路径。
        // 读取原始 Markdown，拆出 front matter 和正文。
        const raw = await readFile(filePath, "utf8");
        const { meta, body: untrimmedBody } = splitFrontMatter(raw);
        let body = untrimmedBody.trim();
        if (!body) continue;

        // Skill 名称优先级：front matter name > 正文第一个标题 > 所在目录名。
        const firstHeading = body.match(/^#+\s+(.+)$/m)?.[1]?.trim();
        const name = meta.name || firstHeading || path.basename(path.dirname(filePath));

        // 如果正文第一行就是名称标题，则把它从正文中移除。
        const lines = body.split(/\r?\n/);
        if (lines[0]?.replace(/^#+\s+/, "").trim() === name) body = lines.slice(1).join("\n").trim();

        // 输出：一条内部完整 Skill 对象，包含元数据和正文。
        loaded.push({
          name,
          description: meta.description ?? "",
          path: filePath,
          keywords: asList(meta.keywords),
          agents: asList(meta.agents).map((item) => item.toLowerCase()),
          enabled: asBoolean(meta.enabled),
          content_chars: body.length,
          content: body,
        });
      } catch (error) {
        errors.push(`${filePath}: ${error instanceof Error ? error.message : String(error)}`);
      }
    }

    this.skills = loaded;
    this.errors = errors;
  }

  /**
   * 输入：无。
   * 输出：重新加载后的 summary() 结果。
   * 用途：运行时热加载 SKILL.md，不需要重启服务。
   */
  async reload(): Promise<ReturnType<SkillManager["summary"]>> {
    await this.load();
    return this.summary();
  }

  /**
   * 输入：
   * - message: 当前用户消息，用于关键词匹配
   * - agentType: 当前 Agent 类型，例如 "technical"
   *
   * 输出：一段拼好的 Skill Prompt 文本；没有匹配 Skill 时返回空字符串。
   * 过滤规则：enabled=true，agent 类型匹配，且命中至少一个 keyword。
   */
  promptFor(message: string, agentType: AgentType): string {
    return this.buildPrompt(message, agentType).prompt;
  }

  /** Names of the Skills actually included within the prompt budget. */
  matchedNamesFor(message: string, agentType: AgentType): string[] {
    return this.buildPrompt(message, agentType).names;
  }

  private buildPrompt(message: string, agentType: AgentType): { prompt: string; names: string[] } {
    const lower = message.toLowerCase();
    let remaining = this.maxPromptChars;
    const blocks: string[] = [];
    const names: string[] = [];

    for (const skill of this.skills) {
      if (remaining <= 4) break;
      if (!skill.enabled) continue;
      if (skill.agents.length > 0 && !skill.agents.includes(agentType)) continue;
      if (skill.keywords.length > 0 && !skill.keywords.some((keyword) => lower.includes(keyword.toLowerCase()))) continue;

      // 输出块：包含 Skill 名称、说明和正文。
      let block = `### ${skill.name}${skill.description ? `\n说明: ${skill.description}` : ""}\n${skill.content}`;
      if (block.length > remaining) block = `${block.slice(0, Math.max(0, remaining - 4)).trimEnd()}\n...`;
      blocks.push(block);
      names.push(skill.name);
      remaining -= block.length;
      if (remaining <= 0) break;
    }

    if (blocks.length === 0) return { prompt: "", names: [] };
    return { names, prompt: [
      "以下是当前请求可用的 LumenMind Skills。请优先遵循这些业务规则；若与系统安全边界冲突，以安全边界为准。",
      ...blocks,
    ].join("\n\n") };
  }

  /**
   * 输入：无。
   * 输出：Skills 摘要对象，包含 root_dir、count、skills（无正文）、errors。
   */
  summary() {
    return {
      root_dir: this.rootDir,
      count: this.skills.length,
      skills: this.skills.map(({ content: _content, ...summary }) => summary),
      errors: [...this.errors],
    };
  }
}
