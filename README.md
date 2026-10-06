# 口播出稿 · Windows（kbcg-xgz-win-workbuddy）

中文口播视频 → 剪映原生草稿：AI 自动完成转写、粗剪、字幕、对齐，直接在你的剪映草稿箱里生成可继续编辑的工程。

> 一句话：把「丢一段口播视频」变成「剪映里出现一条粗剪+字幕完成的草稿」，中间 6 个编辑判断点由 AI 停下来和你确认。

## 它适合谁

- 口播/知识类 IP 的剪辑工作流（单人口播、单人直播切片、删提问者的学员好评采访）
- Windows 10/11 + 剪映专业版 + 一个 AI coding agent（WorkBuddy / Claude Code / ZCode / Cursor 等）
- 想要**可审计**的剪辑：每个留删决定、每张字幕卡、每个切口都有 JSON 留痕和哈希锁

## 架构：契约层与决策层分离

```
视频 ──► 转写(faster-whisper large-v3-turbo) ──► 统一词轴
      ──► 粗剪 6 个决策 JSON ◄── AI/人按 SKILL.md 填写（决策层）
      ──► 契约脚本校验 + 声学验收 + 冻结（契约层，42 项测试保障）
      ──► 字幕分卡 + 意群断点自检 ──► 剪映原生草稿（真主轨 + 原生字幕）
```

- **契约层**（Python 脚本）：留删/分卡/时码/画幅的机械正确性。哈希链锁住
  每一步产物，上游改了下游必须重审。换任何模型跑结果一致，CI 全绿保障。
- **决策层**（6 个 JSON）：讲什么、留什么、怎么断句。由 agent 按工序宪法
  （SKILL.md）填写，质量取决于模型与提示，契约只验证不判断。

## 快速开始

**前提**：Windows 10/11 x64 · 剪映专业版（启动过至少一次）· `ffmpeg`/`ffprobe` 在 PATH · Python 3.11+ · 一个 AI agent 宿主

1. **克隆并打开工作区**：用你的 agent 宿主打开本仓库根目录。
2. **对话里说：「安装口播出稿」**——agent 会跑 `部署/bootstrap.py --yes`
   （约 10–30 分钟，主要是下载 1.6GB ASR 模型），看到 `结论: ✅ 就绪` 即装好。
   手动方式见 `部署/安装说明.md`。
3. **验证**：
   ```cmd
   cd .workbuddy\skills\kbcg-xgz-win-workbuddy
   脚本_win\pipeline.py doctor        :: 环境自检，全 ✓
   python -m unittest tests.test_learning_contracts   :: 42 项契约回归
   ```
4. **跑第一条**：视频丢进 `剪辑工作台\输入\<日期>\`，对话里说
   「用口播出稿，跑一下输入」。中途 6 个编辑判断点会停下来等你。

详细使用手册：`剪辑工作台\开始在这里.md` · 工序宪法：`.workbuddy\skills\kbcg-xgz-win-workbuddy\SKILL.md` · Agent 约定：`AGENTS.md`

## 推荐配置（钉死项）

换宿主/模型会让**决策层**整体漂移（断句、删留、开头选择），而 42 项契约测试
仍然全绿不报警。因此推荐档为实测验证过的：

- 宿主：WorkBuddy 桌面版
- 模型：GLM 5.3 Flash 或 DeepSeek V4.1 Flash（二选一）
- 剪映：5.9.x（草稿结构按此版本验证）

完整钉死清单与机读真值：`部署/环境契约.json`。

## 六个编辑判断点（AI 会停下来等你）

| 闸口 | 你/AI 决定什么 | 产物 |
|---|---|---|
| 内容计划 | 讲什么、开头怎么选、主线 | `content_plan.json` |
| 原声留删 | 逐词留/删/修错字 | `keep.json` |
| 表达审查 | 口头语、停顿、残声逐项裁决 | `expression_review.json` |
| 结构编排 | 段落顺序、Hook 位置 | `structure.json` |
| 切口听审 | 每个刀口的声学确认 | `cut_review.json` |
| 字幕分卡 | 断句与关键词候选 | `cards.json` |

## 已知边界

- 只支持剪映原生草稿交付（Final Cut XML 分支代码保留但 Windows 禁用）
- 单人口播 / 学员好评采访有完整工序；双人口播、多位受访者会识别并**停止**
- ASR 热词：在 `剪辑工作台\配置\设置.json` 的「转写热词」里填你的 IP 专名，
  预防同音错字（实测：不填时 IP 人名曾整片转错）

## 许可

- 代码：MIT（见 LICENSE）
- 样本库数据：见 `.workbuddy/skills/kbcg-xgz-win-workbuddy/样本库/DATA_LICENSE.md`
- 上游依赖：[capcut-mate](https://github.com/Hommy-master/capcut-mate)（按其自身许可）、faster-whisper、Qwen3-ForcedAligner、silero-vad
