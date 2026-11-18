# 大厅资料对象的版本历史与删除标记采集

Python 3.11+ 的授权来源读取项目。提供单响应读取、按前缀的对象版本/删除标记枚举（可中断续领）、指定版本原件领取、HEAD 当前状态核实、至少一次通知的定向核实，以及带出处依据的本地历史登记簿。协议口径见 docs/source-protocol.md。使用标准库运行，不需要安装运行依赖。

## 本地运行

启动演练来源：`python -m acquire.origin`，终端显示实际监听地址。该服务仅监听本机回环地址，记录实际 HTTP 请求，不连接政务系统。

### 单次读取（原有语义）

`acquire.service.collect_one(url, database, method='GET', headers=None, body=None, max_bytes=1048576)` 读取一份响应并按当前 key/value 保存。`python -m acquire.service` 的帮助显示位置参数；来源状态非 2xx 时不写资料。

### 版本历史工作流（新）

`python -m acquire`（即 `acquire.cli`）以子命令驱动，所有状态落在同一个 SQLite 登记簿：

1. `scan <entry_url> --bucket <bucket> [--prefix P] [--resume] [--verify] [--max-pages N]`
   按 `key-marker` 与 `version-id-marker` 成对续页，登记 `Versions` 与 `DeleteMarkers` 两组条目。每页写入检查点；中断后 `--resume` 从检查点继续，已采集页面不重复解释。枚举只增不删，不会因为某页没再看到旧版本而移除已保存原件。
2. `fetch <origin> --bucket <bucket> [--key K]...`
   为登记簿中的每个普通版本按 `versionId` 领取原件；删除标记不是原件，自动跳过。200 保存原件与出处收据；410 登记为永久缺失；其他状态记为可重试失败。已保存原件永不被后续失败覆盖（再次领取直接返回 `already_saved`）。
3. `verify <origin> --bucket <bucket> --key K...`
   HEAD 核实 key 的当前状态。删除标记为最新版本时来源返回 404，但仍带 `x-amz-version-id` 与 `x-amz-delete-marker` 头。HEAD 只证明本次观察，不证明扫描快照。
4. `notifications <origin> --bucket <bucket>`
   拉取通知并逐条处理。`sequencer` 为十六进制，只在同一 key 内按数值比较：新通知触发该 key 的定向 HEAD，陈旧通知忽略，重复投递幂等。通知不修改枚举结论，也不会作用到别的 key。
5. `report --bucket <bucket> [--prefix P]`
   输出每个 key 的版本链、各版本出处（已保存/永久缺失/删除标记/待重试）、HEAD 观察、通知水位，以及尚未确定的历史范围。

编程接口见 `acquire.catalog.Catalog`（`enumerate_prefix`、`fetch_version_object`、`fetch_registered_objects`、`verify_key`、`handle_notification`、`fetch_notifications`、`history_report`）与 `acquire.registry.Registry`。

## 语义边界

- 版本身份是 `(bucket, key, versionId)` 三元组；`versionId` 不透明、不可当时间排序。两个内容字节完全相同的版本仍是各自独立的历史位置，收据按 URL/状态/响应头/字节共同标识，不按文件名或内容摘要合并。
- 删除标记成为最新版本时，普通 GET 返回未找到，但指定 `versionId` 的旧原件可能仍可读取；历史版本被保留策略永久清除（410）不表示该对象当前也被撤下。登记簿用 `delete_marker`、`permanently_missing`、`saved` 三种出处分别留痕。
- 枚举不是冻结快照：扫描期间对象更新时，页内 `IsLatest` 不能拼出同一时点的当前目录，当前状态以独立的 HEAD 观察（`key_states`）为准。
- 通知只承诺至少一次投递，可能晚到或缺段；它只能触发定向核实，前缀覆盖证明只能来自完成的枚举。通知提到但枚举从未覆盖的 key 会在报告的 `unverified_notification_keys` 与 `uncertainty` 中显式标出。
- 未完成扫描的前缀、读取失败待重试的版本、未被枚举覆盖的通知 key，都属于“历史范围还不确定”，在报告中列明，而不是静默当作不存在。

## 资料

历史登记簿包含以下表：`scans`（扫描与续页检查点）、`object_versions`（版本/删除标记条目及首次/末次观察扫描）、`saved_objects`（每个版本的领取结果与收据引用）、`key_states`（最近一次 HEAD 观察）、`notifications`（同 key sequencer 水位与处理标记）、`receipts`（URL、状态、响应头、原始字节）。原有 `Store.documents()`/`Store.receipt()` 的单次读取语义保持不变。

测试：`python -m unittest discover -s tests -v`。
编译检查：`python -m compileall -q acquire`。
如需构建 wheel，可先安装构建依赖再执行 `python -m pip wheel --no-deps .`；日常运行和测试不需要 wheel。

fixtures/response.bin 是合成来源原始响应，scenario.json 给出可修改的本地来源路由及响应序列（已包含分页、删除标记、410、HEAD 404 带头与通知路由）。可通过 responses 数组按命中次数返回不同响应，require_headers/body_contains 检查交互上下文，body_base64 表达任意字节；没有匹配的请求返回404。该回放服务不模拟未在 scenario 中配置的协议行为，复杂联调需要按协议增加对应交互或使用另一个可控来源，不能把回放缺少路由解释成真实业务结果。
