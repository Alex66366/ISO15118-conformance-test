# TITAN 编译与运行故障排查

本文档汇总了 ISO 15118-2/4 TTCN-3 测试代码在 TITAN 工具链中常见的编译和运行错误，供 Infrastructure Agent 和 Fault Corrector Agent 参考使用。

## 1. TTCN-3 编译错误

### 1.1 端口定义错误

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `port type 'X' has no associated port definition` | ComponentsAndPorts.ttcn 中缺少端口类型定义 | 在对应 module 中添加 `type port X message { out ...; in ...; }` |
| `unknown port 'X'` | 端口名称未在 ComponentsAndPorts 中声明 | 确保 testcase 中使用的 port 名称与 ComponentsAndPorts.ttcn 中声明一致 |
| `port 'X' is not a message port` | 端口类型声明为 non-message 类型 | 端口必须声明为 `message` 类型 |

### 1.2 Import / 模块依赖错误

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `module 'X' has not been imported` | 缺少 import 语句 | 在 module 顶部添加 `import from X all;` |
| `cannot find definition of type 'X'` | 类型定义所在模块未导入 | 检查类型定义的模块名，在当前 module 中添加 import |
| `symbol 'X' is not visible here` | import 范围不够 | 使用 `import from X all;` 而非 `import from X type X;` |

### 1.3 组件类型不匹配（runs on）

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `runs on clause mismatch` | 函数/altstep 的 runs on 与调用方组件不一致 | 检查所有 `runs on <Component>` 声明，确保被调用函数的 runs on 与调用方组件兼容 |
| `function 'X' cannot be invoked from a component of type 'Y'` | 组件类型不匹配 | 被调用函数的 runs on 组件类型必须是调用方组件类型的父类型或相同类型 |
| `altstep 'X' is not accessible from component 'Y'` | altstep 的 runs on 与测试组件不匹配 | altstep 必须 runs on 相同的组件类型 |

### 1.4 模板类型错误

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `type 'X' and 'Y' are not compatible` | 模板参数类型与函数签名不匹配 | 检查模板参数类型与函数调用时传入的类型是否一致 |
| `template type mismatch` | 模板参数数量不一致 | 检查模板定义中的参数数量与调用时传入的数量是否一致 |
| `incompatible types in assignment` | 赋值类型不兼容 | 检查赋值操作左右两侧的类型是否匹配 |
| `parameter type mismatch in template` | 模板内参数类型不匹配 | 检查模板内模板参数的使用方式 |

### 1.5 语法错误

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `syntax error, unexpected 'X'` | TTCN-3 语法错误（括号、关键字等） | 参照 Titan ReferenceGuide 第3章检查语法 |
| `expected 'X' before 'Y'` | 缺少符号或符号位置错误 | 检查该位置的符号是否正确 |
| `missing semicolon` | 缺少分号 | TTCN-3 语句以分号结尾 |
| `unmatched closing delimiter` | 括号/花括号不匹配 | 检查所有 `(`, `{`, `[` 是否都有对应的关闭符 |

### 1.6 Timer 错误

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `timer 'X' has not been started` | timeout 分支前未 start | 在 timeout 之前调用 `tc_X.start()` |
| `timer 'X' is already running` | 重复 start 已运行的 timer | 停止后再启动：`tc_X.stop(); tc_X.start();` |
| `timer 'X' is not running` | stop 了未运行的 timer | 确保 stop 调用前 timer 确实处于运行状态 |

### 1.7 Alt 块错误

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `incomplete alt block` | 某端口有 send/receive 但 alt 块未覆盖 | 在 alt 块中为所有已映射端口提供 receive 子句 |
| `port 'X' has outgoing messages but no alt branch` | 端口发送了消息但 alt 块未处理响应 | 添加对应端口的 receive 子句 |
| `no default altstep for port 'X'` | 未提供默认处理 | 添加 altstep 或 `[]` 通配分支 |

---

## 2. C++ 链接错误

### 2.1 链接器错误

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `undefined reference to 'X'` | C++ 适配器未实现对应方法 | 在对应的 .cc 文件中添加方法实现。检查方法签名是否与头文件一致 |
| `undefined reference to 'vtable for X'` | 虚函数未实现 | 在对应的 .cc 文件中实现基类声明的所有纯虚函数 |
| `multiple definition of 'X'` | 同一符号在多个 .cc 文件中定义 | 使用 `static` 或 `inline` 或 `namespace` 限定作用域 |
| `ld returned 1 exit status` | 链接失败 | 查看具体的 undefined reference 错误并修复 |

### 2.2 编译错误

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `fatal error: TitanTci.hh: No such file or directory` | TTCN3_INC 未设置或路径错误 | 设置 `export TTCN3_INC=/usr/include/titan`（Linux）或对应 Windows 路径 |
| `cannot find -lttcn3` | TITAN 库路径未添加到链接器 | 在 Makefile 中添加 `-L/usr/lib/titan` |
| `unknown type name 'CHARSTRING'` | 缺少 TITAN 类型头文件 | 确保 `#include <TITAN_Channel.hh>` 等头文件已包含 |

---

## 3. 运行错误

| 错误信息 | 原因 | 解决方案 |
|---|---|---|
| `Testcase interrupted` | 超时未响应 | 调整 titan_runtime.cfg 中的 `tc_timeout` 参数 |
| `Port not mapped` | 端口未执行 map 操作 | 在测试开始前执行 `map(mtc:port_name, system:port_name);` |
| `Component not created` | PTC 未正确创建 | 检查 `create` 调用和 `alive` 参数 |
| `Verdict not set` | testcase 结束前未设置 verdict | 确保 testcase 中至少调用了一次 `setverdict()` |
| `Control part finished without verdict` | control 块结束但未设置 verdict | 在 control 块末尾添加 `setverdict(pass);` |
| `Signal route not registered` | 信号路由未注册 | 检查 testport 的 `register_signal_route` 调用 |

---

## 4. 编译错误到代码位置的映射

| 编译错误类别 | 对应的 TTCN-3 源文件 | 修复方法 |
|---|---|---|
| `port_undefined` | `ComponentsAndPorts.ttcn` | 定义正确的 `type port X message { ... }` |
| `import_missing` | testcase 所在 `.ttcn` | 添加 `import from <module_name> all;` |
| `type_mismatch` | test_behavior_function 所在 `.ttcn` | 检查模板参数类型是否匹配 |
| `runs_on_mismatch` | function/altstep 定义所在的 `.ttcn` | 统一 `runs on` 组件类型 |
| `ttcn_syntax` | 错误所在 `.ttcn` 文件 | 对照 TITAN ReferenceGuide 语法检查 |
| `cpp_link` | `cpp_adapters/` 中的 `.cc` 文件 | 实现缺失的方法或修复符号定义 |
| `timer_error` | test_behavior_function | 检查 timer 的 start/stop/timeout 使用 |

---

## 5. TITAN 配置文件关键参数

### 5.1 titan_runtime.cfg

```
[LOGGING]
LogEntitybits := LogOptions.*, LogSourceInfo.*  # 完整日志用于故障分析

[EXECUTE]
tc_timeout := 60  # testcase 超时，应大于 timer_value * 安全系数

[TESTPORT_PARAMETERS]
*.pt_V2G_TCP_TLS_SECC_Port.sut_ip := "192.168.1.100"  # SUT IP
*.pt_V2G_TCP_TLS_SECC_Port.connection_timeout := 5.0   # TCP 连接超时
```

### 5.2 V2GTP 头部常量参考

| 字段 | 值 | 说明 |
|---|---|---|
| protocolVersion | `01` | 协议版本 |
| invProtocolVersion | `FE` | 版本取反 |
| V2G_Message payloadType | `8001` | V2G 消息载荷 |
| SupportedAppProtocolReq | `8000` | 应用协议请求 |
| SupportedAppProtocolRes | `8002` | 应用协议响应 |
| SessionSetupReq | `8003` | 会话建立请求 |
| SessionSetupRes | `8004` | 会话建立响应 |
