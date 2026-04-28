# ISO 15118-2 SECC Conformance Test System

本项目是一个基于 Eclipse TITAN 的 ISO 15118-2 SECC 一致性测试系统，用于测试电动汽车（EV）与充电站（SECC / Supply Equipment Communication Controller）之间的高层通信协议（HLC）。

## 项目概述

本项目借助 **Multi-Agent** 架构自动化生成可执行的 TTCN-3 测试程序。通过多个专业 Agent 协作，自动完成从协议规范到可编译运行的测试代码的转换。


### 测试的协议栈

```
┌─────────────────────────────────────────────────┐
│          TTCN-3 测试层（测试行为与判决）            │
├─────────────────────────────────────────────────┤
│          V2GTP (Vehicle-to-Grid Tunneling)       │  TCP/TLS
├─────────────────────────────────────────────────┤
│          EXI (Efficient XML Interchange)          │  二进制编码
├─────────────────────────────────────────────────┤
│          SDP (Service Discovery Protocol)         │  UDP 多播
├─────────────────────────────────────────────────┤
│          TLS 1.2 (可选加密通道)                    │
├─────────────────────────────────────────────────┤
│          IEC 61851 (控制引导 CP 状态)              │
└─────────────────────────────────────────────────┘
```

## 目录结构

```
demo/
├── build.bat                    # Windows 构建脚本
├── build.sh                     # Linux 构建脚本
│
├── config/                      # 配置文件
│   ├── titan_runtime.cfg       # TITAN 运行时配置
│   └── hardware_env.json        # 硬件环境配置（网络、端口、证书路径）
│
├── src/                         # 源代码
│   ├── ttcn3/                   # TTCN-3 测试代码
│   │   ├── 1_Data_Definitions/  # 数据定义
│   │   │   ├── Types/           # 类型定义
│   │   │   │   ├── Types_SECC_15118_2.ttcn    # V2G 消息类型定义（框架）
│   │   │   │   └── TimerAndConstants.ttcn     # 常量和定时器定义（框架）
│   │   │   ├── Templates/       # 消息模板
│   │   │   │   └── CmnTemplates.ttcn           # 通用消息模板
│   │   │   ├── PICS/            # PICS 配置
│   │   │   │   └── PICS_SECC_15118_2.ttcn     # 协议实现一致性声明
│   │   │   └── PIXIT/           # PIXIT 配置
│   │   │       └── PIXIT_SECC_15118_2.ttcn    # 测试额外信息配置
│   │   │
│   │   ├── 2_Architecture/      # 架构定义
│   │   │   └── ComponentsAndPorts/
│   │   │       └── ComponentsAndPorts.ttcn    # 组件和端口类型定义
│   │   │
│   │   ├── 3_Behavior_Steps/    # 行为步骤
│   │   │   ├── PreConditions/   # 前置条件
│   │   │   │   ├── PreConditions_SECC_15118_2.ttcn
│   │   │   │   ├── f_SECC_CMN_PR_Binding_001.ttcn
│   │   │   │   ├── f_SECC_CMN_PR_InitConfiguration_001.ttcn
│   │   │   │   ├── f_SECC_CMN_PR_InitConfiguration_002.ttcn
│   │   │   │   ├── f_SECC_CMN_PR_PLCLinkStatusCombinedTesting_001.ttcn
│   │   │   │   ├── f_SECC_CMN_PR_SetPowerFlowConfiguration_001.ttcn
│   │   │   │   ├── f_SECC_CMN_PR_SetProximityPilot_001.ttcn
│   │   │   │   ├── f_SECC_CMN_PR_StateA_001.ttcn
│   │   │   │   ├── f_SECC_CMN_PR_StateB_001.ttcn
│   │   │   │   └── f_SECC_CMN_PR_SupportedAppProtocol_001.ttcn
│   │   │   ├── PostConditions/  # 后置条件
│   │   │   │   ├── PostConditions_SECC_15118_2.ttcn
│   │   │   │   ├── f_SECC_CMN_PO_InitialState_001.ttcn
│   │   │   │   ├── f_SECC_CMN_PO_ShutdownConfiguration_001.ttcn
│   │   │   │   └── f_SECC_PO_ShutdownConfiguration_002.ttcn
│   │   │   ├── TestBehavior/   # 测试行为
│   │   │   │   ├── f_SECC_CMN_TB_VTB_SDP_001.ttcn
│   │   │   │   ├── f_SECC_CMN_TB_VTB_SupportedAppProtocol_001.ttcn
│   │   │   │   └── f_SECC_CMN_TB_VTB_V2GTPSessionSetup_001.ttcn
│   │   │   └── Services/       # 公共服务
│   │   │       └── CommonFunctions.ttcn
│   │   │
│   │   └── 4_TestCases/        # 测试用例
│   │       └── TestCases_SECC/
│   │           ├── TestCases_SECC.ttcn
│   │           └── TC_SECC_CMN_VTB_V2GTPSessionSetup_001.ttcn
│   │
│   └── cpp_adapters/           # C++ 适配器（SA & CD）
│       ├── common/              # 公共工具
│       │   ├── Common.hh
│       │   └── Common.cc
│       ├── CD/                  # Codec Adapter
│       │   ├── exi_engine/     # EXI 编解码器
│       │   │   ├── ExiCodecAdapter.hh
│       │   │   └── ExiCodecAdapter.cc
│       │   └── titan_tci/      # TITAN TCI 实现
│       │       ├── TitanTci.hh
│       │       └── TitanTci.cc
│       └── SA/                  # System Adapter
│           ├── titan_tri/       # TITAN TRI 基类
│           │   ├── TitanTri.hh
│           │   └── TitanTri.cc
│           └── hardware/        # 硬件适配器
│               ├── HAL61851Adapter.hh
│               ├── HAL61851Adapter.cc
│               ├── TCPTLSPortAdapter.hh
│               └── TCPTLSPortAdapter.cc
│
├── build/                       # 构建输出目录
│   ├── SECC_TestSystem.hh/cc   # TITAN 生成的测试系统代码
│   ├── TestCases_SECC.hh/cc   # TITAN 生成的测试用例代码
│   ├── V2G_TCP_TLS_Port.hh/cc # TITAN 生成的端口代码
│   ├── V2G_UDP_SDP_Port.hh/cc # TITAN 生成的端口代码
│   ├── *.hh / *.cc             # 各类生成的 C++ 代码
│   └── bin/
│       └── TestExecutable       # 编译后的可执行文件
│
├── logs/                       # 日志目录
│   └── titan_traces/           # TITAN 执行日志
│       └── TestExecution.log   # 测试执行日志
│
└── README.md                   # 本文档
```

## 目录说明

### src/ttcn3/1_Data_Definitions/ - 数据定义

| 目录 | 说明 |
|------|------|
| `Types/` | V2G 消息类型、枚举、常量定义 |
| `Templates/` | TTCN-3 消息模板（发送/接收匹配模式） |
| `PICS/` | **P**rotocol **I**mplementation **C**onformance **S**tatement - 被测系统能力声明 |
| `PIXIT/` | **PI**mplementation **E**xtra **I**nformation for **T**esting - 测试环境额外配置 |

### PICS vs PIXIT 区别

| 项目 | PICS | PIXIT |
|------|------|-------|
| 全称 | Protocol Implementation Conformance Statement | Protocol Implementation eXtra Information for Testing |
| 含义 | 被测系统（SUT）支持的功能和能力 | 测试环境特定的参数和配置 |
| 示例 | 是否支持 TLS、充电模式（AC/DC）、最大电流 | SUT IP 地址、端口号、超时配置 |
| 使用者 | 定义被测系统特性 | 测试执行时的环境参数 |

### src/cpp_adapters/ - C++ 适配器

适配器分为 CD（Codec/Domain）和 SA（System Adapter）两层：

| 目录 | 说明 |
|------|------|
| `common/` | 所有适配器共享的工具函数 |
| `CD/exi_engine/` | V2G 消息的 EXI 编解码 |
| `CD/titan_tci/` | TITAN Test Control Interface 实现 |
| `SA/titan_tri/` | TITAN Test Port Interface 基类 |
| `SA/hardware/` | 具体硬件接口实现（TCP/TLS、IEC 61851） |

## 编译运行

### 前置条件

1. 安装 **Eclipse TITAN** 测试框架（版本 6.6.1 或更高）
2. 设置环境变量（见下节）
3. 安装 **GCC/G++** 编译器（推荐 GCC 9+）

### 设置环境变量

构建脚本会自动检测和设置大部分环境变量，但如果需要手动配置，或希望每次打开终端都自动生效，可在 shell 配置文件中添加以下内容。

#### 方式一：写入 shell 配置（推荐）

```bash
# ~/.bashrc 或 ~/.bash_profile 末尾添加

# TITAN 安装根目录（根据实际安装位置调整）
export TTCN3_DIR=/usr

# 子目录路径（TITAN 标准安装为 /titan，若安装在 /usr/include 则留空）
export TTCN3_SUBDIR=/titan

# 衍生路径（通常无需手动设置，构建脚本会自动推导）
export TTCN3_INC="${TTCN3_DIR}/include${TTCN3_SUBDIR}"
export TTCN3_LIB_DIR="${TTCN3_DIR}/lib${TTCN3_SUBDIR}"

# TITAN 工具链路径
export PATH="${TTCN3_DIR}/bin:${PATH}"

# 运行时动态库搜索路径
export LD_LIBRARY_PATH="${TTCN3_LIB_DIR}:${LD_LIBRARY_PATH}"
```

修改后执行 `source ~/.bashrc` 或重新打开终端使配置生效。

#### 方式二：手动设置（仅当前会话有效）

```bash
export TTCN3_DIR=/usr
export TTCN3_SUBDIR=/titan
export TTCN3_INC=/usr/include/titan
export TTCN3_LIB_DIR=/usr/lib/titan
export PATH=/usr/bin:$PATH
export LD_LIBRARY_PATH=/usr/lib/titan:$LD_LIBRARY_PATH
```

#### 方式三：Windows PowerShell

```powershell
$env:TTCN3_DIR="C:\Program Files\TITAN"
$env:TTCN3_SUBDIR="\titan"
$env:TTCN3_INC="C:\Program Files\TITAN\include\titan"
$env:TTCN3_LIB_DIR="C:\Program Files\TITAN\lib\titan"
$env:PATH="C:\Program Files\TITAN\bin;$env:PATH"
```

#### 环境变量说明

| 变量 | 说明 | 典型值 |
|------|------|--------|
| `TTCN3_DIR` | TITAN 安装根目录 | `/usr` |
| `TTCN3_SUBDIR` | include/lib 相对于 `TTCN3_DIR` 的子目录 | `/titan`（标准安装）或 `""` |
| `TTCN3_INC` | TITAN 头文件目录 | `/usr/include/titan` |
| `TTCN3_LIB_DIR` | TITAN 动态库目录 | `/usr/lib/titan` |
| `LD_LIBRARY_PATH` | 运行时动态链接器搜索路径 | `/usr/lib/titan` |

> **注意**：构建脚本会自动检测这些变量。如果系统中存在 `/usr/include/titan`，脚本会优先使用 `TTCN3_DIR=/usr` 和 `TTCN3_SUBDIR=/titan`。手动设置的值会覆盖自动检测结果。

### 安装 TITAN（Ubuntu）

**注意**：需要启用 `universe` 软件源。

```bash
# 确保启用了 universe 源
sudo add-apt-repository universe

# 安装 TITAN
sudo apt update
sudo apt install eclipse-titan
```

安装后各组件路径如下：

| 组件 | 路径 |
|------|------|
| 可执行文件 | `/usr/bin/`（`makefilegen`、`ttcn3_compiler` 等） |
| 头文件 | `/usr/include/titan/` |
| 运行时库 | `/usr/lib/titan/`（含 `libttcn3-parallel-dynamic.so`） |
| 文档 | `/usr/share/doc/eclipse-titan/` |

安装完成后按上一节设置环境变量即可。

> **为什么不推荐从源码编译？** Ubuntu apt 包已包含 TITAN 8.2.0（Ubuntu 24.04），功能完整且自动处理依赖（gcc、libedit、libssl、libxml2、libpcre、libsctp 等），无需手动配置。源码编译仅在你需要最新特性时才有必要。
> 参考：https://projects.eclipse.org/projects/tools.titan

### 编译项目

#### Linux

```bash
cd demo
chmod +x build.sh
./build.sh
```

#### Windows

```batch
cd demo
build.bat
```

构建脚本自动执行以下步骤：
1. 创建 `build/` 目录
2. 生成 `V2G_TCP_TLS_Port` 和 `V2G_UDP_SDP_Port` 端口框架代码
3. 运行 `makefilegen` 生成 Makefile
4. 编译所有 TTCN-3 和 C++ 源码
5. 整理可执行文件到 `build/bin/`

### 运行测试

```bash
# 方式一：从项目根目录运行
./build/bin/TestExecutable config/titan_runtime.cfg

# 方式二：进入 build 目录运行
cd build/bin
./TestExecutable ../config/titan_runtime.cfg

# 查看可用测试用例
./TestExecutable -l

# 查看版本信息
./TestExecutable -v
```

### 验证环境

```bash
# 确认环境变量已设置
echo "TTCN3_DIR=$TTCN3_DIR"
echo "TTCN3_SUBDIR=$TTCN3_SUBDIR"
echo "TTCN3_INC=$TTCN3_INC"

# 检查 TITAN 库是否可被找到
ldd ./build/bin/TestExecutable | grep ttcn3

# 手动指定库路径运行（如果 LD_LIBRARY_PATH 未生效）
LD_LIBRARY_PATH=/usr/lib/titan ./build/bin/TestExecutable -v
```

## 快速开始

本节介绍从零开始运行测试用例的完整流程。

### 前置条件

1. **TITAN 运行时库已安装**
   ```bash
   # 验证 TITAN 已安装
   which makefilegen ttcn3_compiler
   ```

2. **conda 环境 `iso15118` 已激活**（用于运行 SECC 模拟器）
   ```bash
   conda activate iso15118
   # 确认 iso15118-master 项目目录可访问
   cd /home/dmc/桌面/platform/iso15118-master
   ```

### Step 1: 配置 SECC 模拟器

编辑 iso15118-master 的 `.env` 文件：

```bash
# 网络接口（必须与 TITAN 测试端一致）
NETWORK_INTERFACE=ens33

# 协议和认证模式
PROTOCOLS=DIN_SPEC_70121,ISO_15118_2
AUTH_MODES=EIM

# 关闭 TLS（当前测试不支持 TLS）
SECC_ENFORCE_TLS=False
```

### Step 2: 编译项目

```bash
cd /home/dmc/projects/demo

# 确保脚本可执行
chmod +x build.sh

# 编译（会清理 build/ 目录并重新生成）
./build.sh
```

编译成功后会输出：
```
[BUILD] Build completed successfully!
```

### Step 3: 启动 SECC 模拟器（终端 1）

**如果之前已启动过 SECC，先关闭旧进程：**

```bash
# 查找运行中的 SECC 进程
ps aux | grep "python.*iso15118/secc/main.py" | grep -v grep

# 强制关闭所有 SECC 进程
pkill -f "python.*iso15118/secc/main.py"
```

**启动新的 SECC 模拟器：**

```bash
# 激活 conda 环境
conda activate iso15118

# 切换到 SECC 项目目录
cd /home/dmc/桌面/platform/iso15118-master

# 启动模拟器
poetry run python iso15118/secc/main.py
```

确认以下日志输出表示启动成功：
```
INFO  iso15118.secc.transport.udp_server: UDP server started at address FF02::1%ens33 and port 15118
INFO  iso15118.secc.controller.simulator: New Status: ready
```

### Step 4: 运行测试用例（终端 2）

```bash
cd /home/dmc/projects/demo

# 设置 TITAN 动态库路径并运行测试
LD_LIBRARY_PATH=/usr/lib/titan ./build/bin/TestExecutable config/titan_runtime.cfg
```

预期输出：
```
Test case TC_SECC_CMN_VTB_V2GTPSessionSetup_001 started.
...
Test case TC_SECC_CMN_VTB_V2GTPSessionSetup_001 finished. Verdict: pass
```

### 完整流程概览

```
终端1 (SECC 模拟器)                    终端2 (TITAN 测试)
     │                                       │
     │──── UDP server 监听 15118 ──────────▶│
     │◀──── 等待 SDP 请求 ──────────────────│
     │                                       │
     │   SDP Request ──────────────────────▶│ (UDP 多播 ff02::1:15118)
     │◀──── SDP Response ───────────────────│ (TCP 端口 XXXXX)
     │   (启动 TCP server :XXXXX)           │
     │                                       │
     │   TCP 连接 ──────────────────────────▶│ (连接到 XXXXX)
     │◀──── SupportedAppProtocolReq ─────────│
     │──── SupportedAppProtocolRes ─────────▶│
     │   (ResponseCode: OK_SuccessfulNegot...)│
     │                                       │ [Verdict: pass]
```

### 常见问题排查

| 现象 | 可能原因 | 解决方案 |
|------|---------|---------|
| `connect() failed: Invalid argument` | IPv6 scope_id 未正确设置 | 确认网络接口为 `ens33` |
| SECC 日志无响应 | TITAN 发送的 SDP 请求格式错误 | 检查 V2GTP 头 payload_type = 0x9000 |
| TCP 连接超时 | SECC TCP 服务器未就绪 | 重试或检查 SECC 是否正常启动 |
| `libttcn3-dynamic.so not found` | LD_LIBRARY_PATH 未设置 | `export LD_LIBRARY_PATH=/usr/lib/titan` |

## 配置说明

### titan_runtime.cfg

| 配置区段 | 说明 |
|---------|------|
| `[LOGGING]` | 日志输出配置，输出到 `../logs/titan_traces/TestExecution.log` |
| `[TESTPORT_PARAMETERS]` | 测试端口参数（IP、端口、设备路径） |
| `[DEFINE]` | 指定执行的测试用例（如 `TestCase := TC_SECC_CMN_VTB_V2GTPSessionSetup_001`） |
| `[EXECUTE]` | 执行控制（默认执行 `[DEFINE]` 中定义的测试用例） |
| `[CONTROL]` | 执行选项（如 `StopOnFail`、`ParallelTCs`） |
| `[PARAMETERS]` | 全局参数（SUT IP、TLS 配置、CombinedTesting 模式等） |

### hardware_env.json

| 配置项 | 说明 |
|-------|------|
| `network` | 网络接口配置（eth0、IPv6 多播地址 ff02::01、VLAN、MTU） |
| `udp_sdp` | SDP 协议配置（端口 15118、超时 2s、重试 3 次） |
| `tcp_tls` | TCP/TLS 连接参数（端口 15118、连接超时 10s、TLS 1.2） |
| `hal_61851` | IEC 61851 硬件配置（串口 /dev/ttyUSB0、波特率 115200） |
| `certificates` | TLS 证书路径（根 CA、合同证书、私钥） |
| `test_parameters` | 测试参数（EVCC ID、超时时间、CombinedTesting 模式） |

## V2G 通信流程

测试用例覆盖 ISO 15118-2 定义的高层通信流程：

```
EVCC (电动车)                              SECC (充电站)
    │                                          │
    │──── 1. SDP 请求 (UDP 多播) ──────────────▶│  SECC 发现
    │◀─── 2. SDP 响应 (SECC IP/Port) ──────────│
    │                                          │
    │──── 3. TCP 连接 + TLS 握手 ──────────────▶│  建立安全通道
    │◀─── 4. TLS 连接成功 ────────────────────│
    │                                          │
    │──── 5. SupportedAppProtocolReq ──────────▶│  协议版本协商
    │◀─── 6. SupportedAppProtocolRes ──────────│
    │                                          │
    │──── 7. SessionSetupReq ─────────────────▶│  会话建立
    │◀─── 8. SessionSetupRes ─────────────────│
    │                                          │
    │──── 9. ServiceDiscoveryReq ──────────────▶│  服务发现
    │◀─── 10. ServiceDiscoveryRes ─────────────│
    │                                          │
    │──── 11. PaymentDetailsReq ───────────────▶│  支付详情
    │◀─── 12. PaymentDetailsRes ──────────────│
    │                                          │
    │──── 13. ChargeParameterDiscoveryReq ─────▶│  充电参数
    │◀─── 14. ChargeParameterDiscoveryRes ─────│
    │                                          │
    │──── 15. PowerDeliveryReq ───────────────▶│  开始供电
    │◀─── 16. PowerDeliveryRes ───────────────│
    │                                          │
    │──── 17. ChargingStatusReq ──────────────▶│  充电状态监控
    │◀─── 18. ChargingStatusRes ─────────────│
    │         ... (重复) ...                    │
    │                                          │
    │──── 19. SessionStopReq ────────────────▶│  会话停止
    │◀─── 20. SessionStopRes ────────────────│
```

## PICS/PIXIT 配置

### PICS 配置示例

```ttcn
// 是否使用硬件在环测试
modulepar boolean PICS_CMN_CMN_CombinedTesting := false;

// 充电模式
modulepar EnergyTransferModeType PICS_CMN_CMN_ChargingMode := e_dc;

// 是否启用 TLS
modulepar boolean PICS_CMN_TLS_Enabled := true;
```

### PIXIT 配置示例

```ttcn
// 被测系统 IP 地址
modulepar charstring PIXIT_SECC_CMN_SUT_IP_Address := "ff02::01";

// 被测系统端口
modulepar integer PIXIT_SECC_CMN_SUT_Port := 15118;

// 消息超时时间
modulepar float PIXIT_SECC_CMN_Message_Timeout := 2.0;
```

## 测试用例命名规范

遵循 ISO 15118-4 规范：

```
TC_[角色]_[模式]_VTB_[消息类型]_[序号]
```

| 部分 | 说明 | 示例 |
|------|------|------|
| 角色 | SECC/EVCC | SECC |
| 模式 | CMN/AC/DC | CMN |
| 消息类型 | SDP/SupportedAppProtocol/SessionSetup 等 | SessionSetup |
| 序号 | 三位数字 | 001 |

## 扩展测试用例

### 1. 创建测试用例文件

在 `src/ttcn3/4_TestCases/TestCases_SECC/` 下创建：

```ttcn
module TC_SECC_CMN_VTB_SDP_001 {

    import from PreConditions_SECC_15118_2 all;
    import from PostConditions_SECC_15118_2 all;
    import from SECC_TestSystem all;
    import from SECC_Types all;
    import from SECC_PICS all;
    import from SECC_Constants all;
    import from SECC_CmnTemplates all;
    import from SECC_CommonFunctions all;

    testcase TC_SECC_CMN_VTB_SDP_001()
    runs on SECCTester
    system SystemSECC {

        var HAL_61851_Listener v_HAL_61851_Listener;
        var verdicttype preConVerdict;

        // 前置条件
        f_SECC_CMN_PR_InitConfiguration_001(v_HAL_61851_Listener, system);
        preConVerdict := f_SECC_CMN_PR_SupportedAppProtocol_001(v_HAL_61851_Listener);

        // 测试行为
        if (preConVerdict == pass) {
            f_SECC_CMN_TB_VTB_SDP_001(cc_hexTls, inconc);
        } else {
            log("PreCondition was unsuccessful.");
        }

        // 后置条件
        f_SECC_CMN_PO_InitialState_001(v_HAL_61851_Listener);
        f_SECC_CMN_PO_ShutdownConfiguration_001(v_HAL_61851_Listener, system);
    }
}
```

### 2. 创建测试行为函数

在 `src/ttcn3/3_Behavior_Steps/TestBehavior/` 下创建对应的行为函数。

### 3. 更新聚合模块

编辑 `TestCases_SECC.ttcn`，添加新测试用例的 import 语句。

### 4. 更新运行时配置

编辑 `config/titan_runtime.cfg`：

```cfg
[DEFINE]
TestCase := TC_SECC_CMN_VTB_SDP_001
```

### 5. 重新编译

```bash
./build.sh
```

## Multi-Agent 工作流程

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Master Agent│────▶│ SA Agent   │────▶│ CD Agent   │
│  (协调器)    │     │ (系统适配)   │     │ (编解码)    │
└──────┬──────┘     └─────────────┘     └─────────────┘
       │
       ▼
┌─────────────┐
│ TestCase    │◀─────────────────────────────┐
│ Agent       │                             │
│ (测试用例)   │─────────────────────────────┘
└─────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│           生成完整 TTCN-3 测试代码          │
│  + C++ 适配器代码                         │
└─────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────┐
│           TITAN 编译 & 执行               │
└─────────────────────────────────────────┘
```

## 故障排查

### 编译错误

| 错误 | 解决方案 |
|------|---------|
| 找不到模块 | 检查 import 语句和文件路径 |
| 类型不匹配 | 检查模板字段名与类型定义 |
| 未定义符号 | 检查函数/模板是否在正确模块定义 |

### 运行错误

| 错误 | 解决方案 |
|------|---------|
| 库文件 `libttcn3-parallel-dynamic.so not found` | 设置 `export LD_LIBRARY_PATH=/usr/lib/titan` |
| 端口映射失败 | 检查 `titan_runtime.cfg` 端口配置 |
| 连接超时 | 检查网络和被测系统状态 |
| 断言失败 | 检查消息内容和编码格式 |

### 日志查看

```bash
# 查看 TITAN 执行日志
cat logs/titan_traces/TestExecution.log

# 实时跟踪日志
tail -f logs/titan_traces/TestExecution.log
```

---

## 开发修改记录

本文档记录对 iso15118-master (SECC 模拟器) 以外的代码修改。

### 1. 端口适配器实现 (`src/cpp_adapters/SA/ports/`)

项目实现了一套自定义的 TCP/UDP 端口适配器，用于与 SECC 模拟器通信：

| 文件 | 说明 |
|------|------|
| `V2G_TCP_TLS_Port.cc/hh` | TCP 连接端口实现，使用 IPv6 socket 连接 SECC |
| `V2G_UDP_SDP_Port.cc/hh` | UDP SDP 端口实现，发送 SDP 发现请求 |

**关键实现细节：**

- SDP 请求格式（10 字节）：
  - V2GTP 头（8 字节）：`01 FE 90 00 00 00 00 02`
  - SDP 体（2 字节）：`10 00` (security=0x10=NO_TLS, transport=0x00=TCP)

- SDP 响应解析（SDP body = 20 字节，按 ISO 15118-2 标准顺序）：
  - body[0-15]: IPv6 地址（16 字节）
  - body[16-17]: 端口号（大端序，2 字节）
  - body[18]: security 字段（1 字节）
  - body[19]: transport 字段（1 字节）

- SDP 响应中的 IPv6 地址需要 scope_id（通过 `%interface` 或 `if_nametoindex`）
- TCP 端口使用 `getaddrinfo()` 解析 IPv6 link-local 地址以获取正确的 scope_id
- TCP 连接前检查端口是否需要更新（SDP 响应的端口覆盖默认的 15118）

- V2GTP 协议版本：version=1, inverse=254 (0xFE)

### 2. 构建脚本修改 (`build.sh`)

- Step 3：改为从 `src/cpp_adapters/SA/ports/` 复制端口适配器文件到 `build/generated_cpp/`
- Step 4.1：修复 TTCN-3 编译检查逻辑
- Step 6：使用 `ttcn3-dynamic` 替代 `ttcn3-parallel-dynamic`（支持单进程模式）
- Step 6：添加 `-lpthread` 到 `LINUX_LIBS`

### 3. TCP 端口适配器修复 (`V2G_TCP_TLS_Port.cc`)

2026-04-26 修复了以下问题：

- **使用 `getaddrinfo()` 解析 IPv6 link-local 地址**：直接用 `inet_pton` + `sin6_scope_id` 的方式会导致 `connect()` 返回 `EINVAL`。改用 `getaddrinfo()` 自动处理 scope_id。
- **正确添加 scope_id**：当 IP 地址不包含 `%interface` 时，需要添加 `%ens33` 以确保 `getaddrinfo` 返回正确的 `sin6_scope_id`。
- **使用 SDP 响应中的 IP 和端口**：TCP 端口从 UDP SDP 端口解析出的共享变量读取 SECC 的 IP 和端口，而不是使用配置中的硬编码值。
- **TCP 连接重试逻辑**：当 SECC 的 TCP 服务器未就绪时，自动重试最多 5 次。
- **转发完整的 V2GTP 消息**：不再构造 stub EXI payload，直接转发 TTCN-3 测试用例发送的完整消息。

### 3. 测试用例修改

**`TC_SECC_CMN_VTB_V2GTPSessionSetup_001.ttcn`**：增加了 SDP 发现流程，先发送 UDP SDP 请求触发 SECC 启动 TCP 服务器。

### 4. 配置文件修改 (`config/titan_runtime.cfg`)

- `[LOGGING]`：日志路径使用相对路径（相对于项目根目录）
- `[TESTPORT_PARAMETERS]`：配置网络接口和 SECC IP 地址
- `[EXECUTE]`：直接指定测试用例模块名

### 5. V2GTP 协议格式参考

```
V2GTP Header (8 bytes):
  [0]   Protocol Version     = 0x01
  [1]   Inverse Version     = 0xFE
  [2-3] Payload Type       (big-endian)
         - SDP_REQUEST      = 0x9000
         - SDP_RESPONSE    = 0x9001
         - EXI_ENCODED     = 0x8001
  [4-7] Payload Length     (big-endian, 4 bytes)

SDP Request Body (2 bytes):
  [0]   Security Protocol
         - TLS             = 0x00
         - NO_TLS          = 0x10
  [1]   Transport Protocol
         - TCP             = 0x00
         - UDP             = 0x10

SDP Response Body (20 bytes, ISO 15118-2 spec order):
  [0-15] IPv6 Address (16 bytes, big-endian binary)
  [16-17] TCP Port (2 bytes, big-endian)
  [18]    Security Protocol
  [19]    Transport Protocol
```
