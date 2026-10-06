# 口播出稿 · kbcg-pipeline-win

中文口播视频 → 剪映原生草稿：AI 自动完成转写、粗剪、字幕、对齐，直接在你的剪映草稿箱里生成可继续编辑的工程。

[![contract-tests](https://github.com/sanwuqi01/kbcg-pipeline-win/actions/workflows/contract-tests.yml/badge.svg)](https://github.com/sanwuqi01/kbcg-pipeline-win/actions/workflows/contract-tests.yml)

## 快速开始

前提：Windows 10/11 · 剪映专业版（启动过至少一次）· `ffmpeg` 在 PATH · Python 3.11+ · 一个 AI agent（WorkBuddy / Claude Code / ZCode / Cursor）

**安装位置（两种方式通用）**：本地物理盘上的固定目录——建议纯英文、无空格；不要放桌面 / OneDrive / 网盘同步目录 / 移动硬盘 / 网络路径；所在盘剩余空间 ≥ 5GB（模型 1.6GB 和工作区产物都长在仓库目录里，装一次长期用，别换地方）。装在哪个盘都行，软件盘、工作盘都可以。

### 方式 A · 人工克隆（会一点命令行，自己掌握装在哪）

`git clone` 会在「当前目录」下生成 `kbcg-pipeline-win` 文件夹，所以先切到想装的位置再克隆：

```bash
cd /d E:\AI工具        ← cmd 写法（/d 允许跨盘符），换成你自己的盘和目录
git clone https://github.com/sanwuqi01/kbcg-pipeline-win.git
```

不想先切目录的话，把完整目标路径写在克隆命令末尾，一步到位：

```bash
git clone https://github.com/sanwuqi01/kbcg-pipeline-win.git "E:\AI工具\kbcg-pipeline-win"
```

克隆完用 agent 把这个目录作为工作区打开，直接跳到下面的「装好之后」。

### 方式 B · agent 一句话（推荐，粘给 agent 即可）

```text
把 https://github.com/sanwuqi01/kbcg-pipeline-win 克隆到你自己的工作目录下
（保持默认文件夹名 kbcg-pipeline-win；建议纯英文路径，不要放桌面/OneDrive/
网盘同步目录/移动硬盘，所在盘剩余空间 ≥ 5GB——模型 1.6GB 和工作区产物都在
仓库目录里，装好后长期使用，不要再挪动）。
克隆完成后：把该目录作为你的工作目录打开，先读 AGENTS.md，
然后执行环境安装（对话指令「安装口播出稿」，等价 部署/bootstrap.py --yes，
预计 10–30 分钟，主要是下载 1.6GB ASR 模型），
最后跑 脚本_win/pipeline.py doctor 自检并向我报告结论。
```

**装好之后（日常只有一句）**：视频丢进 `剪辑工作台\输入\<日期>\`，对话里说 **「用口播出稿，跑一下输入」**。AI 跑完全链，中途 6 个编辑判断点停下来等你确认，最后草稿直接出现在剪映草稿箱——打开剪映核对三项（素材可见 / 无权限提示 / 能出画面）即完成。

## 它是怎么工作的

```
视频 → 转写 → 粗剪（6 个决策 JSON：AI 按工序判断、契约脚本校验）→ 哈希冻结
     → 字幕分卡 + 意群断点自检 → 剪映原生草稿（真主轨 + 原生字幕）
```

- **契约层**（Python，42 项测试保障）：留删、时码、画幅的机械正确性，换任何模型跑结果一致
- **决策层**（6 个 JSON）：讲什么、留什么、怎么断句——质量取决于模型判断，推荐实测档 **GLM 5.3 Flash 或 DeepSeek V4.1 Flash**（换模型会让判断整体漂移而测试不报警，钉死清单见 `部署/环境契约.json`）

## 文档地图

| 想做什么 | 看哪里 |
|---|---|
| 工序语义唯一真值 | `.workbuddy/skills/kbcg-xgz-win-workbuddy/SKILL.md` |
| Agent 进仓库先读 | `AGENTS.md` |
| 装机与故障排查 | `部署/安装说明.md` |
| 工作台日常操作 | `剪辑工作台/开始在这里.md` |
| 分卡质量自检 | `脚本/核断点.py`（写完 cards.json 必跑） |
| 参与开发 | `CONTRIBUTING.md` |

## 许可

代码 MIT（LICENSE）· 样本库数据另见 `样本库/DATA_LICENSE.md` · 上游依赖按各自许可
