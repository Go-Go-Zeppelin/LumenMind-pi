# 参与贡献

欢迎通过 Issue 描述问题或提出改进，通过 Pull Request 提交修改。

## 本地验证

TypeScript 服务（Node.js 24）：

```bash
cd agent-service
npm ci
npm run typecheck
npm test
npm run build
```

Python 离线测试（从仓库根目录执行，Python 3.12）：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r python-service/requirements-test.txt
PYTHONPATH=python-service python -m pytest python-service/tests -q
docker compose config --quiet
```

完整服务运行依赖另见 `python-service/requirements.txt` 和 README。
离线测试使用 mock 或临时数据库；真实模型、Redis/Chroma 故障恢复测试需要独立环境。

## 提交要求

- 描述问题、修改后的行为及验证结果；将修改限制在相关范围内。
- 记忆模块开发以 [开发方案](docs/design/AGENT_MEMORY_DEVELOPMENT.md) 为依据，区分规划、已接通能力和已通过验收的能力。
- 不提交 `.env`、密钥、运行数据库、真实用户会话或个人信息。
- 使用合成样例和临时数据库验证，避免修改日常用户数据。
- `docs/acceptance/minimal-business/MINIMAL_BUSINESS_*` 是历史业务验收材料，不代表当前记忆升级已经验收。

提交贡献时，请确保你有权以本项目的 [MIT 许可证](LICENSE) 提供相关代码和材料。
