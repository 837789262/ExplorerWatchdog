# ExplorerWatchdog

Windows 资源管理器（explorer.exe）看门狗 —— 实时监控并在崩溃时自动恢复。

---

## 背景

在 Windows 系统中，`explorer.exe` 负责提供桌面、任务栏和文件管理器等核心功能。当它因异常崩溃或被意外关闭时，用户会面临桌面消失、任务栏不见等问题，通常需要手动通过任务管理器重新启动。

**ExplorerWatchdog** 正是为了解决这一痛点而设计：它在后台持续监控 `explorer.exe` 的运行状态，一旦检测到异常退出，立即自动将其重新拉起，无需人工干预。

## 功能特性

- 🔄 **自动恢复** — 检测到 explorer.exe 崩溃或未运行后自动重启
- 🖥️ **会话隔离** — 基于 Windows Session ID 精确匹配当前用户会话，不会误判其他用户/远程桌面的 explorer
- 🔒 **单实例保护** — 通过命名互斥锁确保每个会话只运行一个 watchdog 实例，避免重复启动
- 📌 **系统托盘图标** — 纯 Win32 API 实现，零第三方依赖；双击查看状态，右键打开日志或退出
- 📝 **日志记录** — 自动轮转日志文件（单文件 2MB，保留 3 份备份），方便排查问题
- 📄 **状态文件** — 写入 `status.txt` 供外部程序确认 watchdog 是否存活
- 🔧 **SFC 修复（可选）** — 当 explorer.exe 文件本身丢失时，可调用系统文件检查器尝试修复
- 🚀 **开机自启** — 一键安装到用户启动目录，开机自动运行

## 系统要求

- **操作系统**：Windows 7 / 10 / 11 / Server
- **Python**：3.10+（安装脚本中默认使用 `pythonw.exe` 实现无窗口运行）
- **依赖**：无第三方 Python 库依赖，仅使用标准库 + Win32 API（通过 `ctypes`）

## 快速开始

### 安装（一键部署）

双击运行 `ExplorerWatchdog_Install.cmd`，脚本将自动完成以下操作：

1. 将 `explorer_watchdog.py` 复制到 `%LOCALAPPDATA%\ExplorerWatchdog\`
2. 生成启动器 `run_watchdog.cmd`
3. 在用户启动文件夹创建 `ExplorerWatchdog.cmd` 实现开机自启
4. 立即启动 watchdog（带托盘图标模式）

### 卸载

双击运行 `ExplorerWatchdog_Uninstall.cmd`，脚本将：

1. 删除用户启动文件夹中的自启项
2. 终止所有正在运行的 watchdog 进程

### 重启（更新后重新部署）

运行 `restart_explorer_watchdog.ps1`：

```powershell
.\restart_explorer_watchdog.ps1 -IntervalSec 10
```

## 命令行参数

`explorer_watchdog.py` 支持以下命令行参数：

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--interval` | float | `5.0` | 轮询间隔（秒） |
| `--try-repair-if-missing` | flag | 关闭 | explorer.exe 文件丢失时尝试 SFC 修复（需管理员权限） |
| `--log-file` | str | `%LOCALAPPDATA%\ExplorerWatchdog\watchdog.log` | 日志文件路径 |
| `--status-file` | str | `%LOCALAPPDATA%\ExplorerWatchdog\status.txt` | 状态文件路径 |
| `--once` | flag | 关闭 | 只执行一次检查并退出（调试用） |
| `--tray` | flag | 关闭 | 启用系统托盘图标 |

### 手动运行示例

```bash
# 基本运行（无托盘，前台运行，5 秒轮询）
python explorer_watchdog.py

# 启用托盘图标，10 秒轮询
pythonw explorer_watchdog.py --tray --interval 10

# 调试模式：只检查一次
python explorer_watchdog.py --once

# 开启 SFC 修复 + 自定义日志路径
python explorer_watchdog.py --try-repair-if-missing --log-file "D:\logs\watchdog.log"
```

## 托盘图标说明

启用 `--tray` 后，系统托盘区域会出现一个图标：

| 操作 | 效果 |
|---|---|
| **鼠标悬停** | 显示当前状态（版本号、Explorer 运行状态、重启次数） |
| **双击** | 弹出气球通知，展示详细状态 |
| **右键** | 弹出菜单：「打开日志」/「退出」 |

## 项目文件结构

```
restart_explorer_tool/
├── explorer_watchdog.py                    # 核心程序：监控 + 托盘 + 日志
├── install_explorer_watchdog_startup.ps1   # 安装/卸载核心逻辑（PowerShell）
├── ExplorerWatchdog_Install.cmd            # 一键安装入口
├── ExplorerWatchdog_Uninstall.cmd          # 一键卸载入口
├── restart_explorer_watchdog.ps1           # 重启脚本（停止 → 重装 → 启动）
└── README.md                               # 本文件
```

## 技术细节

### 工作原理

```
启动 → 获取 Session ID → 创建互斥锁（防重复）
  ├─ 托盘模式：后台线程跑监控循环 + 主线程跑 Win32 消息循环
  └─ 无托盘模式：单线程直接跑监控循环

监控循环（每 N 秒）：
  1. 检查 explorer.exe 文件是否存在
  2. 若存在 → 通过 tasklist 按 Session ID 检查进程是否运行
  3. 若未运行 → 自动启动 explorer.exe
  4. 更新状态文件 + 日志
```

### 关键设计决策

- **纯 `ctypes` 实现托盘**：不依赖 `pystray`、`tkinter` 等第三方库，减少部署复杂度
- **`pythonw.exe` 运行**：无控制台窗口弹出，完全后台静默运行
- **Session ID 过滤**：多用户/远程桌面场景下不会误判
- **命名互斥锁**：`Local\ExplorerWatchdog_Session_{id}`，确保同会话单实例
- **日志轮转**：`RotatingFileHandler` 避免日志无限增长占满磁盘

## 状态文件格式

`status.txt` 示例内容，可供外部监控工具读取：

```
version=1.2.1
session_id=1
last_check_ts=1720000000
explorer_running=1
restart_count=0
last_action=ok
```

## 许可

本项目仅供个人使用和学习。
