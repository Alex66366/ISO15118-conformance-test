# 简单测试程序示例

这是一个最小的示例，展示如何编写符合TestUI接口要求的C++测试程序。

## 文件说明

- `simple_test_example.cpp` - 简单的测试程序示例
- `CMakeLists.txt` - CMake构建文件

## 编译和运行

### Linux/Mac

```bash
# 创建构建目录
mkdir build && cd build

# 配置和编译
cmake ..
make

# 运行测试（需要先准备配置文件）
./runTC TC_SIMPLE_TEST I /tmp/v2g_config.json /tmp/slac_config.json
```

### Windows

```bash
# 使用Visual Studio
mkdir build && cd build
cmake .. -G "Visual Studio 16 2019"
cmake --build .

# 运行
.\runTC.exe TC_SIMPLE_TEST I v2g_config.json slac_config.json
```

## 测试程序要求

1. **命令行参数**: 接受4个参数（测试用例名、模式、V2G配置、SLAC配置）
2. **标准输出**: 所有日志输出到stdout
3. **退出码**: 返回0-4表示不同的测试结果
4. **配置文件**: 读取JSON格式的配置文件

## 与TestUI集成

将这个示例程序集成到TestUI：

```bash
# 假设您的示例在 examples/ 目录
cd ISO15118_TestUI-master

# 方案A: 如果是完整项目结构
python manage.py initdb /path/to/examples

# 方案B: 手动创建测试用例
python manage.py shell
>>> from testcase.models import TestcaseType
>>> tc = TestcaseType.objects.create(
...     name="TC_SIMPLE_TEST",
...     type="CMN",
...     status="n",
...     build_status="OK",
...     condition="简单测试条件",
...     expected="测试通过",
...     pics="{}",
...     pixit="{}",
...     path="/path/to/build/runTC"
... )
```

## 下一步

根据您的实际需求，在示例代码中添加：
- ISO15118协议栈集成
- 实际的网络通信
- 消息编解码
- 测试验证逻辑

