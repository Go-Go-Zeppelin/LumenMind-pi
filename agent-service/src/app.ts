import cors from "cors";
import express, { type NextFunction, type Request, type Response } from "express";
import type { AppConfig } from "./config.js";
import type { InfrastructureClient } from "./clients/infrastructure.js";
import type { LumenMindOrchestrator } from "./agents/orchestrator.js";
import { recognizeByRules } from "./agents/intent-router.js";
import { handleMcpHttpRequest } from "./mcp/server.js";
import type { SkillManager } from "./skills/loader.js";

export function createApp(
  appConfig: AppConfig,
  orchestrator: LumenMindOrchestrator,
  skills: SkillManager,
  infrastructure: InfrastructureClient,
) {
  const app = express();
  app.disable("x-powered-by");
  app.use(cors());
  app.use(express.json({ limit: "1mb" }));

  const readMessage = (body: unknown): { message: string } | { error: string } => {
    const message = typeof (body as { message?: unknown })?.message === "string"
      ? (body as { message: string }).message.trim() : "";
    if (!message) return { error: "message 不能为空" };
    if (message.length > 20_000) return { error: "message 超过 20000 字符限制" };
    return { message };
  };

  app.get("/health", async (_req, res) => {
    let pythonInfrastructure: Record<string, unknown> = { status: "unavailable" };
    try {
      pythonInfrastructure = await infrastructure.health();
    } catch (error) {
      pythonInfrastructure = { status: "unavailable", error: error instanceof Error ? error.message : String(error) };
    }
    res.json({
      status: appConfig.apiKey ? "ok" : "degraded",
      runtime: "pi-agent-core",
      sdk_version: "0.82.1",
      model: appConfig.model.id,
      api_key_configured: Boolean(appConfig.apiKey),
      agents: orchestrator.getStats(),
      python_infrastructure: pythonInfrastructure,
    });
  });

  app.get("/skills", (_req, res) => res.json(skills.summary()));
  app.post("/skills/reload", async (_req, res) => res.json(await skills.reload()));

  app.get("/trace/tool/:requestId", (req, res) => {
    const requestId = String(req.params.requestId || "");
    const trace = orchestrator.getToolTrace(requestId);
    res.json({ request_id: requestId, found: Boolean(trace), trace: trace ?? {} });
  });

  app.get("/trace/tools", (req, res) => {
    const rawLimit = Number(req.query.limit ?? 20);
    const traceMax = appConfig.toolTraceMax ?? 200;
    const limit = Number.isFinite(rawLimit)
      ? Math.max(1, Math.min(Math.floor(rawLimit), traceMax))
      : Math.min(20, traceMax);
    res.json({ items: orchestrator.getRecentToolTraces(limit) });
  });

  // 意图评测专用的无副作用入口。默认只跑确定性规则，不访问 Python、Agent 或知识库。
  // 如需测试低置信度的 LLM 路由，可显式传 use_llm=true，但这会产生一次模型调用。
  app.post("/intent", async (req, res) => {
    const message = typeof req.body?.message === "string" ? req.body.message.trim() : "";
    if (!message) {
      res.status(400).json({ detail: "message 不能为空" });
      return;
    }
    if (req.body?.use_llm === true) {
      const history = Array.isArray(req.body?.history) ? req.body.history : [];
      res.json(await orchestrator.recognizeIntent(message, history));
      return;
    }
    res.json(recognizeByRules(message));
  });

  app.post("/chat", async (req, res) => {
    const parsed = readMessage(req.body);
    if ("error" in parsed) {
      res.status(parsed.error.includes("超过") ? 413 : 400).json({ detail: parsed.error });
      return;
    }
    const message = parsed.message;
    const result = await orchestrator.chat({
      message,
      user_id: typeof req.body?.user_id === "string" ? req.body.user_id : "anonymous",
      conv_id: typeof req.body?.conv_id === "string" ? req.body.conv_id : null,
    });
    res.json(result);
  });

  app.post("/chat/stream", async (req, res) => {
    const parsed = readMessage(req.body);
    if ("error" in parsed) {
      res.status(parsed.error.includes("超过") ? 413 : 400).json({ detail: parsed.error });
      return;
    }
    const message = parsed.message;
    res.status(200);
    res.setHeader("content-type", "text/event-stream; charset=utf-8");
    res.setHeader("cache-control", "no-cache, no-transform");
    res.setHeader("connection", "keep-alive");
    res.flushHeaders();
    const send = (event: string, data: unknown) => {
      res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
    };
    try {
      const result = await orchestrator.chat({
        message,
        user_id: typeof req.body?.user_id === "string" ? req.body.user_id : "anonymous",
        conv_id: typeof req.body?.conv_id === "string" ? req.body.conv_id : null,
      }, (event) => send(event.type, event));
      send("done", result);
    } catch (error) {
      send("error", { detail: error instanceof Error ? error.message : "流式对话失败" });
    } finally {
      res.end();
    }
  });

  app.all("/mcp", async (req, res) => {
    await handleMcpHttpRequest(req, res, infrastructure);
  });

  app.use((error: unknown, _req: Request, res: Response, _next: NextFunction) => {
    console.error(error);
    if (!res.headersSent) {
      res.status(500).json({ detail: error instanceof Error ? error.message : "内部服务错误" });
    }
  });
  return app;
}
