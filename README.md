# fit-session

运动设备文件的定义驱动采集、来源检查与原件留存工具。

## 当前能力

`source.py` 查看来源的现有结构，不把检查结果冒充完整业务采集。
`storage.py` 将原始字节、来源标识、摘要和检查结果一起保存在本地 SQLite，重复原件保留同一收件编号。
`sampledata.py` 构造可重复的本地数据；`transport.py` 提供独立的回环 HTTP 演练源与请求记录。
`fitcodec.py` 按文件内定义逐条解码 FIT：本地消息号重定义即切换布局，处理混合端序、
基础类型、无效值、缩放/偏移与压缩时间戳回绕；未知标准字段和无自述开发者字段只保留
原始字节，绝不猜成心率。`collector.py` 按时间窗（非文件顺序等分）关联 session/lap/record，
输出带字节定位证据的训练事件、无效测量与缺失区间。`storage.py` 额外维护完整训练与
事件表，截断/损坏文件只生成标为不完整的预览，不进入完整训练列表，重复接收幂等。
解释范围和来源变体见 `docs/source-contract.md`，当前检查测试与将来新增业务能力的测试应分开保留。

## 环境

Python 3.11 或以上，仅标准库，无需凭据和外部数据。使用容器时可执行 `docker build -t fit-session .`，依赖及镜像须在正式运行前准备。
运行入口、测试与演示均不安装包，不访问公网。数据库使用临时路径，不向代码目录写入业务文件。

## 测试与构建

构建工具版本固定在 `requirements-build.txt`，在环境准备阶段执行 `python3 -m pip install -r requirements-build.txt`；容器构建已包含此步骤。应用运行仍仅使用标准库，测试及演示不安装依赖。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py source.py sampledata.py sampledata_ext.py storage.py transport.py scenario.py fitprofile.py fitcodec.py collector.py
python3 -m build --wheel --no-isolation
```

## 快速演示

```sh
python3 app.py demo
python3 app.py collect-demo
```

`demo` 演示真实启动本地来源、领取原件、执行检查并保存临时数据库，展示检查结果与
重复收件身份。`collect-demo` 在回环 HTTP 上领取三份本地虚构设备文件（完整、截断、
CRC 损坏），演示消息号重定义、压缩时间戳回绕、数组、无效测量、缺失区间、不完整
预览隔离与同文件重复接收的稳定事件标识，退出后自动清理。正常在数秒内完成，不以
预录日志代替执行。

## 单独检查、收件与采集

```sh
python3 app.py fixtures /tmp/fit-session-fixtures
python3 app.py device-fixtures /tmp/fit-session-fixtures
python3 app.py inspect /tmp/fit-session-fixtures/sample.fit
python3 app.py capture /tmp/fit-session-fixtures/sample.fit --database /tmp/fit-session.sqlite3 --source local-partner
python3 app.py collect /tmp/fit-session-fixtures/device-activity.fit --database /tmp/fit-session.sqlite3 --source local-partner
python3 app.py trainings --database /tmp/fit-session.sqlite3
python3 app.py events --database /tmp/fit-session.sqlite3 [--complete-only]
python3 app.py list --database /tmp/fit-session.sqlite3
```

`collect` 对完整文件写入训练与事件，对截断/损坏文件只返回 `incomplete` 预览而不建
训练；`trainings` 因此永远只列出完整训练。生成夹具不会覆盖已有文件。运行错误以 JSON
输出到标准错误并返回退出码 2；原始输入无效时不生成成功收件。内部原件与任何对外展示
副本应分开管理，不写入真实个人数据。
