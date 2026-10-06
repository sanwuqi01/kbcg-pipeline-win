# 剪辑工作台 · Agent 运行守则（Windows 通用坑 + 管线避坑）

> 写给下一个跑工作台的 agent：动手前先读完这页，能省一半往返。
> 宿主环境的命令**照抄，别试错**；管线坑是「遇到症状直接对症」，不用重新排查。
>
> **动手写第一份 JSON 之前，先过一遍《AGENT开工反问清单.md》（同目录）——先问/查清楚再干，别跑完再返工。**

---

## 1. Windows / 宿主环境通用坑（照抄命令，别试错）

| 场景 | 别这样做 | 这样做 |
|---|---|---|
| 跑状态机 | ❌ 双击 `剪辑.cmd`（含 `pause`，非交互环境卡死） | ✅ pwsh：`$env:PYTHONUTF8="1"; python -X utf8 工具/剪辑.py run` |
| 选命令工具 | ❌ Bash 工具（shim 缺 coreutils，`ls`/`grep` 都可能没有） | ✅ 一律用 PowerShell 工具；**它吞 stdout**（只回 exit code）→ 输出重定向到文件再 Read |
| 中文乱码 | ❌ 直接 `> 文件` 重定向（系统按 GBK 解码 python 的 UTF-8 输出 → 乱码） | ✅ 命令前加 `[Console]::OutputEncoding=[Text.Encoding]::UTF8`，重定向用 `*>&1 \| Out-File -Encoding utf8 <文件>` |
| bash 语法残留 | ❌ `PYTHONUTF8=1 python ...`（前缀设环境变量是 bash 写法，pwsh 直接报错） | ✅ pwsh 写法：`$env:PYTHONUTF8="1"; python -X utf8 ...` |
| 长输出 | ❌ 指望从工具回显里读长输出 | ✅ 重定向到文件再 Read；查文件用 Glob/Grep/Read 工具 |
| 写路径 | ❌ `/e/00 ...` 这类 Unix 风格路径 | ✅ 一律 Windows 正斜杠 `E:/某个 目录/...`（含空格路径加引号） |

**标准一条龙模板**（跑完用 Read 工具读 `运行记录\_输出.txt`）：

```powershell
[Console]::OutputEncoding=[Text.Encoding]::UTF8; $env:PYTHONUTF8="1"; python -X utf8 工具/剪辑.py status *>&1 | Out-File -Encoding utf8 运行记录\_输出.txt
```

---

## 2. 管线通用坑（遇到症状直接对症，别重新排查）

1. **合并报 frame_rate_raw 冲突**：VFR 素材各期 ffprobe 分数不同、VFR 丢帧把容器 avg 再拉低一点，都是**正常现象**。`合并草稿.py` 已带浮点容差 3.0%；真档位差（30 vs 25 = 16.7%）仍会响亮失败。不要再去核素材。
2. **登记草稿箱报「索引不存在」**：`--root-meta` 必须传 **`root_meta_info.json` 文件路径**（剪映草稿箱索引文件），不是草稿箱目录。别手动绕。
3. **F1 帧级验收 A1 FAIL（段头早于发音数帧）**：先查 word_track 里的 **orphan/weak 零时长幻词**（ws==we，blk=-1），按验收报告反推真实起音改时码（onset = in + fail_frames/60 + 0.014），**不要去改段或切口**。改完重跑建段之后的所有步骤；表达候选会自动重建并继承旧裁决，只需补 pending 项。
4. **对齐报「VAD 钳制后端点偏移过大」**：成因是**近重复音频上开对齐窗口**——同句复述删掉其中一遍后，保留文本的窗口一开就对到了被删那遍的音频上。**修法：近重复的两遍都保留**（decision=selected，role 写「强调复述/起头复述」），节奏问题交给停顿压缩处理。删重复前先想这条。
5. **合并草稿不带 `--only` 会把后续日期未交付的期一起卷进来**（卡死在「还没就绪」）。挑期合并：`python 工具/合并草稿.py <日期> --only "期甲,期乙"`（逗号分隔，顺序=接龙顺序）。
6. **对齐报「p1a: config.proxy 不存在」/「音频缓存绑定的素材与 config.proxy 不一致」**：素材被挪了文件夹，config.json 的 `proxy/original_path/media.path` 与两处音频缓存绑定（`_vad16k.meta.json` 的 `source`、`word_track.json` 的 `audio_cache.source`）全指旧路径。**修法：同步路径，不要重跑 p1a**（重跑=重新 ASR，词轴可能变化，keep.json 全作废）。先用 sha256 核内容未变，再改三处路径+两处绑定。
7. **合并建稿报 SegmentOverlap（配图段）**：VFR 素材上两条时间轴逐段舍入漂移，同句多图一多必叠。`合并草稿.py` 已带合并端钳制；再遇此报错先确认工具带这段逻辑。
8. **放配图触发词唯一性校验必须用拼接后的成片文本**（`"".join(w["w"] ...)`），不能对 rough_segments.json 原文做 count——JSON 里词是逐字 token 存储，多字词会在原文碎片里假命中。
9. **配图改名/换图后删 `配图计划.json`**：mtime 不变时 p4i 不重算 → 删掉工作区里的 `配图计划.json` 强制重算，再跑状态机。p4i 同句多图只保留**触发词最长**的一张；trigger 必须是成片文本的**连续**子串。标准版分发包没有 `配图工厂\`，手工放成品图走 `工序与闸口.md` 第 18 步即可。

---

## 3. 幂等与安全

- 状态机**重复跑是安全的**：每步按「产物在不在」判断，做完的跳过。删某个 JSON 只会重做该环节之后的部分，不会重新转写。
- 裁决 JSON 是**编辑判断**，机械对齐可以写脚本批量生成，但「删什么留什么」的内容裁决必须基于听感/文本事实，不得编造。
- ⚠ **不要删 `word_track.whisper备份.json`** —— 它是「对齐已完成」的标记，删了会重跑声学对齐，两次对齐叠加会出问题。
- 契约验证命令模板（pwsh）：`[Console]::OutputEncoding=[Text.Encoding]::UTF8; $env:PYTHONUTF8="1"; python -X utf8 <Skill脚本目录>\<契约脚本>.py validate-keep <工作区路径> *>&1 | Out-File -Encoding utf8 运行记录\_输出.txt`（用 `E:/...` 正斜杠路径，跑完 Read 输出文件）。
