# Cache Optimization Notes / 缓存优化复盘

This document records MiniCodex2's DeepSeek prompt-cache investigation, the root cause, and the
current design rules for context construction.

本文档记录 MiniCodex2 针对 DeepSeek prompt cache 的排查过程、根因结论和后续上下文构造原则。

---

## 1. Problem / 问题

MiniCodex2 在 DeepSeek 上长期只有大约 `30%` 到 `45%` 的 cache hit rate，有时甚至跌到
`9%`、`12%`。

At the same time, Codex CLI connected through a DeepSeek-compatible proxy could reach `98%` to
`99%+` hit rate with similar or larger requests.

与此同时，Codex CLI 通过 DeepSeek-compatible proxy 请求时，即使上下文更大，也能达到
`98%` 到 `99%+` 的命中率。

This proved the issue was not simply:

这说明问题不只是：

- DeepSeek cache 不可靠；
- token 太多；
- 模型太弱；
- 网络或价格策略问题。

The likely issue was MiniCodex2's request/context layout.

更可能的问题是 MiniCodex2 构造请求上下文的方式破坏了 provider 侧的 prefix cache。

---

## 2. Important Cache Principle / 关键缓存原则

Provider-side prompt cache is prefix-oriented in practice.

实践中，provider 侧 prompt cache 主要依赖稳定前缀。

```text
request 1: [A][B][C][D]
request 2: [A][B][C][D][E]       -> A-D can be cached
request 3: [A][B][X][D][E][F]    -> cache breaks around X
```

The important point is not only "same information", but "same serialized prefix".

关键不是“语义上差不多”，而是“序列化后的前缀是否稳定一致”。

---

## 3. Hypotheses Tested / 排查过的假设

During the investigation we tested several hypotheses:

排查过程中测试过这些假设：

- `trigger_auto_compact_limit_tokens` 太小，导致频繁压缩。
- compact summary 太短，导致模型反复读文件。
- 中文 token 估算偏差太大。
- `urllib` vs `requests.Session` 导致连接复用不同。
- tool schema 或 payload JSON 字段顺序破坏缓存。
- tool output 太大，导致新增 miss token 占比高。
- runtime facts、evidence、work memory、execution state 等动态 section 被插入到缓存前缀中。
- midstream `system` / `runtime` messages 破坏稳定前缀。

Some of these were partial factors. The main architectural cause was unstable midstream runtime
injection.

其中一部分是局部因素，但主因是动态 runtime/control 信息进入了主消息前缀。

---

## 4. Experiments / 实验过程

### 4.1 Append-only Replay

We built simple replay tests that only append data to the end of a stable payload.

我们做了只追加数据的 replay 测试：

```text
base payload
base payload + 2K
base payload + 5K
base payload + 8K
...
```

Result:

结果：

```text
DeepSeek cache hit: 90% - 99%+
```

This proved that DeepSeek can cache long stable prefixes very well.

这证明 DeepSeek 对稳定长前缀的缓存能力是正常的。

### 4.2 Real MiniCodex2 TUI Loop

In the real TUI loop, MiniCodex2 read files, ran tools, updated work plans, recorded evidence, and
rebuilt runtime context.

真实 TUI 循环中，MiniCodex2 会读文件、运行工具、更新 work plan、记录 evidence，并重建 runtime context。

Result:

结果：

```text
DeepSeek cache hit: often 30% - 45%, sometimes lower
```

This showed that something in the real context construction was unstable.

这说明真实上下文构造中存在不稳定因素。

### 4.3 Payload Snapshot Comparison

We saved model-bound message/payload snapshots and compared adjacent requests.

我们保存每次发送给模型前的消息/payload 快照，并比较相邻请求。

The important observation:

重要发现：

- A normal append-only transcript should keep earlier bytes stable.
- MiniCodex2 inserted changing runtime/system blocks into the active stream.
- Those blocks included tool facts, verification hints, evidence summaries, work memory, and execution state.

- 正常 append-only transcript 应该让旧字节保持稳定。
- MiniCodex2 会把变化的 runtime/system block 插入 active stream。
- 这些 block 包括 tool facts、verification hints、evidence summaries、work memory、execution state。

### 4.4 Removing Midstream Runtime/System Blocks

When we temporarily removed midstream runtime/system injections, cache hit jumped near `98%`.

临时移除中途 runtime/system 注入后，cache hit 立刻接近 `98%`。

However, this also broke agent behavior: the model stopped seeing important tool failures,
verification requirements, and recovery guidance.

但这也破坏了 agent 行为：模型看不到关键工具失败、验证要求和恢复提示。

---

## 5. Root Cause / 根因

The root cause was not simply "too many tokens".

根因不是简单的“token 太多”。

The root cause was:

真正根因是：

```text
MiniCodex2 rebuilt and inserted dynamic runtime state into model-visible context.
Provider cache expects a stable repeated prefix.
Dynamic midstream system/runtime messages made the prefix unstable.
```

More concretely:

更具体地说：

- Stable root protocol can cache.
- Append-only chat/tool history can cache.
- But a rebuilt runtime dashboard in the middle of the transcript breaks cache.
- Even if the content is useful, its delivery channel matters.

- 稳定 root protocol 可以缓存。
- append-only 聊天和工具历史可以缓存。
- 但每轮重建的 runtime dashboard 如果插入 transcript 中间，会破坏缓存。
- 即使内容对模型有用，传递方式也很关键。

---

## 6. Current Direction / 当前修复方向

MiniCodex2 should use this layout:

MiniCodex2 应采用这样的上下文布局：

```text
stable root system protocol
-> append-only user / assistant / tool transcript
-> append-only synthetic runtime observations
-> queryable durable memory / evidence / work plan tools
```

Rules:

规则：

1. Keep the first system/root protocol stable.
   保持第一段 system/root protocol 稳定。

2. Main context should behave like an append-only transcript.
   主上下文应尽量像 append-only transcript。

3. Do not rewrite old messages to make current state look cleaner.
   不要为了让当前状态更整齐而重写旧消息。

4. Dynamic facts should be appended as observations or exposed through tools.
   动态事实应作为 observation 追加，或通过工具查询。

5. Midstream runtime/system control messages should not be injected into the active prefix.
   不要把中途 runtime/system 控制消息插入 active prefix。

6. Cache hit rate is a runtime architecture health metric.
   缓存命中率是 runtime 架构健康度指标。

---

## 7. Synthetic Runtime Observation / 合成 Runtime 观察

To preserve model-visible facts without breaking the system prefix, MiniCodex2 should convert later
runtime observations into synthetic user messages:

为了既保留模型可见事实，又不破坏 system prefix，MiniCodex2 应把后续 runtime observations 转成
synthetic user messages：

```text
[RUNTIME OBSERVATION - synthetic, not human input]
The previous tool batch completed.
- tool=read_file; status=ok; path=...
- tool=run_command; status=failed; ...
```

This is not a real human message. It is a runtime observation delivered through a safer append-only
channel.

这不是真实用户消息，而是 runtime observation 通过更安全的 append-only 通道传递给模型。

---

## 8. Context Buffer Repair Bug / Context Buffer 修复事故

During cache work, another bug appeared: the model repeatedly saw a fake `Recovered context buffer`
tool result.

在缓存排查中还发现另一个 bug：模型反复看到假的 `Recovered context buffer` 工具结果。

Cause:

原因：

```text
ContextBufferStore.append() repaired tool-call sequences too early.
It ran after assistant tool_calls were appended but before real tool results arrived.
```

This created fake tool results. Later, when real tool results came in, the message history was already
corrupted.

它在 assistant tool_calls 刚写入、真实 tool results 还没回来时就提前 repair，导致生成假的 tool result。
后续真实工具结果再进入时，消息历史已经被污染。

Fix rule:

修复规则：

```text
Do not repair incomplete tool-call batches while tools are still running.
Repair only at send boundary, right before constructing the next model request.
```

也就是：context buffer 可以 append，但不要在工具批次未完成时自作聪明修复。

---

## 9. Compacting Strategy / 压缩策略

Compaction is still necessary, but it should not be used to compensate for unstable context layout.

压缩仍然必要，但不能用压缩掩盖上下文布局不稳定的问题。

Recommended direction:

推荐方向：

- Use provider-reported `prompt_tokens`, `cached_tokens`, and output tokens for measurement.
- Keep root protocol and stable memory first.
- Preserve recent user intent, explicit credentials/accounts supplied by user, current task, changed files, errors, and next step.
- Summarize old raw tool outputs after the model has already consumed them.
- Keep durable state in structured stores, not only in compacted prose.

- 使用 provider 返回的 `prompt_tokens`、`cached_tokens` 和 output tokens 作为真实统计。
- 保持 root protocol 和稳定 memory 在前。
- 保留近期用户意图、用户明确提供的账号/凭据、当前任务、改动文件、错误和下一步。
- 模型已经消费过的旧工具原文可以压缩成摘要。
- 持久状态应存在结构化 store 中，而不只依赖压缩文本。

---

## 10. What Not To Do / 不应做的事

Do not:

不要：

- Put every runtime fact into the first system message.
- Rebuild a large current-state dashboard before every model call.
- Insert changing runtime/system blocks in the middle of the transcript.
- Delete runtime facts entirely just to improve cache.
- Trust token estimates without provider usage data.
- Repair tool-call history while a tool batch is incomplete.

- 把所有 runtime fact 塞进第一条 system。
- 每次模型调用前重建一个大型 current-state dashboard。
- 在 transcript 中间插入不断变化的 runtime/system block。
- 为了缓存直接删掉 runtime facts。
- 只相信本地 token 估算，不看 provider usage。
- 在工具批次未完成时修复 tool-call history。

---

## 11. Human + AI Debugging Reflection / 人机协作复盘

This bug was not solved because the AI already knew the answer.

这次问题不是“AI 一开始就知道答案”。它是通过真实运行、真实日志、真实花费和大量反复实验逐步逼近的。

The model had broad prior knowledge. It could propose hypotheses, inspect code, write patches,
design replay scripts, and summarize evidence. But the key direction came from the user's repeated
first-principles questioning:

模型有很强的先验知识：它能提出假设、读代码、写补丁、做 replay 脚本、总结证据。但关键方向来自用户不断用第一性原理追问：

- 如果普通 append-only 聊天可以高命中，为什么 MiniCodex2 不行？
- 如果动态 section 放后面，为什么还是不稳？
- 中途插入 `system/runtime` 是否会破坏缓存？
- 为什么 Codex CLI + DeepSeek 可以 99%，MiniCodex2 不行？
- 是不是应该把消息当成稳定 buffer，而不是每次重新 build 一个看起来相似的结构？

These questions forced the investigation away from vague optimization and toward reproducible
payload-level experiments.

这些问题把排查从“泛泛优化”逼到了“payload 级可复现实验”。

The lesson is not that AI agents are useless. The lesson is that intelligence still needs
instantiation:

这不是说 AI agent 不行，而是说明智能仍然需要实例化运行：

```text
model prior knowledge
+ runtime body
+ logs and tools
+ persistent memory
+ real experiments
+ user critique
= practical engineering discovery
```

AI has compressed knowledge of many rules, but real systems unfold in concrete situations. The
truth of a runtime bug often exists only after the system is run, measured, contradicted, and
re-tested.

AI 记住了大量抽象规律，但真实系统是在具体场景中展开的。runtime bug 的真相，往往只有在运行、测量、反驳、再测试之后才出现。

If another independent agent instance had been asked to audit the same evidence, it might also have
found the answer eventually. Multiple instantiated agents with different hypotheses, shared logs,
and independent critiques may become an important debugging pattern:

如果有另一个独立 agent 实例一起审查同一批证据，它也许最终也能发现答案。多个实例化 agent 以不同假设、共享日志、互相质疑的方式协作，可能会成为一种重要调试模式：

```text
one agent generates hypotheses
one agent preserves skepticism
one agent runs experiments
one agent audits cache/payload diffs
human anchors the goal and reality
```

This supports MiniCodex2's larger direction: an agent is not just "model + file tools"; it is an
engineering system made of model, runtime, memory, logs, feedback, experiments, and human critique.

这也印证了 MiniCodex2 的长期方向：agent 不是“模型 + 文件读写工具”，而是模型、runtime、记忆、日志、反馈、实验和人类质疑共同组成的工程系统。

---

## 12. Checklist / 后续检查清单

- [ ] Keep root protocol stable.
- [ ] Keep main model-visible transcript append-only.
- [ ] Persist context buffer snapshots for debugging.
- [ ] Convert later runtime controls to synthetic user observations or queryable tools.
- [ ] Keep provider usage logging: prompt, cached, output, hit rate.
- [ ] Do not repair tool-call sequence until send boundary.
- [ ] Add regression tests for cache-friendly context ordering.
- [ ] Add regression tests for incomplete tool-call batch append.
- [ ] Document provider differences: OpenAI, DeepSeek, and compatible proxies.



---

## ??????????? system ?????2026-06-21?

### ??

?????? TOOL RESULT FACTS ???????????????**??**??? `runtime` ??????? `to_model_dict` ??? `role: system`?DeepSeek ? token ????system ????????? token ??????????? system ??????? token ???????????

????584 ????184K tokens??

```
prefix match: 739,013/742,499 bytes = 99.5%
cache hit:    183,296/184,232 tokens = 99.5%
same messages: 582/584 (? 2 ????)
```

**????? = ???????????**

### ??

????????????????? `system` ???`user/assistant/tool` ??????????????????? `system`?runtime?system????? Codex CLI ????????????? system ???

---

## 九、最终修复：移除中间 system 角色注入（2026-06-21）

### 发现

之前只定位到 TOOL RESULT FACTS 注入打断缓存，但更深层的问题是**角色**。所有  + "untime" + @ 角色的消息经过  + "	o_model_dict" + @ 被转为  + "ole: system" + @。DeepSeek 的 token 序列中，system 消息之间可能有特殊 token 分隔符，在对话中间插入 system 消息会改变整个 token 序列，导致缓存链断裂。

修复后（584 条消息，184K tokens）：

 + "" + @"" + @
prefix match: 739,013/742,499 bytes = 99.5%
cache hit:    183,296/184,232 tokens = 99.5%
same messages: 582/584 (仅 2 条新消息)
 + "" + @"" + @

字节匹配率 = 缓存命中率，完美对齐。

### 结论

不是不能中间注入内容——是不能注入  + "system" + @ 角色。
 + "user/assistant/tool" + @ 角色的消息在中间追加不会打断缓存链，
但  + "system" + @（runtime -> system）会。
