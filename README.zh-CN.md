# mpv-dandanplay-danmuku

[English](./README.md)

为 **mpv** 提供弹幕（bullet-chat）覆盖，数据来自
[dandanplay](https://www.dandanplay.com/) — 该聚合站汇总了 Bilibili、
Acfun、巴哈姆特、Tucao、爱奇艺等平台的弹幕。脚本会自动把当前播放的视
频匹配到 dandanplay 的剧集，下载弹幕并以滚动 ASS 字幕的形式渲染到
mpv 画面上。

![预览](./docs/preview.jpg)

## 鸣谢

本项目是
[Izumiko/Jellyfin-Danmaku](https://github.com/Izumiko/Jellyfin-Danmaku)
（在 Jellyfin 网页端添加弹幕的浏览器脚本）**大部分代码与功能设计的移
植与重新实现**。匹配算法、来源平台过滤（B站/巴哈/弹弹/其他）、来源标
签解析、弹幕去重、防重叠、CORS 代理回退等核心逻辑均来自该项目。

如果你使用本脚本，**请同时给该上游项目点 star 表示支持** —— 真正的
产品设计和算法工作是他们做的，我们只是把它从「浏览器里的 JS」翻译成
「mpv 里的 Lua」。

## 功能

- **自动匹配**：根据文件名，或在通过 `jellyfin-mpv-shim` 启动时根据
  Jellyfin 元数据。
- **手动模糊搜索**（Ctrl+F10）：两级选择器 — 输入关键词 → 选番剧 →
  选剧集。
- **播放器内设置面板**（Shift+F10）：透明度、字号、速度、密度、显示
  区域、渲染模式、防重叠、来源过滤、去重、每集时间偏移等。
- **作为 mpv 二级字幕加载**：与正常字幕共存，不会替换主字幕。
- **防重叠（默认开启）**：当某条弹幕找不到空闲行时直接丢弃，而不是
  在视觉上和已有弹幕叠在同一行。
- **CJK 感知的行宽计算**：按字符种类逐个测量（中文 1.0 em / 英文
  0.55 em / 描边像素），同一行连续两条中文弹幕不会再发生重叠。
- **分带滚动行分配**：弹幕优先填屏幕上 1/4，行满才扩展到 1/2、3/4、
  全屏，弹幕较少时聚集在视觉显眼处。
- **智能去重**：把刷屏弹幕折叠为一条；累计 ≥ N 条会显示 `[+N]` 标
  注，让梗弹幕仍有可见性。
- **每集时间偏移**：跨会话持久化保存。
- **来源过滤**：按平台屏蔽（B站/巴哈/弹弹/其他）。
- **关键词过滤**：支持 fnmatch 通配符。

## 系统要求

- mpv ≥ 0.40（0.39 大体可用，但自由文本输入用的 `mp.input.get` 是从
  0.39 开始才有的）。
- Python 3.8+ 在 PATH 上（仅用标准库，不需要 pip）。
- 可访问 dandanplay（或者你自己部署的 CORS 代理）。

## 安装

### 一行命令（Linux / macOS / Windows）

```bash
git clone https://github.com/<your-user>/mpv-dandanplay-danmuku.git
cd mpv-dandanplay-danmuku
python3 install.py
```

安装脚本会把脚本包复制到 mpv 的配置目录下（`scripts/dandanplay/`）
并初始化 JSON 配置。它能自动识别：

| 平台 | mpv 配置目录 |
|---|---|
| Linux | `~/.config/mpv`（识别 `$MPV_HOME`） |
| macOS | `~/.config/mpv`（识别 `$MPV_HOME`） |
| Windows | `%APPDATA%\mpv`（识别 `%MPV_HOME%`） |

之后重启 mpv（或 `jellyfin-mpv-shim`）即可。

### 手动安装

把两个文件放到 mpv 配置目录就行：

```
<mpv-config>/
└── scripts/
    └── dandanplay/
        ├── main.lua            # 来自本仓库 scripts/dandanplay/
        └── danmaku_helper.py
```

mpv 会把 `scripts/dandanplay/` 当作脚本包整体加载，不需要任何
init/require 步骤。

如需，再把 `examples/danmaku-config.json` 和
`examples/danmaku-settings.json` 复制到 `<mpv-config>/` 作为默认配置。

### 安装脚本其他命令

```bash
python3 install.py --status     # 查看当前安装状态
python3 install.py --uninstall  # 卸载（保留你的凭证文件）
```

## 使用

### 自动匹配

mpv 开始播放时脚本会尝试匹配：

1. **Jellyfin**（通过 `jellyfin-mpv-shim` 启动时）：解析
   `media-title`，例如 `Series Name s01e03 - Episode Title`，提取剧名/
   季/集 → 搜 dandanplay → 拉取对应弹幕。
2. **本地文件**：解析文件名（例如 `[组] 番名.S02E05.[1080p].mkv`）做
   同样的事。识别常见格式：`S01E03`、`1x03`、`EP03`、`第03话`、
   `[03]`、`- 03 -`。

成功匹配会被缓存，下次同剧同集瞬间命中。

### 手动搜索（Ctrl+F10）

如果自动匹配失败，按 `Ctrl+F10`：

1. 弹出文本框：输入任意模糊关键词（`尖帽子`、`frieren`…）。
2. **第一级 — 番剧选择器**：列出所有匹配到的番剧（含集数与类型），
   用 `↑↓` / `PgUp/PgDn` / `1-9` 选定。
3. **第二级 — 剧集选择器**：所选番剧的全部剧集，按 `Enter` 即加载。

匹配会被缓存，下次同剧同集自动加载。

### 快捷键

| 按键 | 作用 |
|---|---|
| **F10** | 弹幕开关 |
| **Shift+F10** | 设置面板 |
| **Ctrl+F10** | 手动搜索 |
| **Esc**（面板内） | 关闭面板 |

设置面板覆盖了所有可调项：`透明度`、`字号`（在该行按 Enter 可手动输
入）、`速度`、`密度`、`显示区域`、`渲染模式`（保留原始/强制右→左/强
制左→右）、`弹幕防重叠`、`繁简转换`、`时间偏移`（Enter 手动输入）、
`弹幕来源`、`显示模式`、`弹幕去重`及其窗口、阈值。

播放时，画面右上角会出现可点击的 **`弹`** 图标（与 mpv OSC 同步显示
／隐藏），点一下即可开关弹幕。

### 配置文件

脚本会从 mpv 配置目录读取三个 JSON：

| 文件 | 用途 |
|---|---|
| `danmaku-settings.json` | 全局默认设置（面板修改后会写回） |
| `danmaku-config.json` | CORS 代理地址 |
| `danmaku-credentials.json` | dandanplay AppId / AppSecret（如已申请） |

`filter_keywords`（关键词过滤）只能从 `danmaku-settings.json` 编辑，
支持 fnmatch 通配符（`*`、`?`、`[abc]`），整段文字匹配。完整字段参
考 `examples/danmaku-settings.json`。

## 强烈建议：申请你自己的 dandanplay AppId

**默认情况下脚本使用的是上游 Izumiko/Jellyfin-Danmaku 慷慨提供的
CORS 代理 CloudFlare Worker（`ddplay-api.930524.xyz`）。** 这个代
理在服务端帮我们做了 dandanplay v2 API 所需的 HMAC 鉴权，让未注册
的客户端也能直接查询。开箱即用很方便，但有两个问题：

1. **是别人帮你撑着的免费服务**。如果本脚本变得流行，请求量大了可
   能会被限流或下线。
2. **你的弹幕功能依赖于它的可用性**。任何让代理失联的事（DNS、证
   书、维护者不再维护…）都会导致你的弹幕功能不可用，而你自己无法
   修。

**正确做法是去申请你自己的 dandanplay AppId**：

1. 按 [开放平台文档](https://doc.dandanplay.com/open/) 给
   `kaedei@dandanplay.net` 发邮件申请 AppId / AppSecret（1–3 天
   审核）。
2. 拿到之后，在 mpv 配置目录下：
   ```bash
   cp danmaku-credentials.json.example danmaku-credentials.json
   $EDITOR danmaku-credentials.json   # 填上 app_id 和 app_secret
   ```
3. 重启 mpv。helper 会用 HMAC-SHA256 自己签名，直连
   `api.dandanplay.net`，CORS 代理这一段被完全绕开。

直连模式同时还更快（少一跳）也更隐私（请求只到 dandanplay，不
经过第三方 worker）。

如果你更倾向自己部署 CORS 代理，可以把
[Izumiko 的 `cf_worker.js`](https://github.com/Izumiko/Jellyfin-Danmaku/blob/master/cf_worker.js)
部署到自己的 CloudFlare Workers 账号，再把 URL 填到
`danmaku-config.json` 的 `cors_proxy` 字段。

## 故障排查

- **Jellyfin 路径下匹配不到**：脚本对网络流加了 1.5 秒等待时间（mpv
  在 `file-loaded` 时刻 `media-title` 还没拿到正确值）。如果仍然不
  行，脚本会把诊断信息写到 `$TMPDIR/danmaku-debug.log`（或对应平台
  的位置），把内容贴出来即可。
- **弹幕全部堆在屏幕最上方、不滚动**：原因是
  `secondary-sub-ass-override` 还是默认的 `strip`（会把所有定位标签
  剥掉）。脚本在加载 ASS 时会自动把它设为 `no`，如果你用别的方式
  加载 ASS，请在 `mpv.conf` 里加：
  ```
  secondary-sub-ass-override=no
  secondary-sub-pos=0
  ```
- **找不到正确的 Python**：脚本默认调用 `python3`（Linux/macOS）或
  `python`（Windows）。可以用环境变量 `DANMAKU_PYTHON=/path/to/python`
  覆盖。
- **自定义 mpv 配置目录**：识别 `$MPV_HOME`，与 mpv 本身一致。

## 项目结构

```
mpv-dandanplay-danmuku/
├── README.md                       # 英文版
├── README.zh-CN.md                 # 本文件
├── LICENSE                         # MIT，含上游致谢
├── install.py                      # 跨平台安装脚本
├── scripts/
│   └── dandanplay/                 # 真正的 mpv 脚本包
│       ├── main.lua
│       └── danmaku_helper.py
├── examples/
│   ├── danmaku-config.json
│   ├── danmaku-credentials.json
│   └── danmaku-settings.json
└── docs/
    └── preview.jpg
```

## 许可证

MIT — 见 [LICENSE](./LICENSE)。本项目的算法设计大量来自
`Izumiko/Jellyfin-Danmaku`，请同时为该项目点 star 表示支持。
