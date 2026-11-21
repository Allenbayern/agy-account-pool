# agy-account-pool

[English](README.md) | [简体中文](README.zh-CN.md)

面向 macOS 的 **Antigravity 客户端（Classic）+ `agy` CLI 统一账号池自动切换守护**。同时监听 **429 额度报错** 与 **Gemini 百分比阈值**，自动保持桌面端和 CLI 始终锁定同一健康账号，并在非紧急情况下避让正在生成的对话流。

---

## 解决什么问题

- **消除客户端与 CLI 账号分裂**：Antigravity Tools 自带热切只会更新桌面端凭据（Keychain 与 `oauth_creds.json` 等 3 个文件），漏写 `~/.gemini/antigravity-cli/antigravity-oauth-token`，导致 `agy` CLI 仍在用耗尽账号撞 429。
- **堵住“组均值虚高”盲区**：`quota_groups` 显示还剩 `6%`，但 `quota.models[]` 里具体 Gemini 模型往往早已 `0%`。本工具取 `min(分组余量, 主 Gemini 模型最小余量)` 判定真实健康度。
- **不打断正在生成的任务**：额度处于 `5%–15%` 时自动检查桌面端 `language_server.log` 是否正在流式生成，等空闲 `60s` 再切（最长等待 `300s`）；遇到 `429`、`≤ 5%` 或主模型 `0%` 则立刻强切。

## 核心架构

```text
  ~/.antigravity_tools/accounts/*.json      桌面端 language_server.log & cli-*.log
                  │                                        │
         (Gemini 余量 ≤ 15%)                      (429 RESOURCE_EXHAUSTED)
                  └───────────────────┬────────────────────┘
                                      ▼
                     agy-account-follow --watch (--mode pool)
                       ├─ 桌面端忙碌避让（静默 60s，最长等 300s）
                       ├─ 单账号隔离冷却（自动解析 Resets in ...）
                       └─ 桌面端健康优先：CLI 漂移时仅对齐 CLI，不重启客户端
                                      │
          ┌───────────────────────────┴───────────────────────────┐
          ▼                                                       ▼
  antigravity-tools switch --target classic          同步 ~/.gemini/* 4 份 CLI 凭据
       （更新 Keychain 与 language_server）            + current_agy_account_id 游标
```

## 主要特性

- **双条件自动触发**：
  1. **429 报错实时强切**：每 `3s` 按 `(inode, offset)` 增量监听客户端与最近 `cli-*.log`，过滤监控审计日志、503 与断网噪声；捕获真实 429 后解析 `Resets in XhYmZs` 隔离故障号，立刻双端切到最健康账号。
  2. **Gemini 双层百分比阈值（默认 `15%`）**：只盯 Gemini 主组与主模型，不受 Claude/GPT 组耗尽误伤；`≤ 5%` 或主模型归 `0%` 立即切，其余情况等客户端空闲 `60s` 再切（最长等 `300s`）。
- **客户端健康优先原则**：若桌面端账号健康而 CLI 不一致，只同步 CLI 文件与游标，绝不无故重启桌面端 `language_server`。
- **CLI JIT 钩子（`--jit`）**：可选为 `~/.gemini/bin/agy` 打入轻量钩子，每次调用 `agy` 前非阻塞对齐一次凭据。
- **日志自动轮转与去重**：`~/Library/Logs/agy-account-follow.log` 达 2 MB 自动轮转为 `.log.1`，LaunchAgent 预设 `AGY_FOLLOW_QUIET=1` 使 `err.log` 仅记录真实未捕获异常。
- **零第三方 Python 依赖**：仅使用 macOS 自带 `Python 3.9+` 标准库。

## 运行前提

- **macOS**（依赖 `launchd`、`osascript` 通知及 `antigravity-tools` Keychain 切换）
- **Python 3.9+**（系统 `/usr/bin/python3` 即可）
- **Antigravity.app** 与 **[Antigravity Tools](https://github.com/lbjlaq/Antigravity-Manager)**（确保 `antigravity-tools` 在 `PATH`、`~/.local/bin/` 或 `/Applications/Antigravity Tools.app/Contents/MacOS/antigravity-tools`）

## 快速安装

```bash
git clone https://github.com/Allenbayern/agy-account-pool.git
cd agy-account-pool
./install.sh
```

安装完成后，建议在 **Antigravity Tools 设置** 中确认以下 3 项（避免两套切换器冲突）：

1. **关闭** 自带 **Classic 自动热切换**（`classic_auto_switch.enabled = false`）
2. **开启后台自动刷新**，并将 **刷新间隔** 设为 **5 分钟**
3. **开启开机自启动**（保证重启电脑后额度 JSON 持续刷新）

## 账号白名单 / 黑名单配置（可选）

默认允许 `~/.antigravity_tools/accounts/` 下的全部可用账号参与轮换。如需排除个人主号或只允许指定工作号，可创建 `~/.config/agy-account-follow/config.json`（参考 `config.example.json`）：

```json
{
  "allowed_accounts": [],
  "excluded_accounts": ["personal@example.com"]
}
```

也支持环境变量 `AGY_EXCLUDED_ACCOUNTS` 与 `AGY_ALLOWED_ACCOUNTS`（逗号分隔）。

## 常用运维命令

```bash
# 查看当前桌面端 / CLI 账号与全池健康度排行
agy-account-follow --check --mode pool --threshold 0.15

# 手动双端切换到指定账号（请在独立终端执行，避免打断客户端内嵌对话）
agy-account-follow --switch-to acct2@example.com

# 清空所有单账号隔离冷却计时
agy-account-follow --clear-cooldowns

# 查看 / 重装 ~/.gemini/bin/agy JIT 钩子
agy-follow-wrapper-hook --status
agy-follow-wrapper-hook --install

# 实时查看切换日志
tail -f ~/Library/Logs/agy-account-follow.log

# 运行单元测试
python3 -m unittest discover -s tests -v

# 卸载 LaunchAgent、钩子与软链（不会删除你的账号凭据）
./uninstall.sh
```

## 安全与免责声明

- 纯本地运行，不发起额外联网请求，不包含任何遥测上传。
- 日志与报错自动脱敏（`ya29.*`、`1//*`、`eyJ*`、`Bearer ...` 等凭据串一律替换为 `<REDACTED>`）。
- 仅用于同步管理个人合法拥有的多账号本地客户端状态，请遵守相关平台服务条款。

## 开源协议

[MIT](LICENSE)
