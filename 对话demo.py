#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys
import time
import re  # 添加re模块导入
from threading import Event
import json
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from collections import defaultdict

from dotenv import load_dotenv
import openai
from rich.console import Console
from rich.panel import Panel
from ip_mapper import IPMapper

app = Flask(__name__, 
    template_folder='templates',
    static_folder='templates/static'  # 添加static_folder配置
)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# -----------------------------
# 1. API 配置相关
# -----------------------------
API_CONFIGS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "display_name": "DeepSeek"
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "DASHSCOPE_API_KEY",
        "models": ["qwen-max-2025-01-25"],
        "default_model": "qwen-max-2025-01-25",
        "display_name": "通义千问"
    },
    # 使用简洁设计，直接用列表列出所有可用模型
    "yunwu_1": {
        "base_url": "https://yunwu.ai/v1",
        "env_key": "YUNWU_API_KEY_1",
        "models": ["gpt-4o", "o3-mini-high-all", "claude-3-5-sonnet-20241022"],
        "default_model": "gpt-4o",  # 默认对话模型
        "display_name": "云雾-逆向"
    },
    "yunwu_2": {
        "base_url": "https://yunwu.ai/v1",
        "env_key": "YUNWU_API_KEY_2",
        "models": ["gemini-2.0-pro-exp-02-05", "gemini-2.0-flash-thinking-exp-01-21","claude-3-5-sonnet-20241022"],
        "default_model": "claude-3-opus-20240229",  # 默认对话模型
        "display_name": "云雾-管转"
    }
}

# 默认API设置（如默认API不可用，则后续会自动选择第一个可用的API）
CURRENT_API = "qwen"

def load_available_apis():
    """加载可用的API配置（检测.env中的API key）"""
    available = {}
    for api_name, config in API_CONFIGS.items():
        api_key = os.getenv(config["env_key"])
        if api_key:
            available[api_name] = {**config, "api_key": api_key}
    return available

# 加载环境变量和初始化控制台
load_dotenv()
console = Console()

# 检查API配置是否可用
AVAILABLE_APIS = load_available_apis()
if not AVAILABLE_APIS:
    console.print("\n[red]❌ 未找到任何可用的API配置[/red]")
    console.print("[yellow]请在.env文件中至少添加以下其中一个API key：[/yellow]")
    for api_name, config in API_CONFIGS.items():
        console.print(f"[blue]{config['env_key']}=your_{api_name}_api_key[/blue]")
    sys.exit(1)

# 如果默认API不可用，则选择第一个可用的API
if CURRENT_API not in AVAILABLE_APIS:
    CURRENT_API = next(iter(AVAILABLE_APIS))


# -----------------------------
# 2. 流式输出打印类
# -----------------------------
class StreamPrinter:
    """流式输出处理器，负责缓存和逐块打印响应内容"""
    def __init__(self, web_mode=False, sid=None, api_name=None):
        self.buffer = []
        self.is_first_chunk = True
        self.print_lock = Event()
        self.print_lock.set()
        self.last_chunk_ended_with_newline = False
        self.web_mode = web_mode
        self.sid = sid
        self.api_name = api_name
        self.is_reasoning = False  # 添加思考状态标记

    def stream_print(self, content, is_reasoning=False):
        """将内容缓存后逐块打印至终端或发送到网页"""
        if not content:
            return
        self.buffer.append(content)
        if self.print_lock.is_set():
            self.print_lock.clear()
            if self.is_first_chunk and not is_reasoning:
                display_name = API_CONFIGS[self.api_name]["display_name"] if self.api_name else "AI"
                prefix = f"\n[cyan]{display_name}:[/cyan] "
                if self.web_mode:
                    socketio.emit('message', {'type': 'assistant_start', 'content': display_name}, room=self.sid)
                else:
                    console.print(prefix, end="")
                self.is_first_chunk = False
            
            # 处理思考状态的开始
            if is_reasoning and not self.is_reasoning:
                if self.web_mode:
                    socketio.emit('message', {'type': 'reasoning_start', 'content': '（思考中）'}, room=self.sid)
                console.print("\n[bright_blue]（思考中）[/bright_blue]")
                self.is_reasoning = True
            
            while self.buffer:
                chunk = self.buffer.pop(0)
                if self.web_mode:
                    msg_type = 'reasoning_content' if is_reasoning else 'assistant_content'
                    socketio.emit('message', {'type': msg_type, 'content': chunk}, room=self.sid)
                
                # 在终端中显示内容
                lines = chunk.split('\n')
                for i, line in enumerate(lines):
                    if i > 0:
                        console.print()
                        if not is_reasoning:
                            console.print("[cyan]          [/cyan]", end="")
                    if is_reasoning:
                        console.print(f"[bright_blue]{line}[/bright_blue]", end="")
                    else:
                        console.print(line, end="", highlight=False)
                self.last_chunk_ended_with_newline = chunk.endswith('\n')
            self.print_lock.set()

    def reset(self):
        """重置打印状态，结束当前流输出"""
        self.is_first_chunk = True
        if not self.last_chunk_ended_with_newline:
            if not self.web_mode:
                console.print()
        self.last_chunk_ended_with_newline = False
        self.is_reasoning = False  # 重置思考状态


# -----------------------------
# 3. 用户输入与辅助函数
# -----------------------------
def get_multiline_input():
    """
    获取用户输入（根据首行字数决定是否进入多行模式）
    首行字数超过25则提示用户进入多行模式（空行结束输入）
    """
    console.print("\n[bold green]用户:[/bold green] ", end="")
    try:
        first_line = input().strip()
        while not first_line:
            console.print("[yellow]输入不能为空，请重新输入[/yellow]")
            console.print("[bold green]用户:[/bold green] ", end="")
            first_line = input().strip()
    except UnicodeDecodeError:
        console.print("[red]❌ 输入编码错误，请使用UTF-8编码输入[/red]")
        return ""
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]输入已取消[/yellow]")
        return ""

    if len(first_line) < 25:
        return first_line

    lines = [first_line]
    console.print("[dim]（输入内容超过25字，进入多行模式，按回车键继续输入；输入空行结束）[/dim]")
    try:
        while True:
            console.print(f"[dim]{len(lines) + 1}> [/dim]", end="")
            try:
                line = input()
            except UnicodeDecodeError:
                console.print("[red]❌ 输入编码错误，继续输入或输入空行结束[/red]")
                continue
            except KeyboardInterrupt:
                console.print("\n[yellow]已取消当前行输入，按回车结束整体输入，或继续输入新行[/yellow]")
                continue
            if not line.strip():
                break
            lines.append(line)
            if len(lines) > 50:
                console.print("[yellow]⚠️ 输入行数较多，记得输入空行结束[/yellow]")
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]多行输入已终止，返回已输入内容[/yellow]")
    return "\n".join(lines)

def clear_terminal():
    """清除终端显示内容"""
    if sys.platform == "win32":
        os.system("cls")
    else:
        os.system("clear")

def print_model_list():
    """
    打印当前API下的所有可用模型列表，并返回模型列表
    默认模型会在后面标识出来
    """
    models = API_CONFIGS[CURRENT_API]["models"]
    default_model = API_CONFIGS[CURRENT_API]["default_model"]
    console.print(f"\n[cyan]{API_CONFIGS[CURRENT_API]['display_name']} 可用模型列表：[/cyan]")
    for idx, model in enumerate(models, start=1):
        if model == default_model:
            console.print(f"[blue]{idx}. {model} (默认)[/blue]")
        else:
            console.print(f"[blue]{idx}. {model}[/blue]")
    return models

def switch_model(choice, model_list):
    """
    根据用户输入的数字选择当前API下的对话模型
    """
    global current_model
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(model_list):
            selected_model = model_list[idx]
            current_model = selected_model
            console.print(f"\n[green]✓ 已切换到 {selected_model} 模型[/green]")
        else:
            console.print("\n[red]❌ 无效的模型序号[/red]")
    except ValueError:
        console.print("\n[red]❌ 请输入有效的数字[/red]")


# -----------------------------
# 4. API调用与流式响应处理
# -----------------------------
def chat_stream(messages, printer, model="deepseek-chat", client=None):
    """
    发送对话消息至后端API，并以流式方式处理返回内容
    包含错误重试和异常处理（认证、网络、未知错误）
    """
    full_response = []
    reasoning_content = []
    max_retries = 3
    retry_count = 0
    
    # 添加超时检测
    start_time = time.time()
    first_response_received = False

    while retry_count < max_retries:
        try:
            for chunk in client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7,
                stream=True,
                timeout=30
            ):
                # 检查是否是第一个响应
                if not first_response_received:
                    first_response_received = True
                    elapsed_time = time.time() - start_time
                    # 如果是 deepseek-reasoner 且超过10秒没有响应
                    if model == "deepseek-reasoner" and elapsed_time > 10:
                        raise TimeoutError("DeepSeek Reasoner 响应超时")

                if not chunk.choices or len(chunk.choices) == 0:
                    continue

                delta = chunk.choices[0].delta
                if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                    content = delta.reasoning_content
                    reasoning_content.append(content)
                    printer.stream_print(content, is_reasoning=True)
                elif hasattr(delta, 'content') and delta.content:
                    content = delta.content
                    full_response.append(content)
                    printer.stream_print(content, is_reasoning=False)
            break
        except TimeoutError as e:
            console.print(f"\n[red]❌ {str(e)}[/red]")
            return {"reasoning_content": "", "content": ""}
        except openai.AuthenticationError as e:
            console.print(f"\n[red]❌ 认证失败，请检查 API Key 是否正确: {e}[/red]")
            return {"reasoning_content": "", "content": ""}
        except (openai.APIConnectionError, openai.APITimeoutError) as e:
            retry_count += 1
            if retry_count < max_retries:
                wait_time = 2 ** retry_count
                console.print(f"\n[yellow]⚠️ 连接失败，{wait_time}秒后进行第{retry_count + 1}次重试...: {e}[/yellow]")
                time.sleep(wait_time)
            else:
                console.print(f"\n[red]❌ 连接失败，请检查网络连接或稍后重试: {e}[/red]")
                return {"reasoning_content": "", "content": ""}
        except Exception as e:
            console.print(f"\n[red]❌ 发生未知错误: {str(e)} - {type(e)}[/red]")
            return {"reasoning_content": "", "content": ""}

    return {
        "reasoning_content": "".join(reasoning_content),
        "content": "".join(full_response)
    }


# -----------------------------
# 5. 主交互逻辑
# -----------------------------
def main():
    global client, CURRENT_API, current_model
    # 标识是否处于模型选择模式：当用户执行"m"命令后进入此模式，
    # 下一次数字输入将作为模型选择而非API切换命令
    model_selection_mode = False
    current_model = None
    model_list = []  # 保存当前API下的模型列表顺序

    try:
        # 初始化API客户端
        try:
            client = openai.OpenAI(
                api_key=AVAILABLE_APIS[CURRENT_API]["api_key"],
                base_url=AVAILABLE_APIS[CURRENT_API]["base_url"]
            )
            console.print(f"\n[green]✓ 已连接到 {AVAILABLE_APIS[CURRENT_API]['display_name']} API[/green]")
        except Exception as e:
            console.print(f"\n[red]❌ 初始化客户端时发生错误: {str(e)}[/red]")
            return

        # 使用默认对话模型
        current_model = API_CONFIGS[CURRENT_API]["default_model"]
        # 初始对话历史：系统设定角色 提示词
        messages = [{
            "role": "system",
            "content": "你是一个人工智能助手，请用简洁明了的中文回答。"
        }]

        # 显示使用说明
        console.print(Panel.fit(
            "[bold yellow]AI 对话助手[/bold yellow]\n"
            "输入 [cyan]cl[/cyan] 清除记忆，输入 [cyan]q[/cyan] 退出\n"
            "输入 [cyan]m[/cyan] 查看当前API支持的模型列表，进入[cyan]模型选择模式[/cyan]（如果当前API支持）\n"
            "直接输入数字 [cyan]1[/cyan]、[cyan]2[/cyan]、[cyan]3[/cyan]、[cyan]4[/cyan] 切换API服务\n"
            "\n切换API对应关系：\n"
            "  1 - DeepSeek\n"
            "  2 - 通义千问\n"
            "  3 - 云雾-逆向\n"
            "  4 - 云雾-管转",
            border_style="blue"
        ))

        printer = StreamPrinter(api_name=CURRENT_API)

        while True:
            try:
                user_input = get_multiline_input().strip()
                if not user_input:
                    continue

                # 退出命令
                if user_input == "q":
                    console.print("\n[yellow]再见！[/yellow]")
                    break
                # 清除记忆：保留系统消息并清屏
                elif user_input == "cl":
                    messages = messages[:1]
                    clear_terminal()
                    console.print("[green]✓ 记忆已清除[/green]")
                    continue
                # 显示当前API模型列表，并进入模型选择模式
                elif user_input == "m":
                    model_list = print_model_list()
                    model_selection_mode = True
                    continue
                # 数字命令处理:
                # 如果处于模型选择模式，则数字视为模型切换命令（仅限云雾智能API）
                elif user_input.isdigit():
                    if model_selection_mode:
                        switch_model(user_input, model_list)
                        model_selection_mode = False
                        continue
                    else:
                        # 非模型选择，数字命令作为API切换指令
                        if user_input == "1" and "deepseek" in AVAILABLE_APIS:
                            CURRENT_API = "deepseek"
                        elif user_input == "2" and "qwen" in AVAILABLE_APIS:
                            CURRENT_API = "qwen"
                        elif user_input == "3" and "yunwu_1" in AVAILABLE_APIS:
                            CURRENT_API = "yunwu_1"
                        elif user_input == "4" and "yunwu_2" in AVAILABLE_APIS:
                            CURRENT_API = "yunwu_2"
                        else:
                            console.print("\n[yellow]⚠️ 该API未配置或不可用[/yellow]")
                            continue

                        # 切换API后重新初始化客户端及模型
                        try:
                            client = openai.OpenAI(
                                api_key=AVAILABLE_APIS[CURRENT_API]["api_key"],
                                base_url=AVAILABLE_APIS[CURRENT_API]["base_url"]
                            )
                            current_model = API_CONFIGS[CURRENT_API]["default_model"]
                            console.print(f"\n[green]✓ 已切换到 {API_CONFIGS[CURRENT_API]['display_name']} API[/green]")
                            console.print("[dim]输入 'm' 查看模型列表[/dim]")
                            # 切换API后，如为云雾智能则打印该API支持的模型列表
                            if CURRENT_API in ["yunwu_1", "yunwu_2"]:
                                model_list = print_model_list()
                        except Exception as e:
                            console.print(f"\n[red]❌ 切换API失败: {str(e)}[/red]")
                        continue

                # 普通对话内容，追加至消息列表后调用流式API
                if current_model == "deepseek-reasoner":
                    # 如果当前模型是 deepseek-reasoner，则清空消息列表，只保留 system message
                    messages = messages[:1]
                messages.append({"role": "user", "content": user_input})
                response = chat_stream(messages, printer, current_model, client)
                printer.reset()

                if response["content"]:
                    messages.append({"role": "assistant", "content": response["content"]})

            except KeyboardInterrupt:
                console.print("\n[yellow]🛑 操作已中断[/yellow]")
                continue

    except Exception as e:
        console.print(f"\n[red]⚠️ 异常: {str(e)}[/red]")

# 添加Flask路由
@app.route('/')
def index():
    return render_template('index.html')

def get_device_info(user_agent):
    """解析User-Agent获取详细的设备信息"""
    user_agent = user_agent.lower()
    device_info = {
        'type': '未知',
        'os': '未知',
        'browser': '未知',
        'model': '未知'
    }
    
    # 设备类型识别
    if 'ipad' in user_agent:
        device_info['type'] = '平板'
    elif 'mobile' in user_agent or 'android' in user_agent or 'iphone' in user_agent:
        device_info['type'] = '手机'
    else:
        device_info['type'] = '电脑'
    
    # 操作系统识别
    if 'windows' in user_agent:
        device_info['os'] = 'Windows'
        if 'windows nt 10' in user_agent:
            device_info['os'] += ' 10'
        elif 'windows nt 6.3' in user_agent:
            device_info['os'] += ' 8.1'
    elif 'mac os' in user_agent:
        device_info['os'] = 'macOS'
    elif 'linux' in user_agent:
        device_info['os'] = 'Linux'
    elif 'android' in user_agent:
        device_info['os'] = 'Android'
        # 尝试提取Android版本
        android_version = re.search(r'android (\d+(?:\.\d+)?)', user_agent)
        if android_version:
            device_info['os'] += f' {android_version.group(1)}'
    elif 'ios' in user_agent or 'iphone os' in user_agent:
        device_info['os'] = 'iOS'
        
    # 浏览器识别
    if 'chrome' in user_agent and 'edg' not in user_agent:
        device_info['browser'] = 'Chrome'
    elif 'firefox' in user_agent:
        device_info['browser'] = 'Firefox'
    elif 'safari' in user_agent and 'chrome' not in user_agent:
        device_info['browser'] = 'Safari'
    elif 'edg' in user_agent:
        device_info['browser'] = 'Edge'
    
    # 设备型号识别
    if 'iphone' in user_agent:
        device_info['model'] = 'iPhone'
    elif 'ipad' in user_agent:
        device_info['model'] = 'iPad'
    elif 'android' in user_agent:
        # 尝试提取具体型号
        model_match = re.search(r';\s*([^;]+(?:build|android)[^;]*)', user_agent)
        if model_match:
            model = model_match.group(1).strip().split('build')[0].strip()
            device_info['model'] = model
    
    return device_info

@socketio.on('connect')
def handle_connect(auth=None):
    sid = request.sid
    print(f"Client connected: {sid}")
    
    # 获取客户端IP
    client_ip = (
        request.headers.get('X-Real-IP') or 
        request.headers.get('X-Forwarded-For') or 
        request.headers.get('HTTP_X_FORWARDED_FOR') or
        request.environ.get('HTTP_X_REAL_IP') or
        request.environ.get('REMOTE_ADDR') or
        request.remote_addr or
        request.environ.get('REMOTE_ADDR', 'unknown')
    )
    if isinstance(client_ip, str) and ',' in client_ip:
        client_ip = client_ip.split(',')[0].strip()
    
    # 获取设备信息
    user_agent = request.headers.get('User-Agent', '')
    device_info = get_device_info(user_agent)
    
    # 检查是否存在相同IP的会话
    existing_session = None
    for existing_sid, session in user_sessions.items():
        if session.client_ip == client_ip:
            # 如果找到相同IP的会话且在30分钟内活跃，则复用该会话
            if time.time() - session.last_active_time < 1800:  # 30分钟
                existing_session = session
                # 删除旧的会话
                del user_sessions[existing_sid]
                break
    
    if existing_session:
        # 复用现有会话
        user_sessions[sid] = existing_session
        user_sessions[sid].update_active_time()
    else:
        # 创建新会话
        user_sessions[sid] = UserSession(CURRENT_API, AVAILABLE_APIS[CURRENT_API])
        user_sessions[sid].client_ip = client_ip
        user_sessions[sid].device_info = device_info

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    print(f"Client disconnected: {sid}")
    # 清理用户会话
    if sid in user_sessions:
        del user_sessions[sid]

@socketio.on('user_message')
def handle_message(data):
    sid = request.sid
    session = user_sessions[sid]
    message = data['message']
    
    # 记录用户输入，使用存储的客户端IP
    user_logger.log_user_input(
        ip_address=session.client_ip,
        user_id=sid,
        api_type=session.api_name,
        model=session.current_model,
        user_input=message
    )
    
    if session.current_model == "deepseek-reasoner":
        session.messages = session.messages[:1]
    session.messages.append({"role": "user", "content": message})
    
    printer = StreamPrinter(web_mode=True, sid=sid, api_name=session.api_name)
    response = chat_stream(session.messages, printer, session.current_model, session.client)
    printer.reset()
    
    if response["content"]:
        session.messages.append({"role": "assistant", "content": response["content"]})

@socketio.on('switch_api')
def handle_switch_api(data):
    sid = request.sid
    session = user_sessions[sid]
    api_num = data['api_num']
    
    new_api = None
    if api_num == 1 and "deepseek" in AVAILABLE_APIS:
        new_api = "deepseek"
    elif api_num == 2 and "qwen" in AVAILABLE_APIS:
        new_api = "qwen"
    elif api_num == 3 and "yunwu_1" in AVAILABLE_APIS:
        new_api = "yunwu_1"
    elif api_num == 4 and "yunwu_2" in AVAILABLE_APIS:
        new_api = "yunwu_2"
    
    if new_api:
        try:
            session.switch_api(new_api, AVAILABLE_APIS[new_api])
            emit('message', {'type': 'system', 'content': f'已切换到 {API_CONFIGS[new_api]["display_name"]} API'})
            emit('api_models', {
                'models': API_CONFIGS[new_api]["models"],
                'default_model': session.current_model
            })
        except Exception as e:
            emit('message', {'type': 'system', 'content': f'切换API失败: {str(e)}'})
    else:
        emit('message', {'type': 'system', 'content': '该API未配置或不可用'})

@socketio.on('clear_chat')
def handle_clear_chat():
    sid = request.sid
    session = user_sessions[sid]
    session.clear_messages()

@socketio.on('switch_model')
def handle_switch_model(data):
    sid = request.sid
    session = user_sessions[sid]
    model = data['model']
    if session.switch_model(model):
        emit('model_switched', {'model': model})
    else:
        emit('message', {'type': 'system', 'content': '无效的模型选择'})

@socketio.on('get_models')
def handle_get_models():
    sid = request.sid
    session = user_sessions[sid]
    emit('api_models', {
        'models': API_CONFIGS[session.api_name]["models"],
        'default_model': session.current_model
    })

# -----------------------------
# 用户会话管理
# -----------------------------
class UserSession:
    def __init__(self, api_name, api_config):
        self.api_name = api_name
        self.current_model = api_config["default_model"]
        self.messages = [{
            "role": "system",
            "content": "你是一个人工智能助手，请用简洁明了的中文回答。"
        }]
        self.client = openai.OpenAI(
            api_key=api_config["api_key"],
            base_url=api_config["base_url"]
        )
        self.client_ip = None
        self.device_info = None
        self.last_active_time = time.time()  # 添加最后活动时间

    def update_active_time(self):
        """更新最后活动时间"""
        self.last_active_time = time.time()

    def clear_messages(self):
        self.messages = self.messages[:1]
        self.update_active_time()

    def switch_api(self, api_name, api_config):
        self.api_name = api_name
        self.current_model = api_config["default_model"]
        self.client = openai.OpenAI(
            api_key=api_config["api_key"],
            base_url=api_config["base_url"]
        )
        self.update_active_time()

    def switch_model(self, model_name):
        if model_name in API_CONFIGS[self.api_name]["models"]:
            self.current_model = model_name
            self.update_active_time()
            return True
        return False

# 用户会话存储
user_sessions = {}

# -----------------------------
# 日志记录相关
# -----------------------------
class UserLogger:
    def __init__(self, log_dir=None):
        if log_dir is None:
            # 获取当前脚本所在目录
            script_dir = os.path.dirname(os.path.abspath(__file__))
            self.log_dir = os.path.join(script_dir, "logs")
        else:
            self.log_dir = log_dir
            
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        self.ip_mapper = IPMapper(os.path.join(self.log_dir, "ip_mapping.json"))

    def get_feature_hash(self, device_info):
        """根据设备特征生成唯一标识"""
        features = [
            device_info.get('type', '未知'),
            device_info.get('os', '未知'),
            device_info.get('browser', '未知'),
            'PC' if device_info.get('type') == '电脑' else device_info.get('model', '未知')
        ]
        # 将特征组合成一个标识字符串
        feature_str = '_'.join(str(f).replace(' ', '').lower() for f in features)
        return feature_str

    def _get_log_file(self, device_info):
        """根据设备特征获取对应的日志文件名"""
        feature_hash = self.get_feature_hash(device_info)
        current_date = datetime.now().strftime("%Y%m%d")
        return os.path.join(self.log_dir, f"{feature_hash}_{current_date}.log")

    def _ensure_log_file(self, log_file):
        """确保日志文件存在"""
        if not os.path.exists(log_file):
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write("+" + "-" * 110 + "+\n")  # 调整表格宽度
                f.write("| {:<19} | {:<12} | {:<4} | {:<6} | {:<8} | {:<8} | {:<8} | {:<20} |\n".format(
                    "时间戳", "IP/备注", "设备", "系统", "浏览器", "型号", "API", "模型"
                ))
                f.write("+" + "-" * 110 + "+\n")  # 表头分隔线

    def add_ip_mapping(self, ip: str, remark: str):
        """添加IP地址映射"""
        self.ip_mapper.add_mapping(ip, remark)

    def remove_ip_mapping(self, ip: str):
        """删除IP地址映射"""
        self.ip_mapper.remove_mapping(ip)

    def list_ip_mappings(self):
        """列出所有IP映射"""
        return self.ip_mapper.list_mappings()

    def log_user_input(self, ip_address, user_id, api_type, model, user_input):
        """记录用户输入到日志文件"""
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 获取IP的备注名（如果有的话）
        ip_display = self.ip_mapper.get_remark(ip_address)
        
        # 获取设备信息
        session = user_sessions.get(user_id)
        device_info = session.device_info if session else {
            'type': '未知',
            'os': '未知',
            'browser': '未知',
            'model': '未知'
        }
        
        # 处理输入中的换行符，确保日志格式正确
        user_input = user_input.replace('\n', ' ').replace('\r', '')
        
        # 获取对应的日志文件
        log_file = self._get_log_file(device_info)
        self._ensure_log_file(log_file)
        
        # 简化设备信息显示
        os_display = device_info['os'].replace('Windows ', 'Win').replace('Android ', 'A')
        device_type = device_info['type'][:2]  # 只取前两个字
        model_display = device_info['model']
        if model_display == '未知' and device_info['type'] == '电脑':
            model_display = 'PC'
        elif 'android' in model_display.lower():
            model_display = 'android'  # 保持完整android显示
        
        # 写入日志
        try:
            with open(log_file, 'a', encoding='utf-8') as f:
                # 写入基本信息行
                f.write("| {:<19} | {:<12} | {:<4} | {:<6} | {:<8} | {:<8} | {:<8} | {:<20} |\n".format(
                    current_time,
                    ip_display[:12],        # IP/备注
                    device_type,            # 设备类型（只取2字）
                    os_display[:6],         # 操作系统
                    device_info['browser'], # 浏览器完整显示
                    model_display[:8],      # 设备型号
                    api_type[:8],          # API完整显示
                    model[:20],            # 模型名
                ))
                # 写入输入内容行
                f.write("| 输入内容: {}\n".format(user_input))
                # 写入分隔线
                f.write("+" + "-" * 110 + "+\n")
                f.flush()  # 立即写入磁盘
        except Exception as e:
            console.print(f"\n[red]❌ 写入日志失败: {str(e)}[/red]")

# 创建日志记录器实例
user_logger = UserLogger()

# 添加新的路由处理IP映射管理
@app.route('/ip_mappings', methods=['GET'])
def get_ip_mappings():
    return jsonify(user_logger.list_ip_mappings())

@app.route('/ip_mappings', methods=['POST'])
def add_ip_mapping():
    data = request.json
    if not data or 'ip' not in data or 'remark' not in data:
        return jsonify({'error': '缺少必要参数'}), 400
    user_logger.add_ip_mapping(data['ip'], data['remark'])
    return jsonify({'message': '添加成功'})

@app.route('/ip_mappings/<ip>', methods=['DELETE'])
def remove_ip_mapping(ip):
    user_logger.remove_ip_mapping(ip)
    return jsonify({'message': '删除成功'})

def cleanup_inactive_sessions():
    """清理不活跃的会话"""
    current_time = time.time()
    inactive_sids = []
    for sid, session in user_sessions.items():
        if current_time - session.last_active_time > 3600:  # 1小时未活动
            inactive_sids.append(sid)
    for sid in inactive_sids:
        del user_sessions[sid]

# 在主循环中添加定期清理
if __name__ == "__main__":
    # 加载环境变量和初始化控制台
    load_dotenv()
    console = Console()

    # 检查API配置是否可用
    AVAILABLE_APIS = load_available_apis()
    if not AVAILABLE_APIS:
        console.print("\n[red]❌ 未找到任何可用的API配置[/red]")
        console.print("[yellow]请在.env文件中至少添加以下其中一个API key：[/yellow]")
        for api_name, config in API_CONFIGS.items():
            console.print(f"[blue]{config['env_key']}=your_{api_name}_api_key[/blue]")
        sys.exit(1)

    # 如果默认API不可用，则选择第一个可用的API
    if CURRENT_API not in AVAILABLE_APIS:
        CURRENT_API = next(iter(AVAILABLE_APIS))
    
    def cleanup_task():
        while True:
            time.sleep(300)  # 每5分钟清理一次
            cleanup_inactive_sessions()
    
    from threading import Thread
    cleanup_thread = Thread(target=cleanup_task, daemon=True)
    cleanup_thread.start()
    
    socketio.run(app, host='0.0.0.0', port=5005, debug=True, allow_unsafe_werkzeug=True) 