# 凭据与隐私审查记录

审查日期：2026-10-09（Europe/London）。这是当前代码快照的审查，不是对未来提交或整个电脑的无泄露保证。Claude Code 同时开发，因此本次没有修改其 Python 业务代码。

## 范围与方法

- 检查主工作区的代码、文档、配置、CI 和未提交文件；包括开发过程中出现的临时 debug 脚本。
- 使用 Gitleaks 8.30.1 扫描工作目录，包含被 Git 忽略的本地文件；排除 `.git/`、安装的 `.venv/` 与 `__pycache__/`。当前未发现实际家庭数据目录，本次未导入 NAS 数据。
- 使用 `gitleaks git --log-opts="--all --full-history"` 检查本地所有可达 refs 的提交内容。该扫描不代表已检查未获取的远端 refs、GitHub Actions secrets 或整个托管账户。
- 单独扫描本地 Git 配置、提交消息与暂存差异；手工检查配置、SDK 的端点/认证头继承、日志和 CI 凭据持久化行为。
- Scanner 下载自官方发布，并校验发布方 SHA-256。检测输出启用 `--redact=100`；原始报告保留在仓库外临时目录，不提交或上传，不用真实凭据尝试登录任何服务。
- [Gitleaks 的检测方式与命令](https://github.com/gitleaks/gitleaks)属于规则检测；无法识别所有自定义秘密，也不验证令牌是否有效。

## 结果

| 检查 | 结果 |
| --- | --- |
| 工作区 Gitleaks 扫描 | 未发现匹配 |
| 本地可达 Git 历史扫描 | 未发现匹配 |
| 本地 Git 配置与提交消息 | 未发现匹配 |
| 暂存差异 | 未发现匹配；扫描时没有暂存变更 |
| 当前配置示例与 CI | 未发现硬编码凭据 |
| `python -m selftest` | 本地合成样本检查通过 |
| Scanner 正向控制 | 在仓库外生成不具备服务授权的合成 token，规则检测成功且输出/报告完全隐藏匹配值 |
| SDK 环境继承复现 | 使用合成环境变量检查客户端配置，确认端点与认证头继承；没有发出网络请求 |
| 新增 CI workflow | YAML、最小权限与不持久化凭据配置检查通过；尚未在 GitHub runner 执行 |

`bench.md` 的模型 ID、量化配置和硬件信息不是认证凭据；测试中的 `deadbeef`、`cafebabe` 等是合成模型 digest。未发现已提交或当前代码中的 API key、密码、私钥匹配。下面的运行时路径仍需关闭，不能据此宣称零外传。

## 需要开发者修复的运行时问题

### 1. 默认 Ollama 客户端继承端点和认证头（优先）

位置：`models.py` 的 `embed_batch()`、`complete()`、`model_digest()`。

这些函数使用 `ollama.embed/chat/list`，即 SDK 的默认客户端。当前安装的 SDK 在初始化时读取 `OLLAMA_HOST`，并在存在 `OLLAMA_API_KEY` 时自动添加 Bearer Authorization。项目代码未限制 loopback，也没有清除该认证头。

如果启动环境继承了远端端点与 API key，文本和凭据可能发送到该端点。HTTP 代理环境、重定向和云模型选择也需要显式控制。这是代码路径风险；本次未发现实际令牌，也未观测到真实外传。

建议实现一个共享的本地客户端：显式固定或严格校验 `http://127.0.0.1:11434`；禁用环境代理与重定向；在最终发送请求前去掉 Authorization，而不是在客户端构造之后才修改环境；拒绝云模型配置，并校验服务 cloud-disabled 状态。SDK 客户端构造本身会继承 API key，只指定 `host` 不足以消除认证头。

验收：使用合成 key、远端 `OLLAMA_HOST`、代理环境和 mock transport，在不联网的测试中断言所有调用仅允许目标 loopback、请求无 Authorization、云配置被拒绝；检查 embed/chat/list 全部路径。

README 提供的专用终端配置与移除进程内 `OLLAMA_API_KEY` 是临时运行措施，不替代上述代码加固。

### 2. 原始异常可能进入日志

位置：`ingest.py` 的 `build_index()` 中 `_log(... ({e}))`。

异常来自解析器、SDK 或文件读取，可能包含 URL、服务器返回内容或输入片段。不能因为正常日志只输出路径/计数，就保证所有失败路径都不含凭据和家庭文本。

建议默认记录 run/file ID 与受控错误分类，保持原始异常在本机受保护的诊断渠道；共享日志前脱敏。

验收：以合成秘密字符串构造 parser/SDK 异常，捕获 stderr 并断言该字符串、认证头和原文没有出现。

### 3. 尚未完成运行时隐私边界验证

`run.bat`、Web UI、禁止意外外连、备份和跨构建周期锁尚未完整实现。当前无法证明 BitLocker、NAS 权限、云同步排除、server cloud-disabled 配置或实际网络行为已落实。不要把 README 的目标架构解释为已部署的保护。

在上述客户端与错误日志加固完成后，再用代表性数据进行本地网络验证。安装模型/依赖的联网阶段与运行阶段分开；运行时允许本地 Ollama 和指定 NAS，禁止意外目的地。

## 本次新增的预防措施

- `.gitignore` 增加 `.env`、常见私钥/证书容器、凭据 JSON、本地扫描报告；保留无秘密的 `.env.example` / `.env.sample` 模板。
- 新增 `.github/workflows/secrets.yml`：每次 push/PR 扫描完整可达历史与工作树，固定 scanner 版本、校验下载 SHA、仅授予读权限，checkout 不持久化 Git 凭据；不上传报告。
- README 明确当前实现、规划能力与未关闭的安全问题，提供本地启动配置。

忽略规则只防止通常的误添加，不能撤回历史、阻止 `git add -f` 或识别任意命名文件中的秘密。CI 检测在 push 后发生；第一次 push 前仍应在本地扫描。若未来发现真实凭据，应先撤销/轮换，再处理 Git 历史及相关日志，而不只删除当前文件。
