# 来源协议

这是独立合成的大厅资料接入项目，不包含个人资料或真实政务凭据。来源入口样例为 `/bucket?versions`，所有相对链接以响应 URL 为基准。来源账号、访问范围和允许访问的 origin 由调用配置提供；真实密钥不应保存在资料中。

S3版本列表的JSON网关：GET /bucket?versions，以 key-marker 与 version-id-marker 共同续页。Versions 和 DeleteMarkers 中均有 Key,VersionId,IsLatest,LastModified；IsTruncated 和两种 Next 标记说明分页。GET /objects/{key}?versionId=... 返回指定版本，HEAD /objects/{key} 返回当前x-amz-version-id及x-amz-delete-marker；最新删除标记时404但仍返回上述头，指定历史版本410为原件已永久不可领。GET /notifications 返回 Records，每条含 key,versionId,eventName,sequencer；sequencer为十六进制，只在同key内按左补零后的数值比较；不同版本时间相同不构成相同身份。通知不提供全局连续性保证，HEAD只证明本次对象观察，不证明扫描快照。

## 已有接口

acquire.transport.fetch 保留 HTTP 状态、原始响应头与实体字节，不自动跟随重定向。一次读取最多 max_bytes，超出时报错；当前默认超时5秒，已有单次调用语义继续有效。Response.header 大小写不敏感，sha256 只描述响应实体字节，不证明业务完整性。

acquire.decode.decode(body,headers) 返回 key/value 数组，当前只解释 fixtures/response.bin 中的一份响应。acquire.store.Store 在一个本地事务中保存响应与当前 key/value，尚无协议进度和跨响应完整性解释。acquire.service.collect_one 绑定 URL、原件及已解读记录，兼容这些单次操作的返回字段。

来源回放程序支持任意 HTTP 路径、请求体条件、响应序列及字节原件，配置不等同于完整的远端系统。缺失路由不可当成协议承诺；使用者可按上述协议补充来源行为进行联调。样本内容不限定实际输入规模或异常组合。
