# 来源格式与业务语义

FIT 头 12 或 14 字节，.FIT 签名、数据长度及 CRC16 使用协议标准多项式。本地 profile 支持 record(20)、lap(19)、session(18)、field_description(206)、developer_data_id(207) 常用字段；定义按 architecture 选择端序。压缩头省略 timestamp 字段并以 5 位低位恢复。未知消息可以保留定义与原始字段，不能对未知单位做猜测。样例包含本地消息号重定义与时间低位回绕。

本地演练夹具由 `sampledata.py` 生成，均为虚构业务数据。`sample` 是当前可查看输入，`invalid` 是边界失败输入，其余用于对照来源变体。生成不会下载资源。

本地 HTTP 演练器采用方法与原始路径作为路由键，值为按调用次数返回的响应数组，耗尽后维持最后一项。响应可定义 status、headers、text/base64、require_headers、chunk_bytes、disconnect_after。每次请求保留方法、路径、头与正文摘要表示，便于核对实际访问；演练器只监听回环地址。

原始输入上限 16 MiB 是现有查看入口的保护值，不代表未来持续采集需一次读完整来源。格式含义和截断错误不能被 HTTP 200 覆盖。
