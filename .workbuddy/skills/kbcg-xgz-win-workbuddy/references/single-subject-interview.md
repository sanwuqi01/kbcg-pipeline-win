# 单学员好评采访工程契约

本文件只负责把 [`../子技能/粗剪/学员好评.md`](../子技能/粗剪/学员好评.md) 已完成的编辑判断落成可验证的工程记录。为什么选题、为什么这样开头和排序，由子技能决定；本文件不重新发明内容方向，也不负责字幕或视觉包装。

## 边界

只在一位学员是唯一内容主体、提问者只做引导，且成片不保留提问者原声时使用 `single_subject_interview`。双方平等对谈、多位受访者、必须保留主持人观点的素材不在此模式内。

C2864 是多人问答，不能作为本模式的同类型样本，也不能用来学习问句—回答的保留方式。开工仍须按 [`sample-evidence.md`](sample-evidence.md) 读取 2–3 份经哈希确认、真正符合本模式的成对样本；不足时停止并报告，不用 C2864 或单人口播样本补足。

## 说话人契约

在 `word_track.json` 生成后先写 `speaker_turns.json`，再对包含提问者和学员的完整词轴建立 `content_plan.json`，最后写 `keep.json`：

```json
{
  "mode": "remove_interviewer",
  "turns": [
    {"start": 0, "end": 8, "role": "interviewer", "reason": "开场提问"},
    {"start": 9, "end": 64, "role": "subject", "reason": "学员回答"}
  ]
}
```

硬规则：

- 从词 0 到末词必须无缝、无重叠、无遗漏地覆盖。
- `role` 只能是 `interviewer` 或 `subject`；不确定不得猜。
- 所有 `interviewer` 词必须全部被 `keep.drop` 覆盖。
- `content_plan.topic_blocks` 仍须从词 0 到末词无缝覆盖；提问者所在块必须标成 `decision: "exclude"`，学员块再按内容价值标成 `selected` / `candidate` / `exclude`。
- `subject` 只说明身份，不代表必须保留；学员的口吃、重复、弱信息和跑题仍按粗剪规则删。
- `content_plan.review.sample_pairs` 必须登记 2–3 份 `material_type: "single_subject_interview"` 的有效成对样本，不能混入单人口播或多人问答。
- `keep.review.scope` 必须包含 `content_plan` / `keep` / `structure` / `cut_review` / `speaker_turns`，不得包含 `cards` 代替粗剪听审。

## 将粗剪决策单落入内容地图

先确认用户要一条、多条还是完整一条，然后把提问者和每段学员回答都标成连续的 `content_plan.topic_blocks`。长素材必须先完成全量主题地图，不能看到一个亮点就开始顺剪。

### 采访专用内容契约

`content_plan.json` 额外必须有：

```json
{
  "output_request": {
    "mode": "multiple",
    "requested_count": 3,
    "output_index": 1,
    "focus": "AI 在企业中的应用落地",
    "source": "user"
  },
  "story_candidates": [
    {
      "candidate_id": "enterprise-ai-landing",
      "core_claim": "AI 要结合制度和执行力才能落地",
      "block_ids": ["B04", "B05", "B06"],
      "evidence_reason": "有门店、培训和管理案例支撑",
      "independence_reason": "可以从业务场景递进到组织判断",
      "selection": "selected"
    }
  ]
}
```

- `mode` 只能是 `single` / `multiple` / `complete`；多条成片每条建独立工作目录，`output_index` 记录当前是第几条。`multiple` 必须是用户授权，Agent 发现多少候选不能替代授权条数。
- `story_candidates` 要覆盖所有标成 `selected` 或 `candidate` 的可成片块，每个候选必须说清主张、证据和为什么能独立成片。
- 当前工作目录恰好一个 `story_candidate.selection=selected`，它的 `block_ids` 必须与 `topic_blocks.decision=selected` 全等。
- 其他可成片块保留为 `candidate` 索引，但在当前成片的 `keep.json` 中必须全部删掉。

多个 `story_candidates` 若引用同一内容块，必须在 `candidate_overlap_reviews` 中逐块登记候选 ID、各自功能、为什么不可替代，以及是否会导致两条证明任务重复；不能因为字段允许就无解释重叠。

### 多条组合计划

`mode: "multiple"` 时，当前工作目录还必须有 `portfolio_plan.json`。它是源素材级共同真值，先完成并审阅，再复制到每个输出工作目录；每个目录锁定同一份哈希。

第一次建立组合计划时，对 `config.original_path` 完整计算一次 SHA-256，并把结果同时写入 `config.source_sha256` 与组合计划；不能只复制文件名或手填一个看起来相同的哈希。大文件哈希只需在源素材身份未变时计算一次，后续由冻结配置核验。

```json
{
  "schema": "kbcg-xgz/interview_portfolio@1",
  "source_family_id": "源素材哈希前缀加稳定名称",
  "source_sha256": "64 位哈希",
  "word_track_sha256": "64 位哈希",
  "authorized_output_count": 4,
  "authorization_source": "user",
  "outputs": [
    {
      "output_id": "course-value",
      "output_index": 1,
      "candidate_id": "course-value",
      "audience_question": "课程真正好在哪里",
      "core_proof": "实操让学员从小问题看见系统问题",
      "course_value": "课程提供底层逻辑和真实问题求解",
      "primary_block_ids": ["B41", "B42"],
      "supporting_block_ids": [],
      "hook_promise": "本条开头向观众承诺什么",
      "body_payoff": "正文怎样兑现",
      "conclusion": "最终完成什么判断",
      "boundary": "本条明确不承担其他哪一层证明"
    }
  ],
  "cross_output_reuse": [],
  "review": {
    "status": "reviewed",
    "reviewer_type": "agent",
    "reviewed_by": "Codex",
    "reviewed_at": "带时区的 ISO 8601 时间"
  }
}
```

硬规则：

- `source_sha256` 必须匹配已核验并写入 `config.source_sha256` 的源视频摘要，`word_track_sha256` 必须匹配当前词轴。
- `outputs` 数量和编号必须恰好覆盖 `1..authorized_output_count`，ID 与候选 ID 都唯一。
- 当前 `output_index`、选中的 `candidate_id` 和 selected 内容块必须与组合计划对应条目一致。
- 同一块跨输出出现时，必须逐块写入 `cross_output_reuse`，列出每条中的不同功能、不可替代理由与删除影响；片内 Hook 复现仍由 `intentional_repeats` 单独登记。
- 组合计划锁定“这组片分别证明什么”，每条工作目录仍单独完成精删、重排、逐切口听审和冻结。

`selected_story` 必须恰好覆盖所有 selected 学员块，不能引用提问者的 exclude 块。记录原问题只为理解上下文，不把提问者文字写进 Hook 候选或主线。删掉问句后，每段学员回答必须能独立理解。优先保留能自带上下文的回答；若只剩“对”“是的”“这个”等依赖问句的残句，连同回答起始一起删除。只有高价值回答确实需要提问方向才能避免误解时，才在 `content_plan.question_context_bridges` 登记忠实的问题语境、关联的提问者块和回答块、必要性及不扩大范围的理由；它属于粗剪结构上下文，不是用字幕伪造学员结论。

这里的字段只记录子技能已经得出的内容方向。若工程记录与粗剪决策单不一致，应返回子技能重新确认内容，而不是在写 JSON 时顺手改变主题。

## 爆点前置与正文复现

爆点前置后，不能根据“重复就删”或“首尾呼应”作决定。若开头只承担结果预告，而它在正文还需恢复案例上下文、完成因果或承接后续结论，删掉会损害观众判断与理解时，允许在 `intentional_repeats` 登记并恰好复现一次。否则正文删掉该重复。

C2637 提供了“共同长初稿拆成四个独立证明任务 + 条件式 Hook 前置/正文复现 + 多子主题服务一条证据链”的开发证据，详见 [`../知识库/成对示例库/C2637_一条长采访拆成四条.md`](../知识库/成对示例库/C2637_一条长采访拆成四条.md)。它不是四份独立样本，不能提供通用补删率、固定条数或固定结构模板。其他素材若重排，仍必须保证身份、指代、时间和因果真实。

结构完成后运行 `p2b_生成切口清单.py`，在 `cut_review.json` 逐项听审删掉提问者、重排和复现形成的真实切口，确认尾音完整、学员词头完整、无提问残音且语义自然。通过 p1q、freeze、verify 后，本工程契约结束并把粗剪锁交回主 Skill。字幕属于下一阶段，不得借用已删除的问句，也不得回头偷偷改变粗剪内容。

`C2637` 已参与规则修正，本 Skill 以后对它的产物只能记为开发回放，不能宣称闭卷泛化验证。
