# C++测试程序集成 - 两种方式对比

## 🎯 方案对比

| 特性 | 方案A: 自动化集成 | 方案B: 手动配置 |
|------|------------------|----------------|
| **初始化** | 自动扫描C++源码 | 手动创建测试用例 |
| **构建** | 自动CMake编译 | 手动编译 |
| **解析** | 自动解析注释 | 手动填写信息 |
| **适用场景** | 完整的HLC项目 | 简单的独立测试程序 |
| **复杂度** | 中等 | 低 |

---

## 📘 方案A: 自动化集成（推荐用于完整项目）

### 前提条件
- 您的C++项目遵循特定的目录结构
- 包含CMakeLists.txt
- 测试用例注释符合指定格式

### 步骤

#### 1. 准备C++项目结构
```
YourHLCProject/
├── CMakeLists.txt
├── TestCase/
│   └── src/
│       ├── TestCases_SECC_Authorization.cpp
│       └── TestCases_SECC_Payment.cpp
├── src/          # 您的其他源代码
├── include/      # 头文件
└── build/        # 构建目录（自动生成）
```

#### 2. 测试用例文件格式示例

**TestCases_SECC_Authorization.cpp:**
```cpp
#include <iostream>

/*
 * 1. CONDITION: EV connects to SECC and begins authorization process
 * 
 * 2. EXPECTED: SECC should respond with proper authorization result
 * 
 * 3. PICS selection: PICS_CMN_CMN_IdentificationMode = 1
 * 
 * 4. PIXIT selection: PIXIT_SECC_CMN_AuthorizationOngoing = 1
 */

// 测试用例命名格式：TC_SECC_{类型}_VTB_{消息名}_{序号}
verdict_val TestCases_SECC_Authorization::TC_SECC_CMN_VTB_Authorization_001()
{
    std::cout << "Executing TC_SECC_CMN_VTB_Authorization_001" << std::endl;
    
    // 读取配置文件
    // 执行测试逻辑
    // 返回测试结果
    
    return 1; // pass
}
```

#### 3. CMakeLists.txt示例
```cmake
cmake_minimum_required(VERSION 3.10)
project(ISO15118_HLC_TestSuite)

set(CMAKE_CXX_STANDARD 17)

# 包含目录
include_directories(include)

# 测试用例源文件
file(GLOB_RECURSE TESTCASE_SOURCES "TestCase/src/*.cpp")

# 其他源文件
file(GLOB_RECURSE SOURCES "src/*.cpp")

# 创建可执行文件
add_executable(runTC 
    ${TESTCASE_SOURCES}
    ${SOURCES}
    main.cpp
)

# 链接库
target_link_libraries(runTC 
    # 添加您需要的库，例如：
    # jsoncpp
    # pthread
    # ISO15118协议库
)
```

#### 4. 执行初始化命令
```bash
cd ISO15118_TestUI-master
python manage.py initdb /path/to/YourHLCProject
```

**该命令会：**
- 自动构建项目（cmake + make）
- 扫描所有测试用例文件
- 解析注释中的元数据
- 将测试用例导入数据库

---

## 📗 方案B: 手动配置（推荐用于快速测试）

### 适用场景
- 已编译好的独立测试程序
- 不需要复杂的项目管理
- 测试用例数量较少

### 步骤

#### 1. 准备可执行文件
将您的测试程序编译好，假设名为 `my_test_program.exe`（Windows）或 `my_test_program`（Linux）

#### 2. 通过Django Admin或API手动创建测试用例

**方式A: 使用Django Admin界面**
```bash
# 启动Django服务
python manage.py runserver

# 在浏览器访问 http://localhost:8000/admin
# 使用管理员账号登录（如：admin / admin-Test123456@）
# 添加测试用例
```

**方式B: 使用Python脚本**
```python
# create_testcase.py
from testcase.models import TestcaseType

tc = TestcaseType.objects.create(
    name="TC_SECC_CMN_VTB_Authorization_001",
    type="CMN",
    status="n",
    build_status="OK",
    condition="EV connects to SECC and begins authorization process",
    expected="SECC should respond with proper authorization result",
    pics='{"PICS_CMN_CMN_IdentificationMode": 1}',
    pixit='{"PIXIT_SECC_CMN_AuthorizationOngoing": 1}',
    path="/path/to/my_test_program"
)
print(f"Created testcase: {tc.name}")
```

**方式C: 使用REST API**
```bash
curl -X POST http://localhost:8000/api/testcase/ \
  -H "Content-Type: application/json" \
  -d '{
    "name": "TC_SECC_CMN_VTB_Authorization_001",
    "type": "CMN",
    "status": "n",
    "build_status": "OK",
    "condition": "EV connects to SECC...",
    "expected": "SECC should respond...",
    "pics": "{}",
    "pixit": "{}",
    "path": "/path/to/my_test_program"
  }'
```

#### 3. 测试程序接口要求

您的测试程序必须：

**a) 接受命令行参数：**
```cpp
int main(int argc, char* argv[]) {
    if (argc < 5) {
        std::cerr << "参数不足" << std::endl;
        return 4; // error
    }
    
    std::string testcase_name = argv[1];  // 测试用例名称
    std::string mode = argv[2];           // 执行模式（通常是"I"）
    std::string v2g_config = argv[3];     // V2G配置文件路径
    std::string slac_config = argv[4];    // SLAC配置文件路径
    
    // 读取配置
    // 执行测试
    // 输出日志到stdout
    
    return 1; // 根据测试结果返回0-4
}
```

**b) 通过stdout输出日志：**
```cpp
std::cout << "正在执行测试..." << std::endl;
std::cout << "连接充电桩..." << std::endl;
std::cout << "发送AuthorizationReq消息..." << std::endl;
std::cout << "测试完成：通过" << std::endl;
```

**c) 返回exit code：**
```cpp
// 0 = none (未执行)
// 1 = pass (通过)
// 2 = inconclude (不确定)
// 3 = fail (失败)
// 4 = error (错误)
return 1; // 假设测试通过
```

---

## 🔧 实际集成案例

### 案例1: 使用方案A（完整项目）

假设您有一个ISO15118测试套件项目：

```bash
# 1. 克隆或准备您的C++项目
git clone https://github.com/your-org/iso15118-testsuite.git
cd iso15118-testsuite

# 2. 确保项目结构正确
ls -la
# 应该看到：CMakeLists.txt, TestCase/src/, 等

# 3. 在TestUI项目中导入
cd /path/to/ISO15118_TestUI-master
python manage.py initdb /path/to/iso15118-testsuite

# 4. 检查导入结果
python manage.py shell
>>> from testcase.models import TestcaseType
>>> TestcaseType.objects.count()  # 应该看到测试用例数量
```

### 案例2: 使用方案B（快速集成）

假设您只有一个简单的测试程序：

```bash
# 1. 编译您的测试程序
g++ -o my_test my_test.cpp -ljsoncpp -lpthread

# 2. 创建测试用例（使用Python脚本）
python << EOF
from testcase.models import TestcaseType
import os

# 获取绝对路径
program_path = os.path.abspath("my_test")

tc = TestcaseType.objects.create(
    name="MySimpleTest_001",
    type="CMN",
    status="n",
    build_status="OK",
    condition="简单测试条件",
    expected="期望结果",
    pics="{}",
    pixit="{}",
    path=program_path
)
print(f"创建成功: {tc.name}")
EOF

# 3. 启动服务测试
python manage.py runserver
# 打开浏览器访问 http://localhost:8000
```

---

## ⚡ 快速验证清单

集成完成后，检查以下项目：

- [ ] 测试程序可以独立运行
- [ ] 程序接受4个命令行参数
- [ ] 程序输出日志到stdout
- [ ] 程序返回正确的exit code
- [ ] Django数据库中可以看到测试用例
- [ ] Web界面可以显示测试用例列表
- [ ] 点击"执行"可以启动测试
- [ ] WebSocket可以实时显示日志
- [ ] 测试完成后状态正确更新

---

## 🆘 故障排除

### 问题1: "找不到runTC文件"
**检查：**
- build目录是否生成
- CMake编译是否成功
- runTC文件是否有执行权限（Linux）

**解决：**
```bash
# Linux
chmod +x /path/to/build/TestSuite/runTC

# 检查文件是否存在
ls -la /path/to/build/TestSuite/runTC
```

### 问题2: "测试用例没有在Web界面显示"
**检查：**
- 数据库中是否有数据
- build_status是否为"OK"

**解决：**
```python
python manage.py shell
>>> from testcase.models import TestcaseType
>>> for tc in TestcaseType.objects.all():
...     print(tc.name, tc.build_status)
```

### 问题3: "WebSocket连接失败"
**检查：**
- Redis是否运行
- Django Channels配置是否正确

**解决：**
```bash
# Linux
sudo systemctl start redis
redis-cli ping  # 应该返回 PONG

# Windows (使用WSL)
wsl sudo service redis start
```

---

## 📞 下一步

成功集成后，您可以：
1. 在Web界面配置测试参数
2. 批量执行测试用例
3. 查看测试结果和日志
4. 导出测试报告

祝您集成顺利！🚀

