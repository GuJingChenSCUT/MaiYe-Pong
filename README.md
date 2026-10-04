# 買嘢幫 · MaiYePong

面向香港消费者的受控购物代理：理解需求、核对规格和完整费用、比较方案，在用户确认的边界内代办，并保留停止与恢复记录。

## 当前状态

- 当前主应用是 `checkpoint/engineering/app` 的 Python 后端与原生网页，支持桌面/手机、维港日夜背景和 MaiYePong 商标。
- 已连接真实 DeepSeek。2026-10-04 本轮正常、澄清、运费变化、恶意商品文案四类在线案例均通过：30 次生成请求，94,750 tokens。商品仍为合成测试目录，没有真实商户订单或付款。
- Google OIDC 与微信网站扫码登录的后端适配已实现；应用资料尚未配置，因此按钮关闭。微信本机 HTTP 登录明确禁用，等待获批网站应用和正式 HTTPS 回调。
- TOP / HKTVmall 当前只准备签名与受限只读请求，没有在线传输或消费者下单权限。WeChat Pay HK 支付仍关闭。微信登录不代表微信支付授权。
- 本地付款使用 LocalPSP，网页不自动派发支付。原数据库损坏事件根因仍未关闭，停点一未通过；本仓库不是生产支付服务。

## Windows 本机启动

需要 Python 3.12 或兼容更新版本，首次运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup-local.ps1
powershell -ExecutionPolicy Bypass -File scripts\start-local.ps1 -Offline -NoOpen
checkpoint\engineering\.venv\Scripts\python.exe scripts\demo-account.py --show
```

打开 http://127.0.0.1:8765/ ，将窗口中的专用 `demo-buyer` 存取码填入登录页。只需填写存取码，不需要填写用户名。账号只有 buyer 权限；每台电脑随机生成，不在仓库提供公共密码。也可查看本机 `local_state/demo_account_private.txt`。不要上传或分享该文件。

脚本只在已有应用数据库上创建模拟买家；凭据与数据库不匹配时明确拒绝，不重建数据库、不恢复已撤销权限。重复运行保留同一身份。开发者现有三角色存取码仍可通过 `scripts/show-access.ps1` 查看。

已运行服务会被复用；`-Offline` 不能改变正在运行的在线服务。需要切换模式时先确认没有运行中的任务，再受控重启目标进程，或选择独立端口。公开部署需要额外 HTTPS、身份、Cookie、数据库、备份、监控与提供方回调设计。

## 体验完整本地流程

1. 双击根目录 `启动演示.cmd`：复用本机服务、显示模拟买家存取码并打开空白输入页。未安装环境时先执行上面的 setup 步骤。
2. 从 [演示案例](docs/demo/cases.txt) 复制一条完整请求后提交，才会显示结果。五例覆盖比较、缺预算补问、运费变化、预算拦截和缺货停止；仅专用模拟买家在本地模式下精确匹配，不能当作任意自然语言理解能力。没有核实评分时显示「評分待核實」。
3. 确认方案 → 本地唯一命令入队；入队不是付款成功。
4. 停止委托 → 查看派发前停止结果 → 刷新，状态继续保留。Esc 或关闭停止弹窗不提交停止请求。
5. 案例清单不出现在首页。未匹配的输入按常规流程处理，不偷偷替换成示例商品；切换真实模型模式后不会命中演示规则。未知费用不按零，未来积分不抵消现金预算。

完整操作说明见 [DEMO_READ_FIRST](docs/DEMO_READ_FIRST.txt)。[微信付款效果图](docs/demo/payment-concept-test-only.png) 已标注「防止信息泄露，测试专用」，仅供展示，无收款二维码，不会扣款；图中金额也是示意数值。

## 登录和平台接入

详见 [登录配置与权限边界](docs/LOGIN_INTEGRATION.txt) 和 [平台准备度](docs/PLATFORM_READINESS.txt)。本地读取环境变量，`.env.example` 只是模板，不会自动加载。

Google 验证需要可选依赖：

```powershell
checkpoint\engineering\.venv\Scripts\python.exe -m pip install -r checkpoint\engineering\requirements-auth.txt
```

平台离线签名测试另需 `requirements-platform.txt`。配置真实 OAuth 应用后必须重启后端并分别完成平台真实授权验收；按钮可用仅代表配置检查通过，不等于平台认证成功。

## DeepSeek

运行 `scripts/configure-deepseek.ps1` 在本机保存 API key，使用 Windows 当前用户 DPAPI 加密。密钥不进入源码或模型任务。保存后以 `scripts/start-local.ps1` 启动在线服务。模型只理解、澄清、研究和提出工具调用；金额、授权、状态和停止由确定性服务处理。

四类在线测试会消耗模型额度，仅在需要时执行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test-live.ps1 -AllowProviderCharge
```

每场景共享最多16次生成请求、32,768预留输出token、512KiB累计请求体和180秒预算；socket超时不是严格墙钟中断。四场景最多64次请求，不是货币费用上限。

## 测试和证据

本轮基线417个唯一Python离线测试通过，含登录39、接口11、平台90；后续演示增量17项、接口11项复验、模拟账号14项及测试工具7项单独统计，不冒充全量重新运行。详情、真实模型、浏览器及外部依赖见 [本轮验收](docs/verification/latest.json)。首次聚合运行曾触发240秒超时；保留原记录后，同一应用聚合292项在209.532秒通过，未确证慢因，不删除异常记录。测试工具现支持可配置超时并保存超时日志。

```powershell
Set-Location checkpoint\engineering
.venv\Scripts\python.exe tools/verify_refresh.py --out evidence/local-recheck --timeout-seconds 600
```

`reference` 中保留的旧输出是历史参考，不能与本轮结果累计。运行数据库、访问码、会话、密钥、私人证据和打包产物不上传。本机完整原始日志放在忽略提交的 `verification` 中。

根目录 `src`、`tests` 与 Node 清单是独立的 TypeScript/Convex 接入预研；不是当前 Python 交易闭环，不应当作已部署 Convex 服务。

## 设计原则

HKD 金额使用最小货币单位；确认绑定用户、商户、规格、数量、完整金额和报价版本。付款与订单独立记录，付款成功需服务端证据。结果 UNKNOWN 时只查询原交易，不能创建新付款。HKT / The Club 是候选合作方，尚未证明取得合作或接口。

已研究 Symy Shopping 的公开实现，但未复制其 AGPL 源码；其购物车 checkout 不能当作真实订单和支付证据。项目素材按团队提供的香港背景和商标设计集成。
