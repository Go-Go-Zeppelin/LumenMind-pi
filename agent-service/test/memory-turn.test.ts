import { afterEach, describe, expect, it, vi } from "vitest";
import { withMemoryTurn } from "../src/agents/memory-turn.js";
import { InfrastructureClient, InfrastructureError } from "../src/clients/infrastructure.js";
import type { ChatResponse } from "../src/types.js";

const response = () => ({ response: "answer", conv_id: "c", request_id: "r" } as ChatResponse);
function harness() {
  return {
    beginTurn: vi.fn(async (_u, c, r) => ({ status: "RECEIVED", request_id: r, conv_id: c || "server-conv", lease_token: "token" })),
    renewTurn: vi.fn().mockResolvedValue({}), finishTurn: vi.fn().mockResolvedValue({ status: "COMPLETED" }),
    commitTurn: vi.fn(),
  };
}
const req = { user_id: "u", message: "hello", idempotency_key: "stable-key" };
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("durable turn lifecycle", () => {
  it("uses canonical IDs, optional conversation and only one final write", async () => {
    const api = harness();
    const execute = vi.fn(async turn => ({ ...response(), conv_id: turn.convId, request_id: turn.requestId }));
    const result = await withMemoryTurn(api as never, req, 20_000, undefined, execute);
    expect(api.beginTurn).toHaveBeenCalledWith("u", undefined, expect.any(String), "hello", "stable-key");
    expect(result.conv_id).toBe("server-conv");
    expect(result.memory_status).toBe("stored");
    expect(api.finishTurn).toHaveBeenCalledWith("u", "server-conv", result.request_id, "token", "COMPLETED", "answer");
    expect(api.commitTurn).not.toHaveBeenCalled();
  });
  it("degrades on failed begin without an ambiguous fallback write", async () => {
    const api = harness(); api.beginTurn.mockRejectedValue(new Error("timeout"));
    const execute = vi.fn(async t => { expect(t.available).toBe(false); return response(); });
    expect((await withMemoryTurn(api as never, req, 20_000, undefined, execute)).memory_status).toBe("unavailable");
    expect(api.finishTurn).not.toHaveBeenCalled(); expect(api.commitTurn).not.toHaveBeenCalled();
  });
  it("never executes models on 409 conflicts", async () => {
    const api = harness(); api.beginTurn.mockRejectedValue(new InfrastructureError("busy", 409));
    const execute = vi.fn();
    await expect(withMemoryTurn(api as never, req, 20_000, undefined, execute)).rejects.toMatchObject({ status: 409 });
    expect(execute).not.toHaveBeenCalled();
  });
  it("replays stored response without model/tool execution", async () => {
    const api = harness(); api.beginTurn.mockResolvedValue({ status: "COMPLETED", conv_id: "original-c", request_id: "original-r", lease_token: "token", response: "original" } as never);
    const execute = vi.fn();
    expect(await withMemoryTurn(api as never, req, 20_000, undefined, execute)).toMatchObject({ response: "original", request_id: "original-r", conv_id: "original-c", replayed: true });
    expect(execute).not.toHaveBeenCalled(); expect(api.finishTurn).not.toHaveBeenCalled();
  });
  it("rejects duplicate in-flight request", async () => {
    const api = harness(); api.beginTurn.mockResolvedValue({ status: "RECEIVED", conv_id: "c", request_id: "other-r", lease_token: "token" });
    await expect(withMemoryTurn(api as never, req, 20_000, undefined, vi.fn())).rejects.toMatchObject({ status: 409 });
  });
  it("marks write failures as failed while retaining answer", async () => {
    const api = harness(); api.finishTurn.mockRejectedValue(new Error("disk"));
    expect(await withMemoryTurn(api as never, req, 20_000, undefined, async () => response())).toMatchObject({ memory_status: "failed", response: "answer" });
  });
  it("records FAILED without an invented assistant", async () => {
    const api = harness();
    await expect(withMemoryTurn(api as never, req, 20_000, undefined, async () => { throw new Error("model"); })).rejects.toThrow("model");
    expect(api.finishTurn).toHaveBeenCalledWith("u", "server-conv", expect.any(String), "token", "FAILED");
  });
  it("records client cancellation and releases timer", async () => {
    const api = harness(); const ctl = new AbortController();
    await expect(withMemoryTurn(api as never, req, 20_000, ctl.signal, async t => { ctl.abort(); t.signal.throwIfAborted(); return response(); })).rejects.toThrow();
    expect(api.finishTurn).toHaveBeenCalledWith("u", "server-conv", expect.any(String), "token", "CANCELLED");
  });
  it("renews and aborts on lost lease without a late finish", async () => {
    vi.useFakeTimers(); const api = harness(); api.renewTurn.mockRejectedValue(new Error("expired"));
    const task = withMemoryTurn(api as never, req, 20_000, undefined, async t => {
      await new Promise<void>(resolve => t.signal.addEventListener("abort", () => resolve(), { once: true }));
      return response();
    });
    const checked = expect(task).rejects.toMatchObject({ status: 409 });
    await vi.advanceTimersByTimeAsync(20_001); await checked;
    expect(api.renewTurn).toHaveBeenCalledOnce(); expect(api.finishTurn).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
  it("does not report legacy failure as stored", async () => {
    const api = { commitTurn: vi.fn().mockRejectedValue(new Error("down")) };
    expect((await withMemoryTurn(api as never, req, 20_000, undefined, async () => response())).memory_status).toBe("failed");
  });
});

it("HTTP begin retry reuses exact serialized body and key", async () => {
  const mock = vi.fn().mockRejectedValueOnce(new Error("response lost")).mockResolvedValue(new Response(JSON.stringify({ status: "RECEIVED" })));
  vi.stubGlobal("fetch", mock);
  await new InfrastructureClient("http://infra", 1000).beginTurn("u", undefined, "r", "hello", "key");
  expect(mock).toHaveBeenCalledTimes(2);
  expect(mock.mock.calls[0]![1].body).toBe(mock.mock.calls[1]![1].body);
  expect(mock.mock.calls[1]![1].headers["Idempotency-Key"]).toBe("key");
});
