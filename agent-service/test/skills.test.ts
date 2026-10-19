import { describe, expect, it } from "vitest";
import { SkillManager } from "../src/skills/loader.js";

const skillsDir = new URL("../../skills", import.meta.url).pathname;

describe("Skills 迁移", () => {
  it("加载三类业务 Skills，并按 Agent 和关键词注入", async () => {
    const manager = new SkillManager(skillsDir, 5000);
    await manager.load();

    expect(manager.summary().count).toBe(3);
    expect(manager.promptFor("为什么重复扣款，怎么退款？", "billing")).toContain("账单退款处理规范");
    expect(manager.promptFor("为什么重复扣款，怎么退款？", "technical")).toBe("");
    expect(manager.promptFor("接口一直报 500", "technical")).toContain("技术支持处理规范");
    expect(manager.matchedNamesFor("为什么重复扣款，怎么退款？", "billing")).toEqual(["账单退款处理规范"]);
    expect(manager.matchedNamesFor("为什么重复扣款，怎么退款？", "technical")).toEqual([]);
  });

  it("trace 名称只包含提示词预算内实际注入的 Skill", async () => {
    const manager = new SkillManager(skillsDir, 80);
    await manager.load();
    expect(manager.matchedNamesFor("退款", "billing")).toEqual(["账单退款处理规范"]);
    expect(manager.promptFor("退款", "billing")).toContain("...");
    const disabledBudget = new SkillManager(skillsDir, 0);
    await disabledBudget.load();
    expect(disabledBudget.matchedNamesFor("退款", "billing")).toEqual([]);
    expect(disabledBudget.promptFor("退款", "billing")).toBe("");
  });
});
