# 口播视频剪映出草稿（Windows 版）· {{VARIANT}}

中文口播视频 → 剪映原生草稿：AI 自动完成粗剪、字幕、对齐、包装，直接在你的剪映草稿箱里生成可继续编辑的工程。{{VARIANT_NOTE}}

> 打包日期：{{DATE}}
> 详细安装手册（含故障排查表）：`.workbuddy\skills\kbcg-xgz-win-workbuddy\部署\安装说明.md`

---

## 这个文件夹怎么用

**解压出来的这个文件夹本身就是一个 WorkBuddy 工作区**，不用往别处拷：

```
口播视频剪映出草稿-xgz-win-workbuddy-{{VARIANT}}\
├─ README.md                 ← 你在这里
├─ .workbuddy\skills\kbcg-xgz-win-workbuddy\   ← Skill 本体（项目级，打开工作区自动识别）
│   ├─ SKILL.md               ← 工序宪法（AI 靠它干活）
│   ├─ 脚本\ 脚本_win\         ← 契约脚本（转写/对齐/建段/出草稿）
│   ├─ 部署\                   ← 一键适配 bootstrap + 详细安装说明 + 版本契约
│   ├─ 样本库\ 知识库\ 子技能\ references\ 字体\ tests\
│   └─ 模型\                   ← 空壳，模型由 bootstrap 自动下载（约 3.4 GB）
└─ 剪辑工作台\                 ← 操作面：素材进、草稿出
    ├─ 输入\                   ← 视频丢这里（按天建日期文件夹）
    ├─ 输出\ 工作区\ 运行记录\   ← 空目录，跑起来才有内容
    ├─ 工具\ 配置\ 说明书\
    └─ 开始在这里.md            ← 工作台的使用说明
```

## 两步装好

**前提**：Windows 10/11 x64；装好 WorkBuddy 桌面版；装好剪映专业版**并启动过至少一次**；`ffmpeg`/`ffprobe` 在 PATH 里（`scoop install ffmpeg` 即可）。

1. **打开工作区**：WorkBuddy →「打开文件夹」→ 选解压出来的 `口播视频剪映出草稿-xgz-win-workbuddy-{{VARIANT}}`，会话模型选 **GLM 5.3 Flash 或 DeepSeek V4.1 Flash**（实测推荐档，二选一）。
2. **对话里说：「安装口播出稿」**。AI 会自动跑一键适配（约 10–30 分钟，主要在下载 3.4 GB 模型，可以离开），看到 `结论: ✅ 就绪` 即装好。

> 不用碰命令行。备用手段（仅当 AI 引导装不上时才用）：在 `部署\` 目录执行 `bootstrap.cmd`，等价直连 `python bootstrap.py --yes`。

## 怎么开工（唯一入口——对话里叫 AI）

1. 视频丢进 `剪辑工作台\输入\`（建议按天建文件夹，如 `输入\2026-09-22\xxx.mp4`）。
2. 在 WorkBuddy 对话里说：**「用口播出稿，跑一下输入」**。
3. AI 会自动跑完转写→粗剪→字幕→出草稿，中途在 6 个编辑判断点停下来跟你确认（不是报错）。
4. 完成后草稿直接出现在剪映草稿箱，打开剪映确认：项目素材可见、时间线无「无访问权限」、播放器能出画面。

> ⚠ **别直接双击 `剪辑工作台\工具\剪辑.cmd`** —— 没有 AI 在场，没人审错误、没人盯闸口。cmd 只是备用启动器。

{{FACTORY_BLOCK}}

## 验收（装完花 5 分钟做一次）

```cmd
cd /d "<这个文件夹>\.workbuddy\skills\kbcg-xgz-win-workbuddy"
.venv\Scripts\python.exe 脚本_win\pipeline.py doctor      :: 环境自检，全 ✓
.venv\Scripts\python.exe -m unittest tests.test_learning_contracts   :: 42 项契约回归，OK
```

再拿一条 30 秒~2 分钟的中文单人口播走一遍完整流程，剪映里四项全对（素材可见/无权限条/能出画面/字幕字体对）就是完全可用。

## 出问题先看哪

| 症状 | 去哪查 |
|---|---|
| bootstrap 某步失败 | `部署\安装说明.md` §4 手动装、§6 故障排查表 |
| 跑片卡住或报错 | `剪辑工作台\运行记录\<片名>.log` 最后几十行 |
| 不知道停下来干嘛 | 正常，是在等编辑判断；说「推到下一步」让 AI 写决策 JSON |
| 换电脑 / 再给别人打一份包 | `部署\安装说明.md` §7 |

**钉死项**（换了判断质量不可控，契约测试不会报警）：宿主 WorkBuddy 桌面版、模型 GLM 5.3 Flash / DeepSeek V4.1 Flash、Windows 10/11 x64。
