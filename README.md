# ED2K Notifier

一款专为电影、电视剧文件打造的 Windows 桌面端（及命令行）ED2K 计算、TMDB 匹配与 Telegram Bot 推送工具。

## 核心特性

- **高速流式 ED2K 计算**：基于 `pycryptodome` C 加速 MD4 引擎（内置纯 Python RFC 1320 MD4 回退），流式分块（9,728,000 字节/块）低内存计算几十 GB 的 4K 原盘/REMUX，支持 eMule 标准 `blue` 与旧版 `red` 模式，实时显示单文件速度与整批总进度/剩余时间（ETA）。
- **TMDB 影视信息与海报匹配**：自动解析中英文片名、年份、`S01E01` / `EP01` / `第X季第X集` / 字幕组标签及父目录，自动识别 TMDB v3 API Key 或 v4 Read Access Token，优先拉取对应季海报，并支持界面内手动搜索/按 TMDB ID 切换匹配及一键同步整季。
- **视频画质规格 & Telegram `#标签`**：自动提取 `2160p/4K`、`杜比视界/HDR10+/HDR`、`WEB-DL/BluRay/REMUX`、`Netflix/Disney+/AppleTV/HBO`、`H.265/H.264/AV1`、`DDP5.1/Atmos/TrueHD`、`中字` 等规格，附带便于频道/收藏夹检索的 `#电视剧 #4K #Netflix` 标签。
- **整季打包 + Telegraph 链接笔记推送**：
  - 同剧同季文件全部算完后自动按集数升序合并为一组推送。
  - 默认自动创建纯净的 **Telegraph (`telegra.ph`) 链接笔记**，在 Telegram 中只发送 **1 条整洁的 TMDB 海报卡片 + 笔记链接 + 一键直达按钮**（支持切换代码块模式或额外附带 `.txt` 链接清单、多 Chat ID 同时推送、HTTP/SOCKS5 代理及失败退避重试）。
- **本地 SQLite 秒算缓存与历史记录**：自动缓存已计算文件的 `(路径, 大小, 修改时间)`，补新集拖整文件夹时旧文件 0 秒命中；内置历史记录检索窗口，随时复制、导出 `.txt` 或重新推送。
- **现代化桌面 UI**：适配 Windows 11/10 原生深/浅色标题栏、统一 2x HiDPI 矢量图标、圆角海报与规格芯片、空态拖拽引导区、多选右键菜单，自动记忆窗口尺寸与列宽。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 启动图形界面
.\.venv\Scripts\python.exe main.py

# 或使用命令行模式（适合与下载器/脚本联动）
.\.venv\Scripts\python.exe cli.py "D:\Movies\流浪地球2.2023.2160p.mkv"
```

## 打包独立 Windows EXE

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\build.ps1
```

生成的单文件可执行程序位于 `dist\ED2K_Notifier.exe`，配置文件 `config.json` 与历史缓存 `history.db` 均便携保存在程序同目录下。

## 运行测试

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```
