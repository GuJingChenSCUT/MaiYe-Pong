香港购物 Agent 工程交付包 v0.3
更新日期：2026-10-02 UTC

本包将模型协议、应用工作规程、受限工具和持久化交易参考连接起来。
DeepSeek V4.1 Flash 的官方 API model ID 为 deepseek-flash；模型配置默认不启用生产。
本轮没有调用真实 DeepSeek、使用生产身份提供方、接入支付机构沙盒、扣款或部署公网应用。

新增内容
capabilities/：8项应用自定义JSON工作规程及Schema、索引、规则边界、负例；全部draft。
capability_runtime/：规程加载器、工具交集、规则资格与时效选择器、阶段工具政策及离线测试。
transaction_reference/：SQLite持久化交易内核、独立持久化本地支付模拟器、request_execution适配器及测试。
qa/test_reference_composition.py：加载器→假模型工具请求→既有ToolRuntime→交易队列→本地模拟结果的3项组合测试。
diagrams/capability_execution.svg及PNG：能力加载、工具调用和交易执行架构。

保留内容
contracts/：21个JSON Schema、17项OpenAPI操作、13个受限工具、15个角色化输出投影、6逻辑角色和工作流。
runtime_reference/：DeepSeek参考协议适配器、离线测试、显式启用的只读live smoke。
diagrams/：运行时架构和部署拓扑；可编辑源文件及PNG。
coordination/：六个开发工作包和发布门槛。
data/：18条公开研究种子。两种JSONL格式是同一批记录，不是36条；均未获批为生产知识。
qa/：规格校验、28项完整应用验收设计及模型评测计划；完整应用验收尚未执行。
AGENTS.md：开发智能体协作规范；不提供运行时消费权限。

关键边界
八项规程不是DeepSeek原生技能上传格式，也不是个人Codex/ChatGPT技能。
规程不能增加权限：有效工具是角色、任务、阶段、规程与已部署处理器的交集。
可信上下文必须由真实身份与任务服务构造；dataclass和测试principal不等于身份认证。
规则选择器没有真实费率和积分价值，也不是完整计费引擎。测试值全部为synthetic fixture。
哈希固定不是签名发布认证；示例仅显式允许离线draft测试，生产需要独立审核和发布。
本地支付模拟器不是支付机构沙盒；SQLite事务不证明生产PostgreSQL多实例可靠性。
本次交易参考只覆盖单商户单订单、全额退款和合成奖励单位冲回。
撤销先于持久派发claim时可拦截；claim之后原付款可能继续，必须核对在途状态。

验证
各目录验证报告记录真实测试范围和结果；qa/reference_verification.json汇总当前证据。
先在独立环境安装qa/requirements.txt，再运行各目录README中的本地命令。
模型测试不等于真实DeepSeek质量评测，局部组合测试不等于三端完整应用端到端测试。
ModelProfile默认禁用，live_verified=false；不能仅修改布尔值绕过接入证据与发布审查。

下一步按START_HERE.txt完成真实身份、商户接入、DeepSeek效果评测、提供方沙盒与三端联调。
