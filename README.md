# 口播出稿 · kbcg-pipeline-win

中文口播视频 → 剪映原生草稿：AI 自动完成转写、粗剪、字幕、对齐，直接在你的剪映草稿箱里生成可继续编辑的工程。

[![contract-tests](https://github.com/sanwuqi01/kbcg-pipeline-win/actions/workflows/contract-tests.yml/badge.svg)](https://github.com/sanwuqi01/kbcg-pipeline-win/actions/workflows/contract-tests.yml)

## 快速开始

前提：Windows 10/11 · 剪映专业版（启动过至少一次）· `ffmpeg` 在 PATH · Python 3.11+ · 一个 AI agent（WorkBuddy / Claude Code / ZCode / Cursor）

```bash
git clone https://github.com/sanwuqi01/kbcg-pipeline-win.git
cd kbcg-pipeline-win
```

1. 用 agent 打开本目录，对话里说：**「安装口播出稿」**——自动装环境（含 1.6GB 模型下载，10–30 分钟），看到 `结论: ✅ 就绪` 即装好
2. 视频丢进 `剪辑工作台\输入\<日期>\`，对话里说：**「用口播出稿，跑一下输入」**
3. AI 跑完全链，中途 6 个编辑判断点停下来等你确认，最后草稿直接出现在剪映草稿箱——打开剪映核对三项（素材可见 / 无权限提示 / 能出画面）即完成

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
