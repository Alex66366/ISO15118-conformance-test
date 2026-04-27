# 🪟 Windows + Conda 快速启动指南

本指南将帮助您在Windows系统上使用Conda环境快速启动项目，仅用于本地查看前端界面。

---

## 📋 前提条件

- ✅ Windows 10/11
- ✅ 已安装 Anaconda 或 Miniconda
- ✅ 已安装 Node.js 14+ 和 npm
- ✅ 已安装 Git（可选，用于克隆项目）

---

## 🔧 步骤1: 准备Conda环境

### 1.1 打开Anaconda Prompt或PowerShell

**方法1：** 在开始菜单搜索"Anaconda Prompt"并打开  
**方法2：** 在PowerShell中手动激活conda

### 1.2 进入项目目录

```powershell
# 切换到项目目录（根据您的实际路径修改）
cd D:\thu_conformance_test\电动车计费系统安全_new\协议方面\iso15118\平台开发\ISO15118_TestUI-master
```

### 1.3 创建Conda虚拟环境

```powershell
# 创建Python 3.9环境（推荐）
conda create -n iso15118_testui python=3.9 -y

# 激活环境
conda activate iso15118_testui
```

**提示：** 激活后，命令提示符前会显示 `(iso15118_testui)`

---

## 📦 步骤2: 安装Python依赖

### 2.1 升级pip（重要）

```powershell
python -m pip install --upgrade pip
```

### 2.2 安装项目依赖

```powershell
pip install -r requirement.txt
```

**注意：** 
- 首次安装可能需要5-10分钟
- 如果遇到编译错误，可能需要安装Visual Studio Build Tools
- 某些包（如cryptography）可能需要较长时间编译

### 2.3 验证安装

```powershell
python -c "import django; print(django.get_version())"
# 应该显示: 3.2.4
```

---

## 🗄️ 步骤3: 初始化数据库

### 3.1 创建数据库文件

```powershell
python manage.py migrate
```

**如果首次运行，会创建 `db.sqlite3` 文件**

### 3.2 创建超级用户（可选）

```powershell
python manage.py createsuperuser
```

**按照提示输入：**
- Username: `admin`
- Email: （可选，直接回车）
- Password: `admin`（或您喜欢的密码）

---

## 🌐 步骤4: 修改前端配置（重要！）

为了在本地运行，需要修改前端的API地址。

### 4.1 修改API地址

**文件：** `frontend/testui/src/store/api/ajax.js`

**将第8行：**
```javascript
var SERVER_BASE_URL = 'http://192.168.0.100:8000/api/v1'
```

**改为：**
```javascript
var SERVER_BASE_URL = 'http://localhost:8000/api/v1'
```

### 4.2 修改WebSocket地址

**文件：** `frontend/testui/src/services/logging_socket.js`

**将第4行：**
```javascript
const socket = new WebSocket(`${ws_scheme}://192.168.0.100:8000/ws/sockets/`)
```

**改为：**
```javascript
const socket = new WebSocket(`${ws_scheme}://localhost:8000/ws/sockets/`)
```

---

## 🐍 步骤5: 启动后端服务

### 5.1 设置环境变量（可选但推荐）

```powershell
# 设置DEBUG模式
$env:DEBUG = "True"

# 验证设置
echo $env:DEBUG
```

### 5.2 启动Django后端

**在一个新的Anaconda Prompt窗口中：**

```powershell
# 进入项目目录
cd D:\thu_conformance_test\电动车计费系统安全_new\协议方面\iso15118\平台开发\ISO15118_TestUI-master

# 激活conda环境
conda activate iso15118_testui

# 启动服务器
python manage.py runserver 127.0.0.1:8000
```

**成功启动后会显示：**
```
System check identified no issues (0 silenced).
Django version 3.2.4, using settings 'testui.settings'
Starting development server at http://127.0.0.1:8000/
Quit the server with CONTROL-C.
```

**🎉 后端已启动！保持这个窗口打开。**

---

## 💻 步骤6: 安装前端依赖

### 6.1 打开新的PowerShell窗口

**在另一个PowerShell或Anaconda Prompt中：**

```powershell
# 进入前端目录
cd D:\thu_conformance_test\电动车计费系统安全_new\协议方面\iso15118\平台开发\ISO15118_TestUI-master\frontend\testui
```

### 6.2 安装Node.js依赖

```powershell
npm install
```

**注意：**
- 首次安装可能需要3-5分钟
- 如果很慢，可以考虑使用国内镜像：
  ```powershell
  npm config set registry https://registry.npmmirror.com
  npm install
  ```

---

## 🎨 步骤7: 启动前端服务

### 7.1 启动Vue开发服务器

**在前端目录的PowerShell中：**

```powershell
npm run serve
```

**成功启动后会显示：**
```
App running at:
- Local:   http://localhost:8080/
- Network: http://192.168.x.x:8080/
```

**🎉 前端已启动！保持这个窗口打开。**

---

## 🌍 步骤8: 在浏览器中访问

### 8.1 打开浏览器

访问：**http://localhost:8080**

### 8.2 查看前端界面

您应该能看到：
- 🏠 **主页（Home）** - 项目介绍页面
- 📋 **测试用例列表（Testcases）** - 显示所有测试用例
- ➕ **新建测试用例（New）** - 创建新测试用例的表单
- ⚙️ **执行测试（Execute）** - 测试用例执行页面

**注意：** 由于后端API和WebSocket连接到 `localhost:8000`，您需要确保后端服务正在运行。

---

## 🛑 停止服务

当您想停止服务时：

1. **停止前端：** 在运行 `npm run serve` 的窗口按 `Ctrl+C`
2. **停止后端：** 在运行 `python manage.py runserver` 的窗口按 `Ctrl+C`
3. **退出Conda环境：** 输入 `conda deactivate`

---

## ⚠️ 常见问题与解决方案

### Q1: Redis连接失败（仅查看前端可忽略）

**错误信息：** `ConnectionError: Error 10061 connecting to 127.0.0.1:6379`

**说明：** 如果只是查看前端样式，可以暂时忽略。WebSocket功能需要Redis，但不影响基本页面显示。

**解决方法（如需要完整功能）：**
1. 下载Redis for Windows: https://github.com/microsoftarchive/redis/releases
2. 解压并运行 `redis-server.exe`
3. 或使用WSL安装Redis

### Q2: 端口已被占用

**错误信息：** `Error: That port is already in use`

**解决方法：**

**查找占用端口的进程：**
```powershell
# 查找占用8000端口的进程
netstat -ano | findstr :8000

# 查找占用8080端口的进程
netstat -ano | findstr :8080
```

**杀死进程：**
```powershell
# 替换<PID>为实际进程ID
taskkill /PID <PID> /F
```

**或使用不同端口：**
```powershell
# 后端改端口
python manage.py runserver 127.0.0.1:8001

# 前端改端口（需要修改ajax.js中的端口号）
npm run serve -- --port 8081
```

### Q3: npm install失败

**错误信息：** `npm ERR! code EACCES` 或 `npm ERR! code ENOTEMPTY`

**解决方法：**
```powershell
# 清理npm缓存
npm cache clean --force

# 删除node_modules和package-lock.json
Remove-Item -Recurse -Force node_modules
Remove-Item -Force package-lock.json

# 重新安装
npm install
```

### Q4: Python依赖安装失败（cryptography等）

**错误信息：** `Failed building wheel for cryptography`

**解决方法：**
```powershell
# 安装Visual Studio Build Tools（如果还没安装）
# 下载地址：https://visualstudio.microsoft.com/downloads/
# 安装时选择"使用C++的桌面开发"

# 或尝试使用预编译的wheel
pip install --upgrade pip setuptools wheel
pip install cryptography --only-binary :all:

# 如果还是失败，可以使用conda安装
conda install -c conda-forge cryptography
```

### Q5: 前端页面显示空白或API错误

**解决方法：**
1. 检查后端是否正在运行（http://localhost:8000）
2. 打开浏览器开发者工具（F12），查看Console标签的错误信息
3. 检查 `frontend/testui/src/store/api/ajax.js` 中的地址是否正确改为 `localhost`
4. 检查网络标签，查看API请求是否成功

### Q6: ModuleNotFoundError

**错误信息：** `ModuleNotFoundError: No module named 'xxx'`

**解决方法：**
```powershell
# 确保conda环境已激活
conda activate iso15118_testui

# 重新安装依赖
pip install -r requirement.txt
```

### Q7: 路径中包含中文导致问题

**如果项目路径包含中文字符（如"电动车计费系统安全_new"）：**

**解决方法：**
1. 确保PowerShell使用UTF-8编码：
   ```powershell
   [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
   chcp 65001
   ```
2. 或考虑将项目移到不包含中文的路径

---

## 📝 快速命令总结

### 一键启动脚本（创建 start_windows.bat）

**在项目根目录创建 `start_windows.bat` 文件：**

```batch
@echo off
chcp 65001 >nul
echo 🚀 启动 ISO15118 TestUI...

REM 激活conda环境
call conda activate iso15118_testui

REM 启动后端（新窗口）
start "Django Backend" cmd /k "conda activate iso15118_testui && cd /d %~dp0 && python manage.py runserver 127.0.0.1:8000"

REM 等待3秒
timeout /t 3 /nobreak >nul

REM 启动前端（新窗口）
start "Vue Frontend" cmd /k "cd /d %~dp0frontend\testui && npm run serve"

echo.
echo ✅ 服务已启动！
echo 📍 前端: http://localhost:8080
echo 📍 后端: http://localhost:8000
echo.
echo 按任意键关闭此窗口（服务将继续运行）...
pause >nul
```

**使用方法：**
- 双击 `start_windows.bat` 文件
- 或右键选择"以管理员身份运行"（如果需要）

---

## 🔄 每次启动的简化流程

### 方式1: 使用批处理脚本（推荐）

直接双击 `start_windows.bat`

### 方式2: 手动启动

**终端1（后端）：**
```powershell
conda activate iso15118_testui
cd D:\thu_conformance_test\电动车计费系统安全_new\协议方面\iso15118\平台开发\ISO15118_TestUI-master
python manage.py runserver 127.0.0.1:8000
```

**终端2（前端）：**
```powershell
cd D:\thu_conformance_test\电动车计费系统安全_new\协议方面\iso15118\平台开发\ISO15118_TestUI-master\frontend\testui
npm run serve
```

---

## 📊 环境检查清单

启动前检查：

- [ ] Conda环境已创建并激活
- [ ] Python依赖已安装
- [ ] 数据库已初始化（db.sqlite3存在）
- [ ] 前端依赖已安装（node_modules存在）
- [ ] API地址已修改为localhost
- [ ] WebSocket地址已修改为localhost
- [ ] 端口8000和8080未被占用

---

## 🎉 完成！

现在您应该可以在浏览器中查看前端界面了！

**访问地址：** http://localhost:8080

**如需停止服务：** 关闭对应的命令窗口，或按 `Ctrl+C`

---

## 📞 获取帮助

如果遇到问题：
1. 检查终端错误信息
2. 查看浏览器控制台（F12 → Console）
3. 确保所有依赖都已正确安装
4. 参考上面的常见问题部分
5. 检查防火墙是否阻止了端口访问

**祝您使用愉快！** 🚀

