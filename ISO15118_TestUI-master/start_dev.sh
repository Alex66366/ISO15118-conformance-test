#!/bin/bash

# ISO15118 TestUI 快速启动脚本
# 用于本地开发环境

echo "🚀 正在启动 ISO15118 TestUI..."

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 检查Python虚拟环境
if [ ! -d "venv" ]; then
    echo "❌ 虚拟环境不存在，正在创建..."
    python3 -m venv venv
fi

# 激活虚拟环境
echo "📦 激活虚拟环境..."
source venv/bin/activate

# 检查Redis是否运行
if ! redis-cli ping > /dev/null 2>&1; then
    echo "⚠️  Redis未运行，正在启动..."
    sudo systemctl start redis
    sleep 1
fi

# 检查Python依赖
if [ ! -f "venv/.installed" ]; then
    echo "📥 安装Python依赖..."
    pip install -r requirement.txt
    touch venv/.installed
fi

# 检查数据库
if [ ! -f "db.sqlite3" ]; then
    echo "🗄️  初始化数据库..."
    python manage.py migrate
fi

# 检查前端依赖
if [ ! -d "frontend/testui/node_modules" ]; then
    echo "📦 安装前端依赖..."
    cd frontend/testui
    npm install
    cd ../..
fi

# 颜色输出函数
print_info() {
    echo -e "\033[36mℹ️  $1\033[0m"
}

print_success() {
    echo -e "\033[32m✅ $1\033[0m"
}

print_error() {
    echo -e "\033[31m❌ $1\033[0m"
}

print_warning() {
    echo -e "\033[33m⚠️  $1\033[0m"
}

# 启动后端
print_info "启动Django后端服务器..."
gnome-terminal --tab --title="Django Backend" -- bash -c "
    cd \"$SCRIPT_DIR\"
    source venv/bin/activate
    python manage.py runserver 0.0.0.0:8000
    read -p '按Enter关闭此窗口...'
" &

sleep 2

# 启动前端
print_info "启动Vue前端服务器..."
gnome-terminal --tab --title="Vue Frontend" -- bash -c "
    cd \"$SCRIPT_DIR/frontend/testui\"
    npm run serve
    read -p '按Enter关闭此窗口...'
" &

sleep 3

# 显示成功信息
echo ""
print_success "所有服务已启动！"
echo ""
echo "📍 访问地址："
echo "   前端界面: http://localhost:8080"
echo "   后端API:  http://localhost:8000"
echo "   Admin管理: http://localhost:8000/admin"
echo ""
print_warning "提示：关闭服务请直接关闭终端窗口"
echo ""

