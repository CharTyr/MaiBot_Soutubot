# SoutubotSearch — MaiBot 以图搜源插件

Soutubot + SauceNAO + Google Lens 三路搜图源，主查漫画本子与插画出处，创作者平台优先。

## 安装

通过 MaiBot WebUI 插件市场搜索 `soutubot` 安装，或手动克隆：

```bash
cd plugins/
git clone https://github.com/CharTyr/MaiBot_Soutubot.git CharTyr_Soutubot
```

安装后配置 `config.toml` 填入 SauceNAO / SerpAPI 密钥即生效。

## 引擎分工

| 引擎 | 用途 | 需要密钥 |
|---|---|---|
| **Soutubot** | 漫画/本子/页局部（主 nHentai） | 无 |
| **SauceNAO** | 插画出处主引擎（Pixiv/X/Booru…） | `saucenao.api_key` |
| **Google Lens**（SerpAPI） | 插画补充源（网页/社交转载） | `google.api_key` |

插画可信度排序：**X / Pixiv / Fanbox 等创作者平台 > 镜像站 > 搬运站**。

## Planner 工具

```
search_image(msg_id, intent="manga")         # 漫画/本子（Soutubot）
search_image(msg_id, intent="illustration")  # 插画（SauceNAO + Google）
search_image(msg_id, engines="google")       # 只用 Google Lens
```

## 命令

| 命令 | 说明 |
|---|---|
| `/soutu` | 漫画/本子搜索（Soutubot） |
| `/sauce` | 插画搜索（SauceNAO + Google） |
| `/sauce google` | 只用 Google Lens |
| `/soutu_status` | 查看插件状态与配额 |

## 配置（`config.toml`）

```toml
[plugin]
enabled = true
config_version = "0.3.0"

[search]
base_url = "https://soutubot.moe"
timeout_seconds = 60
max_retries = 1
default_strict = false
min_similarity = 28.0
max_results = 6
cooldown_seconds = 8
send_preview = false

[saucenao]
enabled = true
api_key = ""           # SauceNAO API key
endpoint = "https://saucenao.com/search.php"
timeout_seconds = 45

[google]
enabled = true
api_key = ""           # SerpAPI key
endpoint = "https://serpapi.com/search.json"
with_illustration = true   # Google Lens 参与插画搜索
timeout_seconds = 45
```

Google 路径会先把图临时上传到图床再调 Lens（SerpAPI 需要公网 URL）。

## 运行状态

- v0.3.0 在 MaiBot 1.3.16 / SDK 2.16.0 上运行正常
- Soutubot 免密钥直连，SauceNAO / Google 需要各自 API key
