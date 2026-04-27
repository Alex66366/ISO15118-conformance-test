@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo [启动] ISO15118 TestUI (Windows)
echo ========================================
echo.

REM 获取脚本所在目录
set "SCRIPT_DIR=%~dp0"
REM 移除末尾反斜杠（如果存在）
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
cd /d "%SCRIPT_DIR%"

REM 检查conda是否可用
where conda >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到conda命令
    echo 请确保已安装Anaconda/Miniconda，并在Anaconda Prompt中运行此脚本
    pause
    exit /b 1
)

REM 检查环境是否存在
echo [检查] conda环境...
conda env list | findstr /C:"iso15118_testui" >nul 2>&1
if %errorlevel% neq 0 (
    echo [警告] 环境不存在，正在创建...
    call conda create -n iso15118_testui python=3.9 -y
    if %errorlevel% neq 0 (
        echo [错误] 创建环境失败
        pause
        exit /b 1
    )
    echo [安装] Python依赖包...
    call conda run -n iso15118_testui pip install -r requirement.txt
    if %errorlevel% neq 0 (
        echo [错误] 安装依赖失败
        pause
        exit /b 1
    )
) else (
    REM 检查依赖是否已安装
    echo [检查] Python依赖...
    call conda run -n iso15118_testui pip show Django >nul 2>&1
    if %errorlevel% neq 0 (
        echo [警告] 检测到依赖未安装，正在安装...
        call conda run -n iso15118_testui pip install -r requirement.txt
        if %errorlevel% neq 0 (
            echo [错误] 安装依赖失败
            pause
            exit /b 1
        )
    )
)

REM 检查并初始化数据库
echo.
echo [检查] 数据库状态...
if not exist "db.sqlite3" (
    echo [初始化] 数据库不存在，正在创建...
    call conda run -n iso15118_testui python manage.py migrate
    if %errorlevel% neq 0 (
        echo [错误] 数据库迁移失败
        pause
        exit /b 1
    )
) else (
    echo [检查] 数据库已存在，跳过初始化
)

REM 启动后端（新窗口）
echo.
echo [启动] Django后端服务器...
REM 创建临时批处理文件启动后端
echo @echo off > "%TEMP%\start_backend_iso15118.bat"
echo call conda activate iso15118_testui >> "%TEMP%\start_backend_iso15118.bat"
echo cd /d "%SCRIPT_DIR%" >> "%TEMP%\start_backend_iso15118.bat"
echo python manage.py runserver 127.0.0.1:8000 >> "%TEMP%\start_backend_iso15118.bat"
start "ISO15118 Backend - Django" cmd /k "%TEMP%\start_backend_iso15118.bat"

REM 等待3秒让后端启动
timeout /t 3 /nobreak >nul

REM 检查前端依赖
if not exist "frontend\testui\node_modules" (
    echo.
    echo [安装] 检测到前端依赖未安装，正在安装...
    echo        这可能需要几分钟，请耐心等待...
    cd frontend\testui
    call npm install --legacy-peer-deps
    if %errorlevel% neq 0 (
        echo [错误] 前端依赖安装失败
        pause
        exit /b 1
    )
    cd ..\..
)

REM 启动前端（新窗口）
echo.
echo [启动] Vue前端服务器...
echo [提示] 前端服务器启动中，请稍候...
REM 创建临时批处理文件启动前端
echo @echo off > "%TEMP%\start_frontend_iso15118.bat"
echo cd /d "%SCRIPT_DIR%\frontend\testui" >> "%TEMP%\start_frontend_iso15118.bat"
echo echo [前端服务器] 正在启动... >> "%TEMP%\start_frontend_iso15118.bat"
echo npm run serve >> "%TEMP%\start_frontend_iso15118.bat"
start "ISO15118 Frontend - Vue" cmd /k "%TEMP%\start_frontend_iso15118.bat"

REM 等待前端启动
timeout /t 5 /nobreak >nul

echo.
echo ========================================
echo [完成] 服务已启动！
echo ========================================
echo.
echo [访问地址]
echo 前端界面: http://localhost:8080
echo 后端API:  http://localhost:8000
echo Admin管理: http://localhost:8000/admin
echo.
echo [重要提示]
echo - 如果无法访问前端，请检查新打开的前端窗口是否有错误信息
echo - 前端服务器启动可能需要10-30秒，请耐心等待
echo - 确保防火墙没有阻止8080端口
echo - 关闭服务请直接关闭对应的命令窗口，或按 Ctrl+C 停止服务
echo.
echo [检查服务状态]
echo - 后端窗口标题: ISO15118 Backend - Django
echo - 前端窗口标题: ISO15118 Frontend - Vue
echo.
echo 按任意键关闭此窗口（服务将继续运行）...
pause >nul

