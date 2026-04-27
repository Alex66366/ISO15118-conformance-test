# 如何将C++测试程序集成到ISO15118_TestUI项目

## 📋 前提条件

在开始之前，确保您的C++测试程序满足以下要求：

### 1. 程序输出格式要求
您的测试程序必须通过 **标准输出 (stdout)** 打印测试信息，以便系统捕获日志。

### 2. 命令行参数格式
测试程序必须接受以下命令行参数：
```bash
./runTC <testcase_name> <execution_mode> <v2g_config_path> <slac_config_path>
```

**参数说明：**
- `testcase_name`: 测试用例名称（如：`TC_SECC_CMN_VTB_Authorization_001`）
- `execution_mode`: 执行模式（固定为 `"I"`，可能表示Interactive）
- `v2g_config_path`: V2G配置文件路径（JSON格式）
- `slac_config_path`: SLAC配置文件路径（JSON格式）

### 3. 返回值约定
程序执行完成后，通过 **exit code** 返回测试结果：
- `0` → `none` - 未执行
- `1` → `pass` - 通过
- `2` → `inconclude` - 不确定
- `3` → `fail` - 失败
- `4` → `error` - 错误

### 4. 测试用例代码注释格式
每个测试用例必须在C++代码中包含特定格式的注释，系统会自动解析：

```cpp
/*
 * 1. CONDITION: <测试条件描述>
 * 
 * 2. EXPECTED: <期望结果描述>
 * 
 * 3. PICS selection: <PICS配置说明>
 * 
 * 4. PIXIT selection: <PIXIT配置说明>
 */

verdict_val TestCases_SECC_Authorization::TC_SECC_CMN_VTB_Authorization_001()
{
    // 您的测试代码
}
```

## 🗂️ 目录结构要求

将您的C++项目放在以下结构中：

```
YourHLCProject/
├── CMakeLists.txt          # CMake构建文件
├── TestCase/
│   └── src/
│       ├── TestCases_SECC_Authorization.cpp
│       ├── TestCases_SECC_Payment.cpp
│       └── ...             # 其他测试用例文件
├── build/                  # 构建目录（自动生成）
│   └── TestSuite/
│       └── runTC          # 编译后的可执行文件
└── ...                     # 其他源码文件
```

## 🔧 集成步骤

### 步骤1: 准备C++测试项目

确保您的C++项目包含CMakeLists.txt，并且能够编译生成可执行文件 `runTC`。

**示例CMakeLists.txt结构：**
```cmake
cmake_minimum_required(VERSION 3.10)
project(ISO15118_TestSuite)

set(CMAKE_CXX_STANDARD 17)

# 添加测试用例源文件
file(GLOB TESTCASE_SOURCES "TestCase/src/*.cpp")

# 添加其他必要的源文件
# ...

add_executable(runTC 
    ${TESTCASE_SOURCES}
    # 其他源文件...
)
```

### 步骤2: 解析测试用例信息并存入数据库

在Django项目根目录执行以下命令：

```bash
python manage.py initdb /path/to/YourHLCProject
```

**命令说明：**
- 该命令会：
  1. 自动构建您的C++项目（执行cmake和make）
  2. 扫描 `TestCase/src/` 目录下的所有 `.cpp` 文件
  3. 解析测试用例的注释信息（CONDITION, EXPECTED, PICS, PIXIT）
  4. 提取测试用例名称和类型（CMN/AC/DC）
  5. 将信息存入SQLite数据库

**示例：**
```bash
# Windows
python manage.py initdb D:\work\ISO15118_TestSuite

# Linux
python manage.py initdb /home/user/ISO15118_TestSuite
```

### 步骤3: 验证数据导入

检查数据库中的测试用例：

```bash
python manage.py shell
```

```python
from testcase.models import TestcaseType
# 查看所有测试用例
TestcaseType.objects.all()
# 查看特定测试用例
tc = TestcaseType.objects.get(name="TC_SECC_CMN_VTB_Authorization_001")
print(tc.condition)  # 测试条件
print(tc.expected)   # 期望结果
print(tc.path)       # 可执行文件路径
```

### 步骤4: 启动Web服务

```bash
# 启动Django后端
python manage.py runserver

# 启动前端（另一个终端）
cd frontend/testui
npm run serve
```

### 步骤5: 在Web界面中执行测试

1. 打开浏览器访问：`http://localhost:8081`
2. 在测试用例列表中选择要执行的测试用例
3. 配置V2G和SLAC参数（使用JSON Schema表单）
4. 点击"执行"按钮
5. 查看实时日志和测试结果

## 🔍 系统执行流程

当您在Web界面点击"执行"时，系统执行以下流程：

```
1. 前端发送POST请求到 /api/testcase/{id}/execute/
   ↓
2. Django后端 TestcaseExecute.update() 处理请求
   ↓
3. 解析配置参数（timer, pics, pixit, slac）
   ↓
4. 将配置保存到 /tmp/v2g_config.json 和 /tmp/slac_config.json
   ↓
5. 创建 ExecTestcase 线程对象
   ↓
6. 调用 subprocess.Popen 执行：
   ./runTC <testcase_name> I /tmp/v2g_config.json /tmp/slac_config.json
   ↓
7. 实时捕获 stdout 输出
   ↓
8. 通过 WebSocket 推送到前端显示
   ↓
9. 程序执行完成后，根据 exit code 更新测试状态
```

## 📝 配置文件格式示例

您的测试程序需要读取JSON格式的配置文件：

**v2g_config.json:**
```json
{
    "timer": {
        "par_V2G_EVCC_Msg_Timeout_AuthorizationReq": 2,
        "par_V2G_EVCC_Ongoing_Timeout": 60,
        ...
    },
    "pics": {
        "PICS_CMN_CMN_ChargingMode": 1,
        "PICS_SECC_CMN_Pause": false,
        ...
    },
    "pixit": {
        "PIXIT_CMN_CMN_WakeUp": 200,
        "PIXIT_SECC_CMN_TLS": false,
        ...
    }
}
```

**slac_config.json:**
```json
{
    "timer": {
        "par_T_step": 3,
        ...
    },
    "threshold": {
        "C_min": 10,
        ...
    }
}
```

## ⚠️ 常见问题

### Q1: 初始化数据库时出现"cmake not found"错误
**A:** 需要安装CMake：
```bash
# Windows
winget install Kitware.CMake

# Linux
sudo apt-get install cmake
```

### Q2: 测试用例没有自动解析
**A:** 检查C++文件中的注释格式是否正确，确保包含所有必要的PREFIX_LIST标记。

### Q3: 执行测试时找不到runTC文件
**A:** 确保CMake编译成功，并且可执行文件位于 `<project_path>/build/TestSuite/runTC`

### Q4: WebSocket连接失败
**A:** 确保Redis服务正在运行：
```bash
# Linux
sudo systemctl start redis

# Windows (使用WSL或Docker)
```

## 🎯 快速开始模板

如果您想快速验证集成，可以创建一个最小的测试程序：

**main.cpp:**
```cpp
#include <iostream>
#include <fstream>
#include <json/json.h>  // 使用jsoncpp库
#include <cstdlib>

int main(int argc, char* argv[]) {
    if (argc < 5) {
        std::cerr << "Usage: " << argv[0] 
                  << " <testcase_name> <mode> <v2g_config> <slac_config>" 
                  << std::endl;
        return 4; // error
    }
    
    std::string testcase_name = argv[1];
    std::string v2g_config = argv[3];
    std::string slac_config = argv[4];
    
    std::cout << "Starting test: " << testcase_name << std::endl;
    std::cout << "V2G Config: " << v2g_config << std::endl;
    std::cout << "SLAC Config: " << slac_config << std::endl;
    
    // 读取配置文件
    std::ifstream v2g_file(v2g_config);
    std::ifstream slac_file(slac_config);
    
    // 您的测试逻辑...
    
    std::cout << "Test completed" << std::endl;
    
    return 1; // pass (可以根据实际情况返回不同值)
}
```

**CMakeLists.txt:**
```cmake
cmake_minimum_required(VERSION 3.10)
project(SimpleTestSuite)

set(CMAKE_CXX_STANDARD 17)

add_executable(runTC main.cpp)
```

将此项目放在符合目录结构的文件夹中，然后运行 `python manage.py initdb <path>` 即可开始测试！

