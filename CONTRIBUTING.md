# 贡献指南

## 先读三份文件

1. `AGENTS.md` — 本仓库的 agent 约定（铁律 + 任务地图）
2. `.workbuddy/skills/kbcg-xgz-win-workbuddy/SKILL.md` — 工序宪法（改任何
   契约脚本前必须理解你在动哪一层）
3. `tests/test_learning_contracts.py` — 42 项契约测试（你的改动会在这里被审判）

## 改动类型与要求

| 你在改什么 | 必须做 |
|---|---|
| 契约脚本（脚本/ 脚本_win/） | 重跑 42 项测试全绿；行为变化必须带测试 |
| SKILL.md 工序语义 | 同步 references/ 和 剪辑工作台/说明书/（README 的优先级表） |
| 剪辑工作台状态机 | `剪辑.py doctor` 通过 + merge_draft/p1a3/p4i 相关测试 |
| 文档 | 中英混排保持现状（中文为主）；命令一律 `%PY%` Windows 形态 |
| 样本库 | 不改文件内容（SHA-256 锚定）；新增样本按成对样本清单 schema 登记 |

## 提交流程

```bash
# 1. 测试
cd .workbuddy/skills/kbcg-xgz-win-workbuddy
python -m unittest tests.test_learning_contracts

# 2. 约定式提交
git commit -m "fix(p3): 字幕卡边界落入术语词根时报错而非静默通过"
```

提交信息用约定式前缀：`feat:` `fix:` `docs:` `test:` `refactor:` `chore:`。

## 不接受的 PR

- 绕过契约脚本直接改产物 JSON 的"快捷方式"
- 把 macOS 命令形态（python3/zsh/$PYMLX）重新引入文档
- 个人运行数据（输入视频/工作区/运行记录）——.gitignore 已挡，别用 -f
- 关闭或放宽 42 项测试中的任何一项（要改行为先改契约再改测试，写清楚理由）

## 新素材类型支持

想加双人口播、vlog 等新类型的完整工序：先在 SKILL.md「支持与停止」节
把它从停止改为支持，然后 子技能/ 加对应文档、样本库 加成对样本、
decision_contract 加 schema——缺一层就宁可保持"识别并停止"。
