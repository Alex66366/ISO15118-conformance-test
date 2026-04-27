# 🚀 Ubuntu快速启动指南

本指南将帮助您在Ubuntu系统上快速启动项目，仅用于本地查看前端界面。

---

## 📋 前提条件

- Ubuntu 18.04 或更高版本
- 已安装 Python 3.8+
- 已安装 Node.js 14+ 和 npm
- 至少 2GB 可用磁盘空间

---

## 🔧 步骤1: 安装系统依赖

### 1.1 更新系统包
```bash
sudo apt-get update
sudo apt-get upgrade -y
```

### 1.2 安装Python环境（如果没有）
```bash
sudo apt-get install -y python3 python3-pip python3-venv
```

### 1.3 安装Node.js和npm（如果没有）
```bash
# 方法1: 使用apt安装
sudo apt-get install -y nodejs npm

# 方法2: 使用nvm安装最新版（推荐）
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.0/install.sh | bash
source ~/.bashrc
nvm install 14
nvm use 14
```

### 1.4 安装Redis（用于WebSocket）
```bash
sudo apt-get install -y redis-server

# 启动Redis服务
sudo systemctl start redis
sudo systemctl enable redis

# 验证Redis是否运行
redis-cli ping
# 应该返回: PONG
```

---

## 📦 步骤2: 准备项目

### 2.1 进入项目目录
```bash
cd /path/to/ISO15118_TestUI-master
```

### 2.2 创建Python虚拟环境
```bash
python3 -m venv venv
```

### 2.3 激活虚拟环境
```bash
source venv/bin/activate
```

**提示：** 激活后终端提示符前会显示 `(venv)`

---

## 🐍 步骤3: 安装Python依赖

```bash
pip install -r requirement.txt
```

**如果遇到权限问题：**
```bash
pip install --user -r requirement.txt
```

**安装过程可能需要几分钟，请耐心等待。**

---

## 🗄️ 步骤4: 初始化数据库

### 4.1 运行数据库迁移
```bash
python manage.py migrate
```

### 4.2 创建超级用户（可选，用于访问Django Admin）
```bash
python manage.py createsuperuser
```

**按照提示输入用户名、邮箱和密码。**

---

## 🌐 步骤5: 启动后端服务

### 5.1 启动Django后端

在一个终端窗口中：

```bash
# 确保虚拟环境已激活
source venv/bin/activate

# 启动开发服务器
python manage.py runserver 0.0.0.0:8000
```

**成功启动后会显示：**
```
System check identified no issues (0 silenced).
Django version 3.2.4, using settings 'testui.settings'
Starting development server at http://0.0.0.0:8000/
Quit the server with CONTROL-C.
```

**🎉 后端已启动！**

**保持这个终端窗口打开。**

---

## 💻 步骤6: 安装前端依赖

### 6.1 安装Node.js依赖

打开**新的终端窗口**：

```bash
cd /path/to/ISO15118_TestUI-master/frontend/testui

# 安装依赖（这可能需要几分钟）
npm install
```

**首次安装可能会比较慢，请耐心等待。**

---

## 🎨 步骤7: 启动前端服务

### 7.1 启动Vue开发服务器

在**同一个前端目录的终端**中：

```bash
npm run serve
```

**成功启动后会显示：**
```
App running at:
- Local:   http://localhost:8080/
- Network: http://192.168.x.x:8080/
```

**🎉 前端已启动！**

---

## 🌍 步骤8: 在浏览器中访问

### 8.1 打开浏览器

访问：**http://localhost:8080**

或者：**http://127.0.0.1:8080**

### 8.2 前端界面

您应该能看到：
- 🏠 主页（Home）
- 📋 测试用例列表（Testcase List）
- ➕ 创建新测试用例（New Testcase）
- ⚙️ 测试用例详情和执行页面

---

## 🛑 停止服务

当您想停止服务时：

1. **停止前端：** 在运行 `npm run serve` 的终端按 `Ctrl+C`
2. **停止后端：** 在运行 `python manage.py runserver` 的终端按 `Ctrl+C`
3. **退出虚拟环境：** 输入 `deactivate`

---

## ⚠️ 常见问题

### Q1: Redis连接失败
**错误信息：** `ConnectionError: Error 10061 connecting to 127.0.0.1:6379`

**解决方法：**
```bash
# 检查Redis是否运行
sudo systemctl status redis

# 如果没有运行，启动它
sudo systemctl start redis

# 确保Redis监听本地接口
sudo nano /etc/redis/redis.conf
# 找到 bind 127.0.0.1 确保不是注释的
```

### Q2: 端口已被占用
**错误信息：** `Error: That port is already in use`

**解决方法：**
```bash
# 查找占用端口的进程
sudo lsof -i :8000  # 后端端口
sudo lsof -i :8080  # 前端端口

# 杀死进程
sudo kill -9 <PID>

# 或者使用不同的端口
python manage.py runserver 0.0.0.0:8001  # 后端改端口
npm run serve -- --port 8081  # 前端改端口
```

### Q3: npm install失败
**错误信息：** `npm ERR! code EACCES`

**解决方法：**
```bash
# 清理npm缓存
npm cache clean --force

# 删除node_modules和package-lock.json
rm -rf node_modules package-lock.json

# 重新安装
npm install
```

### Q4: Python依赖安装失败
**错误信息：** `Failed building wheel for ...`

**解决方法：**
```bash
# 安装编译工具
sudo apt-get install -y build-essential python3-dev

# 升级pip
pip install --upgrade pip

# 重新安装依赖
pip install -r requirement.txt
```

### Q5: 前端页面显示空白
**可能原因：** 后端API未启动或连接失败

**解决方法：**
1. 确保后端服务正在运行（步骤5）
2. 检查浏览器控制台是否有错误
3. 检查前端API配置（`frontend/testui/src/store/api/ajax.js`）

---

## 📝 快速命令总结

```bash
# 一键启动脚本（创建 start.sh 文件）
#!/bin/bash

# 启动Redis
sudo systemctl start redis

# 启动后端（新终端）
gnome-terminal -- bash -c "cd /path/to/ISO15118_TestUI-master && source venv/bin/activate && python manage.py runserver 0.0.0.0:8000; exec bash"

# 等待2秒
sleep 2

# 启动前端（新终端）
gnome-terminal -- bash -c "cd /path/to/ISO15118_TestUI-master/frontend/testui && npm run serve; exec bash"

echo "✅ 服务已启动！访问 http://localhost:8080"
```

**使用方法：**
```bash
chmod +x start.sh
./start.sh
```

---

## 🎉 完成！

现在您应该可以在浏览器中查看前端界面了！

**访问地址：** http://localhost:8080

**如需停止服务：** 在两个终端窗口分别按 `Ctrl+C`

---

## 📞 获取帮助

如果遇到问题：
1. 检查终端错误信息
2. 查看浏览器控制台（F12）
3. 确保所有依赖都已正确安装
4. 参考上面的常见问题部分

祝您使用愉快！🚀

