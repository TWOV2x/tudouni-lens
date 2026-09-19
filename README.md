# tudouni-lens

**Codex / Claude Code · API 生图视频**

土豆泥粉提了很久：API 接到 Codex、Claude Code 这类桌面端，聊天是通的，图和视频就是不行。

今天把这件事补上了。

这是给外部大脑用的图视 skill。不另开网页，不来回拖文件。下载、放上你们在 [tudouni-api.com](https://tudouni-api.com) 的 Key，对着对话说「出一张…」「出一条视频…」，成品进当前项目。

只做图和视频。文字模型它碰都不碰——桌面端自己已经会聊了。

## 桌面端实拍

人话进对话，图和视频掉进项目。下面两张是同一套 skill 在 Claude Code 里跑出来的。

出图：`image-2.5` · `2K` · 慈父手中线，游子身上衣

![慈父手中线，游子身上衣 · gpt-image-2.5-2k](docs/examples/cifu-youzi-2k.png)

出视频：`MiniMax-H3` · 最短秒数 · 孙悟空 vs 猪八戒卡丁车跑圈

![孙悟空 vs 猪八戒卡丁车 · MiniMax-H3](docs/examples/wukong-bajie-kart.jpg)

## 现在能出什么

货盘跟你这把 Key 走，插件里不写死。这一刻货架上是这些（ID 叫什么就是什么）：

**图**

- `gpt-image-2`（1K / 2K / 4K）
- `gpt-image-2.5`（1K / 2K / 4K）
- `gpt-image-2.5-sunburst`（1K / 2K / 4K）
- `nano-banana-pro`
- `nano-banana-2`
- `nano-banana-2-leo`

**视频**

- `MiniMax-H3`
- `H3video-2k`
- `doubao-seedance-2-0-260128`
- `doubao-seedance-2-0-fast-260128`
- `doubao-seedance-2-0-mini-260615`
- `doubao-seedance-2-5-260628`
- `sd2-fast`
- `sd2.5`
- `sd2-pro`
- `wan3.0-video`

以后广场上新什么，打开 skill 就能看见什么。现价去 [模型广场](https://tudouni-api.com) 看，这里不念价。

## 怎么用

1. 把本仓库拷到
   - Claude Code：`%USERPROFILE%\.claude\skills\tudouni-lens`
   - Codex：`%USERPROFILE%\.agents\skills\tudouni-lens`
   文件夹名必须是 `tudouni-lens`。
2. 在 tudouni-api.com 开一把 Key，**单独一行**写进 `%USERPROFILE%\.tudouni\api_key`。别把 Key 发到聊天里。
3. 对话里：Claude Code 打 `/tudouni-lens`，Codex 打 `$tudouni-lens`。

然后直接说人话就行：

- 「出一张小猫打篮球」
- 「image-2.5 的 2K，慈父手中线游子身上衣」
- 「出一条 MiniMax-H3，孙悟空和猪八戒卡丁车」
- 「我能出什么」

默认出图 1K。要 2K / 4K 说一声。视频会先问一句确认，再提交。换 Key 覆盖钥匙盒那个文件，不用关软件。

macOS / Linux 钥匙盒：`~/.tudouni/api_key`

## 它怎么接上的

桌面端原生对话走的是文字接口。图和视频是另一路：图片生成 / 改图、视频提交再轮询。这个 skill 只走那一路，连的是 tudouni-api.com，货盘按你这把 Key 实时拉。钥匙放在本机盒子里，不进对话、不进仓库。

所以不是「再做一个聊天机器人」，是把你们已经付过钱的图视能力，接到你们每天待着的 Codex / Claude Code 里。

本 skill **只连接 tudouni-api.com**，不能改成别的中转站客户端（见 LICENSE）。

## 一句体己的

你们提需求的时候，我们听得见。桌面端能聊不能画，这件事憋很久了。先把图和视频接上。用的时候卡了、货盘对不上、Key 换了出不来，来面板找我们。别把钥匙发到聊天里——盒子写好就行，我们不看你们的 Key。

晚上赶稿、白天试镜，说一声「出一张」，它就该在项目目录里。
