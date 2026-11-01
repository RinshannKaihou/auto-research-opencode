# auto-research-opencode · 自主研究插件（OpenCode V2）

发布版本 v0.1.1（实验版本）；当前源码另含未发布的 R1／R2、运行控制修复和 E1／E2。安装、更新、卸载和常见问题见[仓库根目录的 README](../README.md)；这份文档讲当前插件怎么工作。

这是面向 OpenCode V2（宿主最低 2.2.5，`@opencode/plugin` SDK 2.0.18）的研究插件。**它会自动推进研究**：你给出研究目标后，插件驱动模型自己完成规划、派发、执行、综合和收尾。你可以随时插话指导，也可以暂停或停止。

研究记录由仓库根目录 `src/auto_research/` 下的 Python 代码管理，保存在 `<项目>/.research/`。当前为 schema 14，首次可写打开会逐级备份并升级旧库，包括研究会话的状态查询或恢复；独立只读浏览支持 schema 9–14 且不升级。旧 todo 保留并映射为 unknown 候选，不自动生成已完成义务；迁移也不会把旧节点或历史审阅伪造成探索和新式覆盖。不支持 schema 14 的旧版不能直接写入升级后的库；回退代码不降级数据库，恢复备份只能在保留当前完整数据之后的副本中进行。

## 探索与证据结项（E1／E2）

两项能力默认关闭，只有显式采用后才改变后续工作。想从项目开始就使用时，在研究主会话依次输入：

```text
/research init <研究目标>
/research exploration on
/research auto
```

`exploration on` 同时采用 R1 上下文、`exploration-v1` 和 `evidence-v1` 结项契约，命令返回实际版本。启用后，主协调可以围绕关键未知登记范围和候选路线，选择一个有限探针，结合结果继续、转向、暂缓或提出停止；保存候选本身不会创建执行节点。单路径交付不必为了启用该能力而凑路线。选择工作仍沿用既有节点、执行授权和预算；重启后项目先暂停，显式恢复时才检查并继续尚未启动的意图。

`/research exploration` 查看当前设置，`/research exploration off` 停止新的探索编排，保留已登记的路线、结果与风险，已经采用的结项契约继续生效。需要 E2 而不需要探索时，可只运行 `/research conclusion-contract evidence-v1`；省略参数可查看当前契约。已结项项目的历史报告不会重算，要继续研究须先启动新 generation。已有 R1 上下文和 Legacy 上下文仍可使用；开启探索时须使用 R1。

在 `evidence-v1` 下，主协调读取固定版本及当前依据，准备结项草案，完整读取草案，再发布关联该草案的最终报告。发布时插件从同一草案生成并冻结可读的证据附录；结项分别记录回答程度、证据闭合、审阅覆盖和停止理由。核心依据或相关风险未知时可以保存 partial／unresolved，不能标成 answered；有依据的非核心缺口可以形成带限定的回答。审阅类型、实际观测到的模型和执行事实分别保存；审阅覆盖不等于科学结论已经证明。材料交付给模型也不证明模型理解了内容。

看板仍只有概览、研究、成果三个主入口；进入“2 研究”后按 `b` 在节点与路线之间切换。概览显示当前选择和结项四维状态，最终材料包含可打开的证据附录。真实模型的质量与成本对照尚未完成，因此新能力保持显式启用。

## Legacy 结项要求

主协调发布 `status: "complete"` 的最终报告后调用：

在尚未采用 `evidence-v1` 的项目中，以下字段从 v0.1.1 起全部必填，旧调用需要更新。

```json
{
  "summary": "完成受控实验，长期行为仍需研究",
  "final_ref": "pub/P-014#report",
  "outcome": "partial",
  "gaps": ["尚无长期训练证据"],
  "review": {
    "status": "unreviewed",
    "refs": [],
    "limitations": ["最终综合尚未独立审阅"]
  }
}
```

- `outcome` 为 `answered`、`partial` 或 `unresolved`；后两种必须有非空 `gaps`。空列表表示未登记未解决项。
- `review.status` 为 `unreviewed`、`partial` 或 `reviewed`；后两种必须通过 `refs` 引用已保存固定版本的审阅材料，前两种必须有非空 `limitations`。三个字段均必填。它是调用者的审阅范围说明，`reviewed` 不表示审阅通过。
- `final_ref` 支持整组已发布材料的引用，也支持包含条目编号的完整引用。该组材料的状态必须为 `complete`；引用整组材料时，材料组须非空且所有条目可解析。引用的文件须通过完整性校验。`complete` 仅表示报告交付完成，允许报告未解决问题。
- 引用、结项要求、调用身份及尚未结束的任务检查，与保存结项记录、结束主会话本次执行、更新运行状态在同一事务中完成。相同操作重试时返回原有结项记录；通知失败不撤销结项。不能用普通工作流状态更新跳过结项检查。
- 历史 pending 待审项、open 问题不会一律阻止结项。这些结项要求尚不包含完整的科学审阅，也不会检查是否已逐条审阅所有研究结论。
- 状态和概览显示登记的最终引用、结果及审阅说明；后续发布的材料不会替换最终引用。历史 `complete` 项目无结项记录时显示“结项记录：未记录；审阅情况未知”。重新开展研究使用 `/research auto [补充指令]` 创建新运行 generation。

`research_checkpoint` 默认写入真实当前工作对象。插件仅从本次请求已收到的完整 context／query／上次写入结果登记基线，再由后端原子比较并追加；后台缓存和 execute.after 不代表模型已经收到结果。原始 head 与可见 checkpoint 分开保存，来源未知的历史项目 checkpoint 仍被隔离。无可信基线时先查询 `research_query(collection="checkpoint")`，在后续请求中使用返回的 base；冲突后重新读、重新综合，不盲目重试旧 state。见下文 R2 写入契约。

## 一句话开始

在想放研究项目的目录里启动 OpenCode，然后在首页输入：

```text
/research auto <研究目标>
```

之后不需要再发任何消息：

1. **主协调会话**提出研究节点（`research_propose`），把节点派发出去（`research_dispatch`），然后等待（`research_wait`）。
2. 插件为每个派发的节点**新建一个执行会话**，标题形如"X-001 · 问题"，并给它一个独立的工作目录 `workspaces/branch-*/`。
3. 节点执行会话做实验，发布报告和数据（`research_publish`），记录引用这些材料的结论（`research_record`）；需要独立复核时可以叫一位只读专家（`research_delegate`，见下文"节点内专家"）；完成或受阻时调用 `research_finish`。
4. 插件把节点结果交给主协调，唤醒它继续：综合结果，派发后续节点。
5. 目标回答后，主协调发布最终报告并调用 `research_conclude`，研究结束。

进度会显示在以下位置：
- 右侧栏研究面板；
- 输入框上方的状态条；
- 对话区的 `【Research 自动推进】…` 记录；
- `/research board` 看板。

在看板"运行"视图里选中一个会话按 Enter，就会跳到对应的节点执行会话。

## 推进引擎怎么工作

OpenCode 每结束一轮都会发出一个事件，引擎读取研究记录后决定下一步：

| 情况 | 处理 |
|---|---|
| 主协调这一轮有产出 | 插入一条"继续推进"消息，开始下一轮 |
| 主协调在等待，或者有节点在跑而它这一轮没有产出 | 进入等待，节点结束后再唤醒 |
| 节点调用了 `research_finish` | 结束节点本次执行，把总结和已发布材料交给主协调，调度下一个排队的节点 |
| 节点这一轮有产出但还没结束 | 让它继续 |
| 同一会话连续 3 轮没有有意义的研究进展 | 暂停这个会话；note、checkpoint、重复处置、重放和纯文案修订不重置计数 |
| 一轮出错 | 自动重试一次，再出错就暂停这个会话 |
| 你按 Esc 中断一轮 | 暂停这个会话 |
| 插件在同一宿主进程内重绑定 | 先核对已有启动、执行和结果；不能确认的执行保持未核实，不伪装成冷启动 |
| OpenCode 进程真正重启 | 运行中的项目改为暂停；不能确认退出的任务和专家保持未核实，不自动重跑 |

节点的结束请求只对当前执行轮有效；该轮失败或中断会取消待提交请求，后续获授权的执行轮可再次结束同一节点。重绑定依据当前轮的消息身份核对，不会拿原始启动消息误判第二轮或已暂停的节点。

事件存储失败最多重试三次，之后仅阻止相关会话的新工作。事件流断开或缓冲溢出时，暂停、停止和只读查询仍可用；`/research resume` 会先暂停调度、重新订阅并核对现有执行，再恢复可继续的会话。无法对应到当前执行轮的结果仍需核对。

事件暂存或连接异常会在 `/research status` 和侧栏显示恢复提示；未知归属的事件有界保留，先核对归属再处理。续轮确定尚未调用宿主时，只暂停续轮并退还该次预留，不要求核对原任务退出；宿主结果未知时仍保留未核实。任务连续三次未能提交会暂停调度，排障后再显式恢复，避免重复刷启动记录。

另外几条规则：
- 节点"发布了新材料"只作为消息送达主协调，不会唤醒它；只有节点完成、受阻或失败时才唤醒。
- 在研究会话里直接发的消息会被记为"用户指导"写进研究记录；普通消息不会改变运行或暂停状态。结项后、或 `/research init` 之后还没开始时，消息不会写进研究记录；要让讨论的结论进入新一轮，用 `/research auto <补充指令>`。
- 并行节点数默认是 1，可以用插件选项 `concurrency` 调整。
- 研究会话里没有 OpenCode 自带的 `subagent` 和 `question` 工具：后台子代理不在推进引擎的视线里，提问会让自动推进停下来等人回答。节点需要帮手时用 `research_delegate`。

## 节点内专家

节点执行会话需要另一个视角时（复核一段分析、核对结果、查阅相关材料），可以调用 `research_delegate` 叫一位**只读专家**，或者用 `research_delegate_batch` 一次叫一批同时运行。

- **怎么运行**：插件新建一个会话，标题形如"X-001 · 专家 · 标签"，发给它一张固定格式的任务单（问题、目的、材料、交付物、完成标准、回报要求）。节点的这次工具调用会一直等到专家交回报告。
- **报告去哪**：完整报告存进研究记录；节点拿到约 1600 字的预览和完整报告的文件路径，自己判断采不采用。
- **只读有两道保证**：专家会话建立时带上会话级权限规则（先全部拒绝，再放行 `read`、`glob`、`grep`、`research_query`、`research_read_input`，`.env` 文件不放行）；每次请求再按清单删掉其他工具。研究记录服务端也拒绝专家的一切写入。专家不能运行命令、改文件，也不能再叫别人。
- **盲评**：任务里写 `context_mode: "blind"` 时，专家只看得到分配的材料，编号为 `input-1`、`input-2`……，用 `research_read_input` 分段读取。它看不到材料来自哪份发布，看不到节点工作目录、研究记录和项目目标。OpenCode 默认塞进系统提示词的工作目录和 `AGENTS.md` 也会被替换掉。盲评材料必须是已发布的文件（`pub/P-…#item`）。
- **限制**：每个节点同时最多 2 位专家（插件选项 `specialistFanout`），一批超过上限时一个都不启动；每位专家最多 10 分钟（插件选项 `specialistTimeoutMs`），超时后收回已写的部分，记为"未完成"。
- **中断与重启**：节点委派调用被中断时会请求中断专家，并按能否确认退出记录结果。同进程重绑定恢复原任务的工具范围和截止时间；真正重启时仍在执行或无法核对的专家标为未核实，不自动重派，也不假定外部进程已退出。
- **还没有做**：DSH 版里主协调发起的项目级"整理专家"（`research_memory` 的 `consolidate`）。

## 命令

| 命令 | 作用 |
|---|---|
| `/research auto [目标或补充指令]` | 目录里没有项目时，建立项目并开始（必须写目标）；已有项目时，开始或重新开始，后面的文字作为补充指令记为用户指导并写进启动消息，项目目标不变（研究在运行或暂停、主会话这一轮还没结束、或超过 2000 字时不接受补充指令，会提示）。在首页执行会自动新建或打开主会话 |
| `/research pause` | 停止新的启动授权，已授权的当前轮可收尾；不再续轮 |
| `/research resume` | 恢复可继续的会话；未核实的执行不会自动重跑 |
| `/research stop` | 请求中断运行中的会话，取消排队任务；退出未核实的执行保留待核对（需要确认） |
| `/research status` | 查看项目和本会话的状态 |
| `/research recover controller <原因>` | 核对身份不明的旧控制器后接管；项目保持暂停（需要确认） |
| `/research recover task <任务 ID> <原因>` | 核实节点执行及其外部进程已退出，登记核对结果（需要确认） |
| `/research recover specialist <任务 ID> <原因>` | 核实专家执行及其外部进程已退出，登记核对结果（需要确认） |
| `/research recover delivery <report\|wake> <通知 ID> <delivered\|not-sent> <原因>` | 核对报告或唤醒消息是否被宿主接受，登记结果（需要确认） |
| `/research retry <任务 ID> <原因>` | 对已核实退出的节点创建新执行（需要确认） |
| `/research budget` | 查看持久限额、使用量和覆盖范围 |
| `/research budget <turns\|nodes\|seconds\|tokens\|cost> <正整数\|off> <原因>` | 主会话显式修改限额，off 表示未启用；resume 不清账 |
| `/research board` | 打开只读看板：1 概览、2 研究、3 成果；v 切换所属内容 |
| `/research context-policy [legacy\|r1]` | 主会话查看／切换上下文策略；后续模型请求生效，不重开项目 |
| `/research init <目标>` | 只建立项目、不开始，适合先和模型讨论方案 |
| `/research guidance [路径 [--version v]]` | 查看或登记科研指导文档 |
| `/research takeover` | 接管目录里已有的项目，包括 DSH 建的。这是**单向**操作，需要确认，而且项目必须已停止 |
| `/research detach` | 把本会话移出项目。主会话要先停止研究才能移出 |

模型能用的研究工具按角色开放：

| 角色 | 专属工具 |
|---|---|
| 主协调 | `propose`、`dispatch`、`wait`、`conclude`、`close_node` |
| 节点执行 | `finish`、`consume`、`delegate`、`delegate_batch` |
| 两者共用 | `query`、`record`、`revise`、`checkpoint`、`dispose`、`narrow_scope`、`note`、`snapshot`、`publish`、`relate` |
| 专家 | `query`、`read_input`（盲评只有 `read_input`），外加 OpenCode 自带的 `read`、`glob`、`grep` |

## 运行控制与恢复

主协调负责规划、派发和综合；原生工具只开放 `read`、`glob`、`grep`，运行命令和修改文件由节点执行会话负责。插件同时过滤请求中的工具，并在工具实际执行前检查角色，旧请求或其他插件重新加入的工具也要经过检查。

该限制跟随研究角色，在 `init` 后的 manual、暂停、停止、结项状态下都有效；改变运行状态不会把主协调变成普通执行会话。`websearch`、`webfetch`、todo 和未明确分类的 MCP 工具也不在主协调原生工具清单内。文献查找及数据采集由节点承担，主协调使用已有材料和研究记录进行规划与综合。需要普通工具时，先停止项目并 `/research detach` 移出主会话，或新建未关联研究的会话。未关联会话的原生工具不受该角色限制。

控制器交接或核对期间，`research_query` 和专家的 `research_read_input` 仍可读取已有信息；后者保留角色、指定输入及服务端 lineage 校验。角色未登记或不可识别时只开放已有材料的原生读取，不把未知角色当成节点执行授权。

软暂停阻止新任务和新一轮授权。暂停前已获授权、且能与当前节点执行及原消息精确关联的轮次可以完成当前工作并调用 `research_finish`；暂停后新插入的消息不能继承该授权。已完成节点会结算，但暂停期间不自动唤醒主协调或开始下一轮。`/research resume` 是显式恢复操作，不清除预算或把未知执行认定为已退出。

启动和消息交付持久记录稳定身份。宿主响应丢失时，未知不等于失败或未发送；系统保留未核实状态，不盲目重派或重复投递。状态会显示待核对的通知，节点和专家状态可在看板查看。同一进程重绑定先核对原执行；进程身份不明时控制器要求人工核对。真正冷启动会暂停运行中的项目，保留未知执行。

如果错误发生在调用宿主 API 之前，控制器会按原启动阶段和当前任务身份撤回本次提交：节点回到原任务队列，未启动的专家记为未完成，未发送的报告／唤醒允许重试。只有明确未发送的轮次预留才退还；已发送或响应丢失的请求仍保留核对入口。

控制器会尽可能记录本机进程启动身份，以识别 PID 复用。不同 epoch 或同一 PID 本身不足以证明旧进程退出；旧控制器可能存活或不可探测时，新实例只登记接管候选，不自动替换现有 owner。用户核对后可用 `recover controller` 显式接管；如果 owner 已再次变化，旧接管请求会被拒绝。Linux／macOS 支持本机启动身份探测，其他平台保留未知状态，不用会终止进程的探测方式。

`control_revision` 是状态版本和诊断信息，不是一次性执行许可。实际写入在事务内检查当前运行状态、owner fence、generation 与执行轮／消息绑定；软暂停保留已获授权的当前轮收尾，因此不把每次 revision 变化一律视为撤销。

确认恢复前应独立检查旧宿主、会话和可能存活的外部进程。`/research recover task` 与 `/research recover specialist` 的确认表示用户已经核实相应执行及外部进程退出；它们本身不启动实验。确需重新执行节点时，再使用 `/research retry`。对于投递未知，核对宿主消息后用 `recover delivery` 记录 `delivered` 或 `not-sent`；后者允许使用原消息 ID 和原内容重试。控制器身份不明时使用 `recover controller`，接管后项目仍暂停，未知任务仍须逐项核对。上述动作均要求原因及界面确认，不能由模型自行代替用户核实。

## R2 写入与审阅

主协调和节点共享五个窄写工具，模型上下文不再列出 `research_memory`。旧接口保留给历史调用，仍走同一权限与领域事务；首次无基线 checkpoint 会被拒绝，旧成功请求按原 ID／payload 重放。

| 工具 | 最小意图与边界 |
|---|---|
| `research_record` | 知识种类和 statement；证据、条件及科学关系由模型明确声明 |
| `research_revise` | 固定 ref、changes、reason、preset；editorial 只改文案，correct_previous 只影响该输入版本，custom 明确填写 change_kind／affected_scope_mode／affected_scope |
| `research_checkpoint` | 通常只填 state；当前请求无已交付基线时先读 checkpoint，再据返回值重新组织 state |
| `research_dispose` | 指定 change_id／affected_version 的科学处置，不代替审阅报告 |
| `research_narrow_scope` | 明确缩小未知影响范围，不表示风险解决 |

例如仅改措辞、未增加证据且保留执行来源：

```json
{
  "ref": "knowledge/K-001@2",
  "changes": {"statement": "在已测条件下观察到改善"},
  "reason": "限定表述",
  "preset": "editorial",
  "preserve_sources": true,
  "no_new_evidence": true
}
```

两个来源标志必须明确提供；一旦改变证据、支撑或 checks，不能借此保留旧验证结果。旧空来源仍为空、unlinked 仍为 unlinked，不从 author 推定执行来源。record／revise 返回实际持久化 field_checks。

checkpoint 的 base 绑定 host、session、association、attempt、owner、generation 和插件实例。`checkpoint_base_required` 表示需要重新读取；`checkpoint_conflict` 表示当前原始 head 已变，应重新读并综合。响应提供 next_base，但只有该结果进入后续模型请求，才成为自动写入基线。相同 operation ID 仍返回原响应；不能改 payload 后沿用该 ID。连接旧 Python 包时明确报契约不兼容，更新代码后先执行 `bun run sync:python`。

发布、结束、修订和 correction note 只生成候选。活跃节点的输入、真实支撑、依赖和 consume 派生当前审阅范围；主协调直接调用 `research_review_scope(refs, reason)` 声明综合采用范围，不依赖 checkpoint。checkpoint 的 `decision_refs`／`scope_reason` 兼容入口仍保留：省略表示保留，空列表表示明确清空。后续查询传入的 decision_refs 只影响该次只读查看。

`research_query` 的 `review_candidates`／`review_obligations`／`adoptions` 支持分页。义务按 generation、owner、精确目标版本及 change_id 区分，使用 `research_relate` 链接报告：

```json
{
  "source_ref": "pub/P-009#review",
  "target_ref": "knowledge/K-001@2",
  "label": "reviews",
  "note": "已检查这一固定版本，发现反例",
  "obligation_id": "<query 返回的真实 obligation_id>",
  "result": "challenges",
  "complete": true
}
```

报告必须独立、非空、可解析，目标与义务精确相符。完整 supports／challenges／inconclusive 都可完成任务，覆盖保持“尚未评估”；challenges 继续显示在当前风险中，风险处置另行登记。离开范围标为 out_of_scope，保留历史和风险；宿主可审计地 waive、reopen、accept_previous，模型没有这些管理入口。重开之后旧报告不能自行再次关单。

完整和部分报告中的 challenge 均独立保留，节点结束、停止／结项、范围退出或新 generation 不会清除。处理某个 challenge 使用 `research_dispose`，提供风险条目返回的 `review_event`、精确 `affected_version`、`disposition_kind`、reason 和 evidence_refs；不要同时提供 change_id。`unresolved` 保留风险，其余处置必须有证据，`revised` 还须提供不同的 replacement_ref。该记录只表明显式处理了这一挑战，不证明科学覆盖通过；历史报告与处置仍可查询。普通知识影响仍按 change_id／affected_version 处置。

同一快照在一个插件实例内复用回执，query 回执按工具调用稳定去重。上下文与回执登记共享 2 秒等待期限，迟到回执只在后续完整交付同一快照时可用，不会回填旧请求。Legacy／R1 的完整 checkpoint 块均最多 4,000 字符；超过上限或剩余上下文预算时整个省略，要求显式读，不为截断 state 签发 base，也不让长 state 挤占科研依据。

typed relate 的知识关系追加新 revision，须明确 revision 的变更／范围字段；`grounded_in` 是支撑，`motivated_by` 不传播失效。节点关系以 source 为后继，target 为前驱节点或其发布材料；depends_on 明确指定采用材料，不允许循环或修改已派发节点。提案输入、原材料 pin 和历史知识版本不被改写。引用可派生采用，同 attempt／ref／relation／用途的显式 consume 去重，不同用途仍保留。

## 自主调度预算与观测

先 `/research init <目标>`，按需配置预算，再开始研究。例如：

```text
/research budget turns 20 本次试验上限
/research budget nodes 8 本次试验上限
/research budget seconds 1800 本次试验上限
```

未配置的维度显示未配置；没有隐藏默认科研预算。turns 按同一 generation／工作 owner 的受管自动请求预留，含节点与专家 kickoff，只有确定尚未提交宿主的预留可退还，交付未核实的预留不退还；nodes 限制项目累计节点数；seconds 是包含暂停和重启时间的持久截止时间。resume 不清零，提额须显式给原因。到时取消排队任务并请求中断；原生轮次结束不能证明外部进程退出，不能据此清除执行核对要求。

停止／结项之后 `/research auto` 创建新 generation，开始新的轮次／时限统计；项目累计 nodes 不清零。预算暂停状态不能用 auto 绕过，仍须显式改额后 resume。cost 接受正小数，例如 `/research budget cost 0.25 本次上限`。

节点在受管轮次前后若源码／实验配置的实际内容改变，可计作开发推进；仅调用 shell、touch、note、checkpoint 或改 Markdown 文书不计。检测由宿主 worker 执行，Python 后端不遍历／读取工作区，RPC 重放不触发扫描。只检测节点 cwd 下代码及 yaml／toml，排除隐藏目录、inputs、node_modules、venv、workspaces、缓存、site-packages／dist-packages、符号链接，以及带 pyvenv.cfg／conda-meta 的环境目录。

探测归属读取与 worker 共享 500ms 等待预算，每次最多遍历 10,000 个目录项、2,000 个源文件、新读取 16MiB。大小／修改时间／ctime／inode 未变的文件复用上次摘要；touch 后重算但内容摘要不变。一个插件实例最多运行一个探测，忙碌、超限、读取失败或工作段变化都保持 unknown，不使用部分摘要刷新 idle。缓存只保留最近一个工作区，后端按 owner／attempt／generation 和算法标识校验前后摘要，仅保存摘要和归属指纹。这是开发活动信号，不是文件完整性认证、实验成功或研究质量评分；科研调用预算另行约束。

deadline 按运行状态和预算变更设置定时器，不持续轮询闲置项目或扫描用量；刷新读取失败保留旧定时器，得到新状态后才替换或清除。控制事件不等待遥测落库；原生开始事件的用量登记按事件顺序入队，业务响应在有活跃关联时附带归属，无关联不阻断合法重放。非写请求的 replayed 为 unknown，不继承上次写入状态。Legacy 优先展示当前 generation 的最新挑战，旧代未处置挑战仍在完整索引中。

tokens／cost 是宿主已观测实际用量的停止阈值，覆盖可能不完整；并发请求、未知调用和外部进程不受全局费用／CPU／GPU 硬上限约束。研究 → 运行会话展示已用／限额／未配置和覆盖边界。

`.research/observations/<插件实例>.jsonl` 独立记录调用起止、输入字段／字符数、拒绝码、传输尝试、重放、preset 展开、实际模型／variant／原生 usage 和外部工具执行元数据。事务失败不会回滚该日志；日志 I/O 失败不改写已提交业务结果，会报告 coverage 缺口。不存提示词、工具参数或命令正文。缺失 token／cost 保持 unknown，聚合 usage.updated 不重复计量；使用 `python3 scripts/r2_measure.py <日志文件...>` 汇总。

## 用真实模型时的注意事项

- **审批会让推进停下**：节点执行会话要运行命令、写文件，OpenCode 默认会弹出审批，这时自动推进会停在审批上等你处理。想完全无人值守，启动时加 `--auto`（自动批准没有被明确禁止的操作），或者在配置里提前放开需要的权限。
- **请在项目根目录启动 OpenCode**：所有研究会话都放在这个目录下，由同一份插件实例管理。在子目录里打开时只能只读查看，不能控制推进。
- **同一项目不要同时在 DSH 和 OpenCode 里打开。**

## 不影响其他功能

- **没有关联研究项目的会话**：请求会去掉本插件的工具，不加入研究上下文；`subagent`、`question` 等自带工具照常可用。已有注册表时插件可能查询会话归属，以发现稍后建立的关联。
- **Python 只在需要时启动**：已有注册表，或者第一次使用 `/research` 时才会启动。研究项目内若身份或控制状态无法核实，可能执行实验的工具会被拒绝，状态恢复后再继续。
- **不改全局设置**：不写全局权限规则，不改 agent 和模型；只读规则只加在插件自己建的专家会话上。界面上只增加 `/research` 命令、一个全屏页面，以及只在研究会话里出现的侧栏面板和状态条。

## 开发与测试

```bash
bun test                                           # 推进引擎、工具、角色、命令的测试（假宿主 + 真实 Python）
node_modules/.bin/tsc -p tsconfig.json --noEmit    # 类型检查
make test-python                                   # 在仓库根目录运行 Python 测试
```

改完代码后执行 `opencode service restart`，正在用的 OpenCode 才会加载新代码：插件加载在后台服务里，只重启界面不够。

早期版本曾在真实 OpenCode 2.0.18 中跑通完整流程。当前控制器协议使用隔离的 OpenCode 2.2.5 与本地剧本模型复验；最低宿主版本因此提升至 2.2.5，SDK 版本保持 2.0.18。探针只使用临时项目、独立 XDG 目录和本地模型，不操作用户的后台服务或研究数据。

专家另用一个剧本在真实的 OpenCode 里跑过：
- 节点 X-001 叫了一位普通专家，专家调用 `shell` 被拒绝，读研究记录后交回报告；
- 节点 X-002 用自带的 `write` 写了一份报告并发布，再交给一位盲评专家，盲评专家读到 `input-1` 的内容，调用 `research_query` 被拒绝；
- 盲评专家的请求里没有工作目录、`AGENTS.md`、项目目标和发布编号。

### R1 读取策略

主会话用 `/research context-policy r1` 显式启用，`/research context-policy legacy` 回切，省略参数查看当前策略和主会话最近一次保存的注入策略。默认保持 legacy。切换清除缓存，下一次模型请求按新策略生成；不会重算旧回答、报告或重开已结束的项目。

看板主导航为 `1 概览 / 2 研究 / 3 成果`。按 `v` 在研究过程／运行会话之间切换，或在研究发现／发布材料／依据检查之间切换。依据检查显示风险含义、相关性来源、范围和材料入口，`f` 切换当前风险／历史审阅／文书提示，`g/o/s` 选择类型／决策范围或节点／审阅状态，`[` / `]` 翻列表页，`l` 选依据、Enter 打开、Backspace 返回对象。`x` 才显示原始数据。知识详情按固定引用读全条件和已存检查，并提供当前版本入口；更新版本不自动表示旧版失效。

看板没有独立的上下文检查入口。开发排障可使用 host-only `project_read(view="context_preview")` 读取同快照双策略预览、历史注入与进程观测，回放使用 `context_shadow`；这些接口不调用模型、不写记录、不重开项目。进程重启后观测计数归零；记录成功不证明模型执行成功或读懂了内容。

`research_query` 新增 `critical_now`、`findings`、`completed_findings` 集合，原样回传 `cursor` 续页；当前结果变化后需刷新。`review_todos` 支持 `state/trigger_kind/node_id` 筛选。unknown、未记录与截断都会明确保留。`review_todos`、`hints` 的游标绑定项目、集合及筛选，可原样回传续页；旧 offset/after 方式须重复原筛选。风险页的“决策范围”保留未知相关风险；分页失效提示按 r 刷新，不自动重试。

完整记录可按 `ref` 展开。超长记录用 `offset/limit` 分段，后续传 `expected_digest=<首段 content_sha256>`；记录变化会要求刷新，不会混接旧页。看板自动完成记录分段读取，文件与目录用 `n` 续读／翻页。

紧预算仍保留完整展开范围；预算连范围索引也无法容纳时会返回所需最小字符数。历史旧版本的字段失败仍可按固定 ref 查询；当前风险保留最新、已固定、明确输入及支撑祖先的失败。
