# 来源协议

这是独立合成的大厅资料接入项目，不包含个人资料或真实政务凭据。来源入口样例为 `/bucket?versions`，所有相对链接以响应 URL 为基准。来源账号、访问范围和允许访问的 origin 由调用配置提供；真实密钥不应保存在资料中。

S3版本列表的JSON网关：GET /bucket?versions，以 key-marker 与 version-id-marker 共同续页。Versions 和 DeleteMarkers 中均有 Key,VersionId,IsLatest,LastModified；IsTruncated 和两种 Next 标记说明分页。GET /objects/{key}?versionId=... 返回指定版本，HEAD /objects/{key} 返回当前x-amz-version-id及x-amz-delete-marker；最新删除标记时404但仍返回上述头，指定历史版本410为原件已永久不可领。GET /notifications 返回 Records，每条含 key,versionId,eventName,sequencer；sequencer为十六进制，只在同key内按左补零后的数值比较；不同版本时间相同不构成相同身份。通知不提供全局连续性保证，HEAD只证明本次对象观察，不证明扫描快照。

## 已有接口

acquire.transport.fetch 保留 HTTP 状态、原始响应头与实体字节，不自动跟随重定向。一次读取最多 max_bytes，超出时报错；当前默认超时5秒，已有单次调用语义继续有效。Response.header 大小写不敏感，sha256 只描述响应实体字节，不证明业务完整性。

acquire.decode.decode_versions 解释一页 ListObjectVersions 响应，分别返回 Versions、DeleteMarkers、IsTruncated 与成对的 NextKeyMarker/NextVersionIdMarker；decode 保留旧的单响应 key/value 口径。acquire.store.Store 在一个本地事务中保存响应与当前 key/value，兼容既有单次操作；版本历史不写入该表，避免同 key 覆盖旧资料。

## 版本历史接口

acquire.registry.Registry 是只增不删的 SQLite 登记簿。object_versions 以 (bucket,key,versionId) 为主键，版本与删除标记用 kind 区分，记录首次/末次出现于哪次扫描；重新看到同一版本只更新观察字段，页面中消失的旧条目不删除。saved_objects 逐版本登记领取结果：saved（原件与收据已保存）、permanently_missing（来源 410，原件被永久清除）、failed（可重试错误）；已 saved 的行不会被任何后续结果覆盖。key_states 保存最近一次 HEAD 观察，与枚举表分开，因为分页扫描不是同一时点快照。notifications 以 (bucket,key,sequencer) 去重，sequencer 按十六进制数值只在同 key 内比较，applied 标记是否已触发核实。receipts 的身份是 URL、状态、响应头与实体字节的组合哈希：相同字节来自不同版本 URL 时仍是两张独立出处收据。

acquire.catalog.Catalog 协同四类动作：enumerate_prefix 按成对 marker 续页并在每页落检查点，中断后 resume 续领，完成扫描才具备前缀覆盖证明；fetch_version_object/fetch_registered_objects 按 versionId 领取原件，删除标记跳过，410 与删除标记分别留痕；verify_key 做 HEAD 定向核实；handle_notification/fetch_notifications 处理至少一次投递的通知，新通知只核实其自带 key，重复投递幂等、陈旧通知忽略。history_report 汇总已核实对象、各版本出处与不确定范围（未完成扫描、通知提到但枚举未覆盖的 key、待重试的原件读取）。

来源回放程序支持任意 HTTP 路径、请求体条件、响应序列及字节原件，配置不等同于完整的远端系统。缺失路由不可当成协议承诺；使用者可按上述协议补充来源行为进行联调。样本内容不限定实际输入规模或异常组合。
