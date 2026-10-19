/**
 * 单个 Agent 实例的运行统计器。
 *
 * record() 累计总调用次数、成功次数和耗时；setMonitorPenalty() 保存外部监控
 * 给出的惩罚值。snapshot() 输出 total、success_rate、avg_ms、monitor_penalty
 * 和 routing_score，其中 routing_score 综合成功率、平均延迟和监控惩罚。
 * Orchestrator 会在同类 Agent 实例池中选择 routing_score 最高的实例执行请求。
 */
export class AgentStats {
  private total = 0;
  private success = 0;
  private totalMs = 0;
  private monitorPenalty = 0;

  setMonitorPenalty(value: number): void {
    this.monitorPenalty = Math.max(0, Math.min(0.9, value));
  }

  record(ok: boolean, latencyMs: number): void {
    this.total += 1;
    if (ok) this.success += 1;
    this.totalMs += latencyMs;
  }

  snapshot() {
    const successRate = this.total === 0 ? 1 : this.success / this.total;
    const avgMs = this.total === 0 ? 0 : this.totalMs / this.total;
    const latencyScore = 1 / (1 + avgMs / 1000);
    return {
      total: this.total,
      success_rate: Number(successRate.toFixed(3)),
      avg_ms: Number(avgMs.toFixed(1)),
      monitor_penalty: Number(this.monitorPenalty.toFixed(3)),
      routing_score: Number(((successRate * 0.7 + latencyScore * 0.3) * (1 - this.monitorPenalty)).toFixed(3)),
    };
  }
}
