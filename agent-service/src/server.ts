import { config } from "./config.js";
import { LumenMindOrchestrator } from "./agents/orchestrator.js";
import { createApp } from "./app.js";
import { InfrastructureClient } from "./clients/infrastructure.js";
import { SkillManager } from "./skills/loader.js";
import { createKnowledgeSearchTool } from "./tools/knowledge-search.js";

const infrastructure = new InfrastructureClient(config.infrastructureUrl, config.infrastructureTimeoutMs);
const skills = new SkillManager(config.skillsDir, config.skillsMaxPromptChars);
await skills.load();
const knowledgeTool = createKnowledgeSearchTool(infrastructure);
const orchestrator = new LumenMindOrchestrator(config, infrastructure, skills, knowledgeTool);
const app = createApp(config, orchestrator, skills, infrastructure);

app.listen(config.port, config.host, () => {
  console.log(`LumenMind Pi Agent 服务已启动: http://${config.host}:${config.port}`);
  console.log(`模型: ${config.model.id}  base_url: ${config.model.baseUrl}`);
  console.log(`Skills: ${skills.summary().count}  MCP: http://${config.host}:${config.port}/mcp`);
});

