# 旧记忆离线迁移与隔离任务维护

当前工具只读取操作员提供的离线 JSON，不连接 Redis、Chroma 或模型，不读取 `.env`。默认只生成计划；只有 `--apply` 才写入 SQLite，并且必须先完成 SQLite online backup。先使用副本演练，再安排停写窗口导入正式库。

## 1. 导出格式

由操作员在停写后导出旧 Redis 消息和 Chroma 画像，合并为以下格式。原始导出文件及备份包含用户数据，应保存在受控目录，不提交 Git。

```json
{
  "schema_version": 1,
  "scopes": [
    {
      "user_id": "example-user",
      "conversations": [
        {
          "conv_id": "old-conversation",
          "messages": [
            {"role": "user", "content": "以后邮件联系我", "timestamp": "2026-01-01T00:00:00+00:00"},
            {"role": "assistant", "content": "好的", "timestamp": "2026-01-01T00:00:01+00:00"},
            {"role": "user", "content": "最后这条还没有回复", "timestamp": "2026-01-01T00:01:00+00:00"}
          ],
          "summary": "旧会话摘要，只作为迁移审计保存"
        }
      ],
      "profile": {
        "preferences": ["邮件联系"],
        "entities": {"产品": ["键盘"]}
      }
    }
  ]
}
```

命名空间来自命令行 `--namespace`，不接受导出正文覆盖。一个用户和一个会话在同一导出中只能出现一次。每条消息必须有 ISO 时间；兼容旧字段 `ts`。有时区的时间统一换算为 UTC。旧无时区时间按 UTC 做确定性排序，不推断机器当时的时区；混用时区的导出应先由操作员归一化。

消息按时间、原列表位置排序；跨会话按时间、会话位置、消息位置分配稳定 `source_seq`。只有真实 `user` 紧接 `assistant` 的配对成为 `COMPLETED`；没有回复的真实用户消息成为 `INTERRUPTED`。孤立 assistant、system、tool 等消息计入跳过数量，绝不伪造对应用户。时间相同的消息须在导出列表中保留已知的原始顺序。

用户级 `profile` 优先，即使为空也不回退。没有用户级画像时，可提供：

```json
"profiles": [
  {
    "conv_id": "old-conversation",
    "timestamp": "2026-01-01T00:00:00+00:00",
    "profile": {"preferences": ["邮件联系"], "entities": {}}
  }
]
```

此时只选择最新会话画像，沿用旧画像解析、去重、数量与字段长度限制。候选画像在持久化前应用与新事实相同的敏感值过滤，命中的密码、令牌等条目被丢弃，只报告 `skipped_sensitive_candidates` 数量，不打印或写入审计正文。数值转字符串等旧归一化兼容行为仍保留。旧情景记录不在此格式中，也不导入新检索集合。

## 2. 先预演

在 `python-service` 目录运行，所有路径都显式指定：

```bash
python -m memory.migrate \
  --snapshot /absolute/offline/legacy-memory.json \
  --database /absolute/staging/memory.sqlite3 \
  --namespace lumenmind
```

默认 `dry-run` 不打开或创建目标数据库，只输出 scope、来源指纹、轮次数量、候选事实数量、旧摘要数量和跳过数量，不打印消息正文。先检查数量和配对结果。

## 3. 应用到副本，再应用正式库

```bash
python -m memory.migrate \
  --snapshot /absolute/offline/legacy-memory.json \
  --database /absolute/staging/memory.sqlite3 \
  --namespace lumenmind \
  --backup-dir /absolute/backups/memory \
  --apply
```

工具先执行 SQLite online backup，包含已提交的 WAL 内容；备份失败则不初始化或导入目标库。首次创建目标时，先备份空库，再初始化 authority schema。备份路径由命令输出，备份文件默认仅当前操作用户可读写。

每个 scope 独立执行以下步骤：

1. 核对删除屏障、现有流量和导入指纹。
2. 提交 `migration_state=IMPORTING` 维护锁及不含正文的导入审计任务。新 begin 会被拒绝。
3. 在单个事务导入该 scope 的消息、候选画像和旧摘要，更新序号及游标，最后改为 `LEGACY_IMPORTED`。

导入消息 `memory_eligible=0`，抽取消费游标移动到最后一条导入来源。新流量从下一序号开始，旧消息不自动回填长期事实。所有画像事实均为 `origin=LEGACY_IMPORT,status=CANDIDATE,provenance_state=LEGACY_UNVERIFIED`，没有伪造来源证据，也不会成为 ACTIVE。只有后续真实用户重申并通过新 Judge 才能建立可信事实。

旧摘要写入 `memory_summary` 审计，标记 `prompt_version=LEGACY_IMPORT`；session 当前摘要仍为空、代际为 0，覆盖边界为 0，不直接进入正常模型上下文。可恢复原文仍可以按真实 user/assistant 角色供当前会话读取；它们不因导入被假设为工具执行证据。

## 4. 幂等与失败恢复

相同 scope、相同来源指纹重复执行只返回审计回执，不增加行、不改写新流量。输入改变后不会沿用旧指纹。已有新轮次、不同导入、已删除会话、`ERASED`、遗忘边界或删除代际都阻止旧数据重新导入。

一个 scope 的数据事务失败时，所有本次消息、事实、摘要和会话写入回滚，维护锁保持 `IMPORTING`。修复原因后重跑**完全相同的导出文件**继续；不要手动解除锁后向同一 scope 发送新聊天。多个 scope 分别原子提交，整个导出文件不保证一次性全部成功；重跑会跳过已成功 scope 的同指纹导入。

先在副本上验证 `PRAGMA integrity_check`、消息/事实/摘要数量、跨 scope 隔离、候选事实不可见、最后序号与重复执行不增行，再切正式库。保留 Redis/Chroma 原始快照及 SQLite 备份，不以清空旧集合完成迁移。恢复服务前确认旧 `update_profile` 写入器退出。

恢复备份时应先停止写入和 Worker，再用 SQLite backup API 从备份恢复目标，避免直接复制活跃的数据库文件而遗漏 WAL：

```python
import sqlite3
with sqlite3.connect("/absolute/backups/memory/selected.backup.sqlite3") as source:
    with sqlite3.connect("/absolute/staging/memory.sqlite3") as destination:
        source.backup(destination)
```

如果迁移后已产生新轮次、遗忘或删除，不能直接用旧备份覆盖；先停写并保全新增轮次和删除屏障，制定带增量与屏障的恢复方案，避免复活已经遗忘的数据。

## 5. 重新领取隔离任务

模型配置或校验问题修复后，可重新排队现有 `QUARANTINED` 任务。它保留原固定范围、游标和版本基线，不跳过来源；基线失效时仍由 Worker 正常拒绝。支持事实、摘要及索引任务。

```bash
python -m memory.migrate \
  --database /absolute/staging/memory.sqlite3 \
  --namespace lumenmind \
  --requeue-job TASK_ID \
  --user-id example-user
```

默认仅显示请求的维护动作，不打开数据库。确认目标后添加 `--apply --backup-dir /absolute/backups/memory`。应用操作先备份，再把该任务转为 `RETRY_WAIT`、清零内容失败计数、保留累计领取次数，写入 `maintenance_requeue` 审计。它不会清理或跳过坏批次，也不会把任务标记为成功。
