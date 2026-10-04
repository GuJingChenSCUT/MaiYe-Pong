HacKU 四切片工程补充包 0.4-proposal | 2026-10-03

产品目标：帮助香港成年消费者完成有预算和条件限制的购物委托，对依据、授权边界和结果负责。
首版：两家比较，一家成交；白名单日用品；HKD；一个 PSP；买家、商户、运营三端。
业务指标：真实用户愿意再次委托，而且正确性不下降、人工负担有可观察的改善。

这是可以复跑的参考工程补充，不是已上线应用。原始参考代码位于 reference/；新代码位于 slice04/。
新增契约是 0.4 提案，尚未取得团队契约负责人的合并确认；不得直接替换原有线上契约。
未取得真实 DeepSeek Key、商户接口授权、支付沙盒或真实用户研究记录。

先读：
  docs/01_review_and_architecture.txt  审查发现、职责边界、工程待办
  docs/02_deepseek_codex_and_harness.txt  Codex 接入重点与产品调用的区别
  docs/03_four_slices_and_acceptance.txt  演示脚本、验收门槛与故障处理
  docs/04_knowledge_and_value_protocol.txt  知识治理和三组对照方法

本地复跑（Python 3.12；不要在全局环境覆盖依赖）：
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python tools/verify_all.py
Windows 将 .venv/bin/python 替换为 .venv\Scripts\python.exe。
顶层 evidence/verification.json 是本轮复跑结果；reference/ 内的旧报告带各自时间，不等同本轮结论。
verify_all 只跑离线任务，不会调用真实模型或付款。根目录 requirements.txt 锁定直接依赖；
dependency_versions.json 记录本次验证的传递依赖版本，生产项目仍应使用自己的完整锁文件。

真实模型验证（工程师已在自己的环境配置 DEEPSEEK_API_KEY 后）：
  .venv/bin/python tools/run_agent.py --mode live --scenario shipping_increase --allow-provider-charge --out evidence/live_shipping_01.json
该选项会发生真实 API 调用及供应商计费；商户工具仍是合成场景。
缺 Key 会返回 blocked，不会自动降级成成功的离线演示。
--thinking 可单独验证思考模式；no_change / both_unavailable / injection 为其他场景。
每次使用新的输出文件名，保留失败记录。不要把 Key 放入参数、仓库、截图或报告。

四个结果的当前界限：
  事实：两家真实页面可追溯，产地冲突未解除，禁止执行；条件小计不等于到手总价。
  Agent：离线流程与约束已验证；真实模型资格审查 blocked_key_missing。
  交易：SQLite + LocalPSP 模拟成功/停止/跨进程原单恢复已验证；官方 PSP 未验证。
  价值：可运行汇总器、空观测表和建议试验分配；真实样本为零，不能宣称省时或增收。

关键文件：
  data/public_fact_slice.json       两家页面事实、来源、未知与冲突
  data/source_index.json            官方资料及五个开源项目固定提交审查记录
  schemas/commerce.schema.json      十个可执行本地契约定义（较完整生产域模型更窄）
  slice04/domain.py                金额/证据/权限/快照门禁
  slice04/agent.py                 实际 DeepSeek 调用入口和动态工具环境
  slice04/transaction.py           完整快照绑定本地交易参考组件
  data/benchmark_observations.jsonl 空的真实观测表；不含编造的成绩
  data/release_gates.json           负责人、状态和上线前所需证据

生产接线必须另外完成：可信身份、持久队列、数据库事务/锁、真实商户适配器、PSP 签名回调、
付款/订单两个状态机、取消与退款、三端访问控制、隐私与权利审核。禁止将 TestPrincipal 作为登录实现。
hash 证明同一内容，不证明来源真实；证据发布状态和 connector 权限只能由可信服务写入。
reference/ 中既有 Plan 合同仍保留更宽的商户上限。本补充的“两家比较、一家成交”在新切片实现，
生产 API 需显式迁移及版本审查，不能只靠提示词覆盖旧合同。

引用与许可：原有参考代码来自本次用户上传，保留归属；外部五个项目只作静态研究，未复制其源码。
若团队日后引入第三方源码，固定提交、保留 LICENSE/NOTICE、核查依赖与比赛规则后再合并。
未发布站点、推送 GitHub、联系商户或处理真实付款。
