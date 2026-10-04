DeepSeek 工具调用参考实现 · 2026-10-02

状态与适用范围
这是可运行的 API 协议适配与工具网关参考代码，包含离线测试；它不是完整三端
应用、支付系统或生产级工作流。没有进行真实 DeepSeek 推理、支付沙盒或真实
商户集成。没有自动创建账户、读取密钥或发送实际订单数据。

默认模型 deepseek-flash，strict 工具调用端点 https://api.deepseek.com/beta。
模型别名、账户能力及接口规范在上线时需再次核验，不能凭模型名称承诺成功率。
HTTP 使用标准库 urllib；JSON Schema 使用 jsonschema。无需 LangChain、MCP
服务或六个独立模型进程便能运行本参考。每次任务的角色、上下文、工具分别隔离。

本实现做了什么
1. 动态读取 ../contracts/tool_registry.json。没有硬编码业务工具数量；工具的
   allowed_agent_roles 由服务器 TrustedContext.role 匹配，客户端不能切换角色。
2. 将本地输入合同转换为 DeepSeek strict wire schema。对象属性全 required，
   additionalProperties=false。minLength/maxLength/minItems/maxItems 以及不在
   官方支持列表中的 format 不发到 provider，但保留在本地原始合同并重新校验。
   此参考只实现当前合同需要的 schema 子集；未实现的引用/组合会失败关闭，
   不是断言 DeepSeek 不支持所有这些 JSON Schema 能力。
   const 标量转换为单值 enum；default 不送到 provider，也不会自动补业务参数。
3. 使用顶层 thinking:{type:enabled|disabled}。开启时发送 reasoning_effort。
   默认 tool_choice=auto，不在 thinking 模式强制 required 或命名工具。
4. 原样保留完整 assistant 消息（含 reasoning_content）及 tool_call_id 对应关系。
   reasoning_content 只属于受控私有协议状态，不作为交易审计证据、用户解释、
   分析埋点或公开日志。跨进程恢复/加密持久化/保留期由部署层另行实现。
5. 参数 JSON 拒绝重复键、非有限数、越界字段、错误格式和超限长度；全批请求
   在执行前先校验，未知工具或角色越权不会调用服务。只读工具按顺序执行。
6. Authorizer.check 必须由真实服务实现资源归属、作用域、状态、版本与时效检查；
   Authorizer.check_output 必须检查结果投影、版本、证据和领域约束。参数结构
   正确不等于拥有交易权限。测试里的 FixtureAuthorizer 不能放进生产。
7. 输出使用 registry 的 output_schema 或 output_schemas_by_role，不能被 handler
   覆盖。同时校验 status/data/error 含义，防止额外敏感字段泄漏给模型。
8. 任何非 read 工具必须是该轮唯一命令，交给注入的 DurableWorkflow.enqueue_once，
   然后立刻停止模型循环。模型没有银行卡、数据库或收单 API 凭据。
   返回 workflow_handoff 仅表示工作流接收回执，不能显示“付款成功”。
9. operation_key 来自服务器的持久化逻辑步骤，不使用 tool_call_id 作为付款幂等键。
   工作流必须在同一事务中检查授权、命令内容摘要及唯一键，持久化后再执行。
   超时/连接中断/错误回执可能发生在命令已接收之后：必须按原 operation_key
   对账，不能重新让模型生成命令或更换幂等键。没有工作流实现就失败关闭。
   提交后的输出校验或权限hook发生任何常规异常也归类为需要对账；运行时的
   audit callback 只是尽力发送的观测事件，失败累计 audit_failure_count，不会
   把已提交写命令伪装为可重试失败。真正金融审计须由工作流事务原子落库。
10. 模型调用轮数、工具数量、单轮数量、消息/参数/结果大小与时间都有上限；
    HTTP 无自动重试并拒绝重定向，防止 Bearer 密钥被带到其他端点。
    时间上限在调用边界检查，不能替代生产容器的强制终止。

本实现还没有做什么
本目录没有持久化状态机、数据库事务/RLS、预算原子预留、真实支付、签名回调、退款、
奖励账本、浏览器界面、租户身份服务、案件裁决、生产密钥库、计费预算服务或
分布式审计。assistant_text 只是解释文本，仍可能含幻觉；没有对自然语言的所有
事实作确定性证明。用户界面的付款、退款和授权状态必须读取业务 API 的真实
状态，不能依据 assistant_text 判断或将其渲染成权威交易回执。
v0.3 在相邻 transaction_reference/ 增加 SQLite 局部参考内核和本地模拟支付，
并通过 qa/test_reference_composition.py 验证本适配器的写命令接线；它仍不是生产工作流。
撤销按钮必须直接走可信撤销 API，不应被本模型任务过期或工具循环阻塞。
真实运行前还要补网络域名白名单、供应商数据条款、故障恢复和跨租户渗透测试。
ModelProfile 的 live_verified/生产启用门槛属于外层部署编排检查，本模块尚未
实现该生产发布控制；存在配置文件不等于门槛已经生效。本探针不得用于跳过
生产门槛，探针通过也不能把业务接入状态自动升级为 production_verified。

本地安装与检查（在本目录运行）
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python check_registry.py
.venv/bin/python smoke_probe.py
.venv/bin/python smoke_probe.py --thinking
或执行 .venv/bin/python verify_offline.py，一次完成上述离线检查，并生成
offline_verification.json 和 offline_test_output.txt。报告包含被核验文件的哈希，
它描述参考实现的检查，不是整个三端产品或真实资金安全的通过证明。

真实 API 协议探针（未执行）
先在安全环境中设置 DEEPSEEK_API_KEY，不要写入源码、前端或提交的配置文件。
.venv/bin/python smoke_probe.py --live --allow-provider-charge
.venv/bin/python smoke_probe.py --live --allow-provider-charge --thinking
两项显式参数缺一不可；调用可能产生 API 费用。工具仅读取虚构公开测试常量，
不能访问订单、资金或其他工具。通过仅说明该探针的协议可用，不代表购物准确率、
整份业务 schema 已被服务端接受，或六角色协作/付款流程已验证。

接入真实业务的顺序
先由身份服务构造 TrustedContext，再由编排器确定当前角色与必要证据；为该角色
所有可用工具绑定真实 read handlers 和 output policy。生产 Authorizer 必须采用
拒绝优先规则，使用服务端状态，不能信任模型提供 actor/tenant/预算/mandate。
写工具只绑定持久化命令接收端。若发生模型失败、错误参数或权限拒绝，向编排器
返回错误码；由工作流决定澄清、人工审核或终止，禁止模型自行扩大权限重试。

官方依据（2026-10-02 核对；无私有 API 调用）
https://api-docs.deepseek.com/guides/tool_calls/
https://api-docs.deepseek.com/guides/thinking_mode/
https://api-docs.deepseek.com/api/create-chat-completion/
这些文件是工程参考与测试证据，不构成商户合作、支付接入或性能承诺。
