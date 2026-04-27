cmake_minimum_required(VERSION 3.10)
project(SimpleISO15118Test)

set(CMAKE_CXX_STANDARD 17)

# 编译测试程序
add_executable(runTC 
    simple_test_example.cpp
)

# 如果需要链接其他库，取消下面的注释：
# find_package(JSONCPP REQUIRED)
# target_link_libraries(runTC jsoncpp_lib)

# 如果需要多线程支持：
# target_link_libraries(runTC pthread)

# 编译命令：
# mkdir build && cd build
# cmake ..
# make
# 生成的可执行文件在 build/runTC

