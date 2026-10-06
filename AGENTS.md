# AGENTS.md — 给 AI Agent 的入口约定

你是被宿主（WorkBuddy / Claude Code / ZCode / Cursor 等）调进本仓库的 agent。
本文件告诉你怎么在这个仓库里正确地干活。**先读完再动手。**

## 这个仓库是什么

中文口播视频 → 剪映原生草稿的自动剪辑管线。两层结构：

- **契约层**（`.workbuddy/skills/kbcg-xgz-win-workbuddy/`）：`脚本/` + `脚本_win/`
  的 Python 契约脚本 + 42 项回归测试 + SKILL.md 工序宪法。**与模型无关**，
  换谁跑都一样，由 CI 保障。
- **决策层**（content_plan / keep / expression_review / structure / cards /
  cut_review 这 6 个 JSON）：由 agent（你）按 SKILL.md 的工序语义填写，
  契约脚本校验。**质量取决于你的判断**，测试不覆盖语义质量。

## 改代码前必须跑

```bash
cd .workbuddy/skills/kbcg-xgz-win-workbuddy
python -m unittest tests.test_learning_contracts   # 42 项契约回归，必须全绿
```

测试是纯标准库、无外部依赖、~1 秒跑完。**没有任何理由跳过。**
改 `脚本/` 或 `脚本_win/` 下任何文件后重跑；改测试本身时说明理由。

## 三条铁律（违反 = 制造静默事故）

1. **不要绕过契约脚本直接改产物 JSON**。决策产物（keep/cards/…）只在
   闸口由你填写；用脚本改上游会让哈希链失效，冻结必然失败。
2. **不要静默降级**。环境缺件、素材类型不支持、契约校验失败 → 停下
   报告，不要"先跑通再说"。
3. **Windows 命令规范**：所有命令走 `"%PY%"`（管线解释器，解析顺序见
   SKILL.md「环境」节），不要用 `python3` / `zsh` / `$PYMLX`（那是
   macOS 原版残留）。UTF-8 由入口自举，不用手动 chcp。

## 常见任务地图

| 任务 | 入口 |
|---|---|
| 跑一条片子（对话式） | 读 SKILL.md → P0 起步 |
| 跑一条片子（工作台状态机） | `剪辑工作台/工具/剪辑.py`，先读 `剪辑工作台/开始在这里.md` |
| 环境自检 | `pipeline.py doctor`（含 Mac 残留扫描） |
| 分卡质量自检 | `脚本/核断点.py <工作目录>`（写 cards.json 后必跑） |
| 专名核查 | `脚本/核专名.py <工作目录>`（写 keep.json 前） |
| 新机器部署 | `部署/bootstrap.py`，读 `部署/安装说明.md` |

## 文档优先级（冲突时从高到低）

1. `SKILL.md`（工序宪法——语义、契约、失败条件的唯一真值）
2. `references/`、`知识库/`（按阶段读，不通读）
3. `剪辑工作台/说明书/`（工作台状态的工序抄本；与 SKILL.md 冲突时以
   SKILL.md 为准并回头改说明书）

## 提交规范

- 约定式提交：`fix:` / `feat:` / `docs:` / `test:` / `refactor:`
- 改契约脚本必须附测试证明（新增或现有测试覆盖该行为）
- 个人运行数据永不进库（.gitignore 已挡：输入/工作区/输出/运行记录/模型）

## 目录速览

```
├─ AGENTS.md            ← 你在这里
├─ README.md            面向人的安装与使用
├─ .github/workflows/   CI：42 项契约测试
└─ .workbuddy/skills/kbcg-xgz-win-workbuddy/
   ├─ SKILL.md          工序宪法（唯一语义真值）
   ├─ 脚本/ 脚本_win/    契约脚本（平台共享 / Windows 专属）
   ├─ tests/            42 项契约回归
   ├─ 部署/             bootstrap + 环境契约（钉死版本）
   ├─ 样本库/           成对学习样本（见 样本库/DATA_LICENSE.md）
   ├─ 知识库/ references/ 子技能/ 字体/
   └─ 模型/             空壳（bootstrap 下载 ~1.6GB，git 忽略）
```
