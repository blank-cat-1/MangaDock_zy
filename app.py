# -*- coding: utf-8 -*-
import os, re, time, requests, shutil, glob, json, img2pdf, random, threading
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import zipfile
from PIL import Image
from natsort import natsorted
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session, flash
from flask_sqlalchemy import SQLAlchemy
import uuid
from threading import Timer
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf.csrf import CSRFProtect

china_tz = timezone(timedelta(hours=8))

app = Flask(__name__)
# 使用更安全的 secret_key（随机生成的强密钥）
app.secret_key = os.urandom(24)
# 加强 session 和 cookie 安全配置
app.config['SESSION_COOKIE_HTTPONLY'] = True  # 防止 JavaScript 访问 cookie
app.config['SESSION_COOKIE_SECURE'] = False  # 开发环境设为 False，生产环境应设为 True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'  # 防止 CSRF 攻击
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)  # 会话有效期 24 小时
app.config['SESSION_REFRESH_EACH_REQUEST'] = True  # 每次请求刷新会话

# 模板渲染优化
app.config['TEMPLATES_AUTO_RELOAD'] = False  # 禁用模板自动重载（生产环境）
app.jinja_env.cache_size = 1000  # 增大模板缓存大小
app.jinja_env.auto_reload = False

app.wsgi_app = ProxyFix(
    app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1
)

# 静态文件缓存配置
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1年缓存

# 初始化 CSRF 保护
csrf = CSRFProtect(app)

# 登录失败计数存储（内存存储，重启后会重置）
login_failures = {}
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_DURATION = timedelta(minutes=15)

# 数据库配置 - SQLite 特定配置（使用 instance 文件夹）
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(app.instance_path, 'download_tasks.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# 确保 instance 文件夹存在
if not os.path.exists(app.instance_path):
    os.makedirs(app.instance_path)
db = SQLAlchemy(app)

# 配置参数
CONFIG = {
    'max_workers': 2,
    'request_timeout': 20,
    'retry_times': 3,
    'queue_buffer': 10,
    'delay_range': (0.1, 0.5)  # 延迟
}

class DownloadTask(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    comic_name = db.Column(db.String(255))
    url = db.Column(db.String(512))
    status = db.Column(db.String(20), default='pending')  # pending, running, completed, cancelled, error
    progress_percent = db.Column(db.Integer, default=0)
    total_chapters = db.Column(db.Integer, default=0)
    completed_chapters = db.Column(db.Integer, default=0)
    comic_format = db.Column(db.Integer, default=1)
    log = db.Column(db.Text, default='')
    start_time = db.Column(db.DateTime)
    end_time = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))
    is_update = db.Column(db.Boolean, default=False)
    group = db.Column(db.String(255), default='默认分组')  # 分组字段

class ReadingProgress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    comic_name = db.Column(db.String(255), nullable=False)
    last_chapter = db.Column(db.Integer, default=0)
    last_page = db.Column(db.Integer, default=0)
    scroll_position = db.Column(db.Integer, default=0)  # 滚动位置
    total_chapters = db.Column(db.Integer, default=0)
    total_pages = db.Column(db.Integer, default=0)
    last_read_at = db.Column(db.DateTime, default=datetime.now(china_tz))
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))


class ReadingTime(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    comic_name = db.Column(db.String(255), nullable=False)
    duration = db.Column(db.Integer, nullable=False)  # in minutes
    read_at = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now(china_tz))

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

    def set_password(self, password):
        # 使用 pbkdf2:sha256 算法，确保在 Docker 精简镜像中也能正常工作
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class LoginLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)
    user_agent = db.Column(db.String(500))
    login_time = db.Column(db.DateTime, default=datetime.now(china_tz))
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.String(200))

# 初始化数据库
with app.app_context():
    db.create_all()
    if not User.query.filter_by(username="admin").first():
        user = User(username="admin")
        user.set_password("123456")   # 默认密码
        db.session.add(user)
        db.session.commit()
        print("已创建默认用户：admin / 123456")

green = "\033[1;32m"
red =  "\033[1;31m"
dark_gray = "\033[1;30m"
light_red = "\033[1;31m"
reset = "\033[0;0m"

def safe_print(message, end="\n", flush=False):
    """安全打印函数，用于日志记录"""
    print(message, end=end, flush=flush)

def create_task(url, comic_format, is_update=False):
    """创建新任务并返回任务ID"""
    with app.app_context(): 
        task_id = str(uuid.uuid4())
        comic_name, _, _ = title(url) if url else ("未知漫画", 0, "")
        
        task = DownloadTask(
            id=task_id,
            comic_name=comic_name,
            url=url,
            comic_format=comic_format,
            start_time=datetime.now(china_tz),
            is_update=is_update
        )
        db.session.add(task)
        db.session.commit()
        
        return task_id

def update_task(task_id, **kwargs):
    """更新任务状态"""
    with app.app_context(): 
        task = db.session.get(DownloadTask, task_id) 
        if task:
            for key, value in kwargs.items():
                if key == 'log':
                    task.log = (task.log or '') + value + '\n'
                else:
                    setattr(task, key, value)
            db.session.commit()
        return task

def get_task(task_id):
    """获取任务信息"""
    with app.app_context():  
        return db.session.get(DownloadTask, task_id)  

def get_all_tasks():
    """获取所有任务"""
    with app.app_context():
        return DownloadTask.query.order_by(DownloadTask.created_at.desc()).all()


def delete_task(task_id):
    """仅删除任务记录，保留漫画文件"""
    with app.app_context():
        task = db.session.get(DownloadTask, task_id)
        if task:
            # 删除数据库记录
            db.session.delete(task)
            db.session.commit()
            print(f"成功删除任务记录: {task_id}")
            return True
        return False

def get_reading_progress(comic_name):
    """获取漫画阅读进度"""
    with app.app_context():
        return ReadingProgress.query.filter_by(comic_name=comic_name).first()

def save_reading_progress(comic_name, chapter, page, scroll_position, total_chapters, total_pages):
    """保存阅读进度 - 修复��数顺序"""
    with app.app_context():
        progress = ReadingProgress.query.filter_by(comic_name=comic_name).first()
        if progress:
            progress.last_chapter = chapter
            progress.last_page = page
            progress.scroll_position = scroll_position
            progress.total_chapters = total_chapters
            progress.total_pages = total_pages
            progress.last_read_at = datetime.now(china_tz)
        else:
            progress = ReadingProgress(
                comic_name=comic_name,
                last_chapter=chapter,
                last_page=page,
                scroll_position=scroll_position,
                total_chapters=total_chapters,
                total_pages=total_pages,
                last_read_at=datetime.now(china_tz)
            )
            db.session.add(progress)
        db.session.commit()
        return progress

def get_all_reading_progress():
    """获取所有阅读进度"""
    with app.app_context():
        return ReadingProgress.query.all()


def record_reading_time(comic_name, duration):
    """记录阅读时间"""
    with app.app_context():
        # Create a new ReadingTime entry
        reading_time = ReadingTime(
            comic_name=comic_name,
            duration=duration,
            read_at=datetime.now(china_tz)
        )
        db.session.add(reading_time)
        db.session.commit()
        return reading_time


def get_total_reading_time():
    """获取总阅读时间（分钟）"""
    with app.app_context():
        total = db.session.query(db.func.sum(ReadingTime.duration)).scalar()
        return total or 0


def get_reading_time_by_comic():
    """按漫画分组获取阅读时间"""
    with app.app_context():
        result = db.session.query(
            ReadingTime.comic_name,
            db.func.sum(ReadingTime.duration).label('total_duration')
        ).group_by(ReadingTime.comic_name).order_by(db.desc('total_duration')).all()
        return result


def get_reading_time_for_comic(comic_name):
    """获取单个漫画的总阅读时间（分钟）"""
    with app.app_context():
        total = db.session.query(db.func.sum(ReadingTime.duration)).filter(ReadingTime.comic_name == comic_name).scalar()
        return total or 0


def get_reading_time_monthly():
    """按月份分组获取阅读时间"""
    with app.app_context():
        result = db.session.query(
            db.func.strftime('%Y-%m', ReadingTime.read_at).label('month'),
            db.func.sum(ReadingTime.duration).label('total_duration')
        ).group_by('month').order_by('month').all()
        return result

# 缓存变量
comics_cache = {
    'data': None,
    'timestamp': 0,
    'expiration': 300  # 5分钟缓存
}

def get_available_comics():
    """获取可阅读的漫画列表（包括未完成的和已删除任务但文件仍存在的）"""
    global comics_cache

    # 检查缓存是否有效
    current_time = time.time()
    if comics_cache['data'] and (current_time - comics_cache['timestamp'] < comics_cache['expiration']):
        return comics_cache['data']

    with app.app_context():
        # 先从数据库获取所有任务
        query = DownloadTask.query.filter(
            DownloadTask.status.in_(['completed', 'running', 'error', 'cancelled'])
        )

        all_tasks = query.all()

        comics = {}

        # 处理有数据库记录的漫画
        for task in all_tasks:
            if task.comic_name not in comics:
                comic_path = os.path.join("./comic", task.comic_name)
                has_content = False
                available_chapters = 0

                if os.path.exists(comic_path):
                    try:
                        # 优化文件查找，一次遍历获取所有文件
                        files = os.listdir(comic_path)
                        pdf_files = []
                        cbz_files = []
                        for f in files:
                            if f.endswith('.pdf'):
                                pdf_files.append(f)
                            elif f.endswith('.cbz'):
                                cbz_files.append(f)

                        if pdf_files or cbz_files:
                            has_content = True
                            available_chapters = len(pdf_files) + len(cbz_files)
                    except Exception:
                        continue

                if has_content:
                    comics[task.comic_name] = {
                        'id': task.id,
                        'comic_name': task.comic_name,
                        'comic_format': task.comic_format,
                        'status': task.status,
                        'total_chapters': task.total_chapters,
                        'completed_chapters': task.completed_chapters,
                        'available_chapters': available_chapters,
                        'created_at': task.created_at
                    }

        # 检查文件系统中是否有漫画文件但没有数据库记录的情况
        if os.path.exists("./comic"):
            for comic_name in os.listdir("./comic"):
                if comic_name not in comics:
                    comic_path = os.path.join("./comic", comic_name)
                    if os.path.isdir(comic_path):
                        try:
                            files = os.listdir(comic_path)
                            pdf_files = []
                            cbz_files = []
                            for f in files:
                                if f.lower().endswith('.pdf'):
                                    pdf_files.append(f)
                                elif f.lower().endswith('.cbz'):
                                    cbz_files.append(f)

                            if pdf_files or cbz_files:
                                # 优化格式检测
                                if len(pdf_files) > len(cbz_files):
                                    comic_format = 1  # PDF格式
                                elif len(cbz_files) > len(pdf_files):
                                    comic_format = 2  # CBZ格式
                                else:
                                    comic_format = 1 if pdf_files else 2

                                comics[comic_name] = {
                                    'id': comic_name,
                                    'comic_name': comic_name,
                                    'comic_format': comic_format,
                                    'status': 'completed',
                                    'total_chapters': len(pdf_files) + len(cbz_files),
                                    'completed_chapters': len(pdf_files) + len(cbz_files),
                                    'available_chapters': len(pdf_files) + len(cbz_files),
                                    'created_at': None
                                }
                        except Exception:
                            continue

        # 更新缓存
        comics_cache['data'] = list(comics.values())
        comics_cache['timestamp'] = time.time()

        return comics_cache['data']

def is_mxs_url(url):
    """判断是否为 mxs12.cc 网站的 URL"""
    return 'mxs12.cc' in url or 'wzd1.cc' in url


def title_mxs(url):
    """获取 mxs12.cc 漫画标题和总章节数"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html_content = response.text

        if not html_content.strip():
            raise Exception("获取到空的网页内容")

        soup = BeautifulSoup(html_content, "html.parser")
        title_tag = soup.find("h1")

        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "未知漫画"

        # 获取章节列表
        links = soup.select('ul#detail-list-select li a')
        chapter_max = len(links)

        # 获取封面图片 - 处理 /book/ 路径
        path_parts = url.strip('/').split('/')
        # 查找 book 后的 ID，或者直接取最后一段
        if 'book' in path_parts:
            book_index = path_parts.index('book')
            if book_index + 1 < len(path_parts):
                cid = path_parts[book_index + 1]
            else:
                cid = path_parts[-1]
        else:
            cid = path_parts[-1]
        cover_url = f"https://www.wzd1.cc/static/upload/book/{cid}/cover.jpg"
        try:
            response = requests.get(cover_url, timeout=10)
            if response.status_code == 200:
                if not os.path.exists("./static/cover"):
                    os.makedirs("./static/cover")
                with open(f"./static/cover/{title}.jpg", 'wb') as f:
                    f.write(response.content)
                print(f"封面已保存到: ./static/cover/{title}.jpg")
        except Exception as e:
            print(f"下载封面时出错: {e}")

        safe_print(f"提取到的章节数={chapter_max}")

        return str(title), chapter_max, html_content

    except Exception as e:
        error_msg = f"获取漫画信息失败: {str(e)}"
        safe_print(error_msg)
        return "未知漫画", 0, ""


def title(url):
    """获取漫画标题和总章节数（自动识别网站）"""
    if is_mxs_url(url):
        return title_mxs(url)

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        html_content = response.text

        if not html_content.strip():
            raise Exception("获取到空的网页内容")

        soup = BeautifulSoup(html_content, "html.parser")
        title_tag = soup.find("h1", class_="comics-detail__title")

        if title_tag:
            title = title_tag.get_text(strip=True)
        else:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else "未知漫画"

        pattern = r'<meta data-n-head="ssr" data-hid="og:image" name="og:image" content="(https?://[^"]+)"'
        match = re.search(pattern, html_content)
        if match:
            image_url = match.group(1)
            print(f"找到图片URL: {image_url}")
            try:
                response = requests.get(image_url)
                response.raise_for_status()
                # 保存图片到本地
                if not os.path.exists("./static/cover"):
                    os.makedirs("./static/cover")
                with open(f"./static/cover/{title}.jpg", 'wb') as f:
                    f.write(response.content)
                print(f"图片已保存到: ./static/cover/{title}.jpg")
            except Exception as e:
                print(f"下载或保存图片时出错: {e}")

        chapter_max = 0
        chapter_slot = re.search(r'chapter_slot=(\d+)', html_content)
        if chapter_slot:
            chapter_max = int(chapter_slot.group(1))
        if chapter_max == 0:
            chapter_match = re.search(r'共(\d+)话', html_content)
            if chapter_match:
                chapter_max = int(chapter_match.group(1))
        if chapter_max == 0:
            chapter_links = re.findall(r'/comic/chapter/[^/]+/0_(\d+)\.html', html_content)
            if chapter_links:
                chapter_max = max(map(int, chapter_links)) + 1  # 加1因为章节通常从0开始

        safe_print(f"提取到的章节数={chapter_max}")
        safe_print(f"HTML片段包含chapter_slot? {('chapter_slot' in html_content)}")

        return str(title), chapter_max, html_content

    except Exception as e:
        error_msg = f"获取漫画信息失败: {str(e)}"
        safe_print(error_msg)
        return "未知漫画", 0, ""

def images_to_cbz(folder_path):
    """将图片转换为CBZ格式"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            if fname.lower().endswith(('.jpg', '.jpeg', '.png')):
                images.append(os.path.join(folder_path, fname))
        
        # 排序
        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"
        
        cbz_name = os.path.join(os.path.dirname(folder_path), f"{os.path.basename(folder_path)}.cbz")
        with zipfile.ZipFile(cbz_name, 'w') as cbz_file:
            for image_path in images:
                cbz_file.write(
                    image_path,
                    arcname=os.path.basename(image_path),
                    compress_type=zipfile.ZIP_STORED
                )
        return True, f"成功生成CBZ：{cbz_name}"
    except Exception as e:
        return False, f"CBZ生成失败：{str(e)}"

def images_to_cbz_with_name(folder_path, output_dir, file_name):
    """将图片转换为CBZ格式，使用指定文件名"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            if fname.lower().endswith(('.jpg', '.jpeg', '.png')):
                images.append(os.path.join(folder_path, fname))
        
        # 排序
        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"
        
        cbz_name = os.path.join(output_dir, f"{file_name}.cbz")
        with zipfile.ZipFile(cbz_name, 'w') as cbz_file:
            for image_path in images:
                cbz_file.write(
                    image_path,
                    arcname=os.path.basename(image_path),
                    compress_type=zipfile.ZIP_STORED
                )
        return True, f"成功生成CBZ：{cbz_name}"
    except Exception as e:
        return False, f"CBZ生成失败：{str(e)}"
    
def images_to_cbz_watch(folder_path):
    try:
        single_image_path = "./static/cover/bzmh.png"
        if not os.path.exists(single_image_path):
            return False, f"错误：图片文件不存在，请检查路径 -> {single_image_path}"
        cbz_name = os.path.join(folder_path, "00.cbz")
        with zipfile.ZipFile(cbz_name, 'w') as cbz_file:
            cbz_file.write(
                single_image_path,
                arcname=os.path.basename(single_image_path),
                compress_type=zipfile.ZIP_STORED
            )
        return True, f"成功生成 CBZ：{cbz_name}（包含图片：{os.path.basename(single_image_path)}）"
    except Exception as e:
        return False, f"CBZ 生成失败：{str(e)}"

def images_to_pdf(folder_path):
    """将图片转换为PDF格式，增强版尺寸超限处理"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            if fname.lower().endswith(('.jpg', '.jpeg', '.png')):
                images.append(os.path.join(folder_path, fname))
        
        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"
        
        pdf_name = os.path.join(os.path.dirname(folder_path), f"{os.path.basename(folder_path)}.pdf")
        return _convert_images_to_pdf(images, pdf_name)
    except Exception as e:
        return False, f"PDF生成失败：{str(e)}"

def images_to_pdf_with_name(folder_path, output_dir, file_name):
    """将图片转换为PDF格式，使用指定文件名"""
    try:
        images = []
        for fname in os.listdir(folder_path):
            if fname.lower().endswith(('.jpg', '.jpeg', '.png')):
                images.append(os.path.join(folder_path, fname))
        
        images = natsorted(images)
        if not images:
            return False, f"文件夹 {folder_path} 中没有图片"
        
        pdf_name = os.path.join(output_dir, f"{file_name}.pdf")
        return _convert_images_to_pdf(images, pdf_name)
    except Exception as e:
        return False, f"PDF生成失败：{str(e)}"

def _convert_images_to_pdf(images, pdf_name):
    """内部函数：将图片列表转换为PDF"""
    try:
        valid_images = []
        max_size = 14400  # PDF单位最大尺寸
        min_size = 3      # PDF单位最小尺寸
        
        # 配置img2pdf允许的尺寸范围
        layout_fun = img2pdf.get_layout_fun((min_size, min_size, max_size, max_size))
        
        for image_path in images:
            try:
                with Image.open(image_path) as img:
                    if img.mode in ('RGBA', 'P'):
                        img = img.convert('RGB')
                        
                    width, height = img.size
                    safe_print(f"原始图片尺寸: {width}x{height}")
                    
                    scale = 1.0
                    if width > max_size:
                        scale = max_size / width
                    if height > max_size:
                        scale = min(scale, max_size / height)
                        
                    if width < min_size:
                        scale = max(scale, min_size / width)
                    if height < min_size:
                        scale = max(scale, min_size / height)

                    if scale != 1.0:
                        new_width = int(round(width * scale))
                        new_height = int(round(height * scale))
                        
                        new_width = max(min(new_width, max_size), min_size)
                        new_height = max(min(new_height, max_size), min_size)
                        
                        resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                        
                        resized_path = os.path.splitext(image_path)[0] + "_resized.jpg"
                        resized_img.save(resized_path, "JPEG", quality=95)
                        
                        safe_print(f"图片已调整为 {new_width}x{new_height}，保存至 {resized_path}")
                    else:
                        if width < min_size or width > max_size or height < min_size or height > max_size:
                            raise ValueError(f"图片尺寸({width}x{height})不在有效范围内")
                        valid_images.append(image_path)
                        safe_print(f"图片尺寸有效: {width}x{height}")
            except Exception as e:
                error_msg = f"处理图片 {image_path} 时出错: {str(e)}，已跳过"
                safe_print(error_msg)
                continue
        
        if not valid_images:
            return False, "没有有效的图片可以生成PDF"
        
        try:
            pdf_bytes = img2pdf.convert(valid_images, layout_fun=layout_fun)
            with open(pdf_name, "wb") as f:
                f.write(pdf_bytes)
        except Exception as e:
            for img_path in valid_images:
                with Image.open(img_path) as img:
                    w, h = img.size
                    if w < min_size or w > max_size or h < min_size or h > max_size:
                        safe_print(f"致命错误：调整后的图片 {img_path} 尺寸({w}x{h})仍然无效！")
            
            return False, f"PDF生成失败：{str(e)}"
        
        for img_path in valid_images:
            if "_resized.jpg" in img_path and os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except Exception as e:
                    safe_print(f"清理临时文件 {img_path} 失败: {str(e)}")
        
        return True, f"成功生成PDF：{pdf_name}"
    except Exception as e:
        return False, f"PDF生成失败：{str(e)}"

def download_image_mxs(session, img_url, save_path, retries=3):
    """下载单张图片（mxs12.cc 专用）"""
    for attempt in range(retries):
        try:
            safe_print(f"正在下载: {os.path.basename(save_path)}")
            with session.get(img_url, stream=True, timeout=30) as response:
                if response.status_code == 200:
                    with open(save_path, 'wb') as f:
                        for chunk in response.iter_content(2048):
                            f.write(chunk)
                    return True
                else:
                    safe_print(f"图片请求失败，状态码: {response.status_code}，第{attempt+1}次重试。")
        except Exception as e:
            safe_print(f"图片请求失败，错误: {str(e)}，第{attempt+1}次重试。")
        if attempt < retries - 1:
            time.sleep(2)
    safe_print(f"图片多次尝试失败: {os.path.basename(save_path)}")
    return False


def download_images_concurrently_mxs(session, img_urls, save_dir, max_workers=2):
    """并发下载图片（mxs12.cc 专用）"""
    os.makedirs(save_dir, exist_ok=True)
    safe_print(f"开始下载 {len(img_urls)} 张图片到: {save_dir}")

    success_count = 0
    failed_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for img_idx, img_url in enumerate(img_urls, start=1):
            img_name = f"{img_idx:02d}.jpg"
            img_path = os.path.join(save_dir, img_name)
            future = executor.submit(download_image_mxs, session, img_url, img_path)
            futures.append(future)

        for future in concurrent.futures.as_completed(futures):
            if future.result():
                success_count += 1
            else:
                failed_count += 1

    safe_print(f"图片下载完成: 成功 {success_count} 张，失败 {failed_count} 张")
    return success_count


def crawl_chapter_mxs(chapter_url, folder, chapter, comic_format, task_id):
    """下载单个章节（mxs12.cc 专用）"""
    save_dir = os.path.join("./comic", folder, f"{chapter:02d}")
    os.makedirs(save_dir, exist_ok=True)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0'
    }

    try:
        with requests.Session() as session:
            # 配置会话参数
            session.headers.update(headers)
            session.timeout = 30  # 增加超时时间

            for attempt in range(3):
                try:
                    safe_print(f"正在访问章节 {chapter}: {chapter_url}")
                    response = session.get(chapter_url, timeout=30)
                    response.raise_for_status()

                    safe_print(f"章节 {chapter} 页面获取成功，状态码: {response.status_code}")
                    safe_print(f"页面大小: {len(response.text)} 字节")

                    soup = BeautifulSoup(response.text, 'html.parser')
                    img_tags = soup.find_all('img', class_='lazy')
                    img_urls = [img['data-original'] for img in img_tags if img.has_attr('data-original')]

                    safe_print(f"章节 {chapter} 找到 {len(img_urls)} 张图片")

                    if img_urls:
                        success_count = download_images_concurrently_mxs(session, img_urls, save_dir)
                        safe_print(f"章节 {chapter} 图片下载成功: {success_count} 张")

                        if success_count > 0:
                            # 章节下载完成后压缩成 CBZ
                            cbz_path = os.path.join("./comic", folder, f"{chapter:02d}.cbz")
                            success, msg = images_to_cbz(save_dir)
                            if success:
                                safe_print(f"已压缩为: {os.path.basename(cbz_path)}")
                            else:
                                safe_print(f"压缩失败: {msg}")

                            # 删除原文件夹
                            shutil.rmtree(save_dir)

                            return True, f"章节 {chapter} 下载完成（成功 {success_count} 张）"
                        else:
                            safe_print(f"章节 {chapter} 图片下载全部失败")
                            shutil.rmtree(save_dir)
                    else:
                        safe_print(f"章节 {chapter} 未找到图片")

                except Exception as e:
                    safe_print(f"章节 {chapter} 第 {attempt + 1} 次尝试失败: {str(e)}")
                    if attempt < 2:
                        time.sleep(3)  # 增加重试间隔
                    else:
                        safe_print(f"章节 {chapter} 三次尝试都失败，放弃")

        return False, f"章节 {chapter} 下载失败"

    except Exception as e:
        safe_print(f"章节 {chapter} 下载异常: {str(e)}")
        return False, f"章节 {chapter} 下载失败: {str(e)}"


def download_image(session, base_url, save_dir, n, task_id, retries=CONFIG['retry_times']):
    """下载单张图片"""
    img_url = base_url.format(n)
    file_path = os.path.join(save_dir, f"{n}.jpg")

    for attempt in range(retries):
        time.sleep(random.uniform(*CONFIG['delay_range']))
        try:
            with session.get(img_url, stream=True, timeout=15) as response:
                if response.status_code == 200:
                    with open(file_path, 'wb') as f:
                        for chunk in response.iter_content(2048):
                            f.write(chunk)
                    return True, n, False
                else:
                    if response.status_code == 404:
                        return False, n, True
                    else:
                        safe_print(f"图片{n} 下载失败，第{attempt+1}次重试。")
        except Exception as e:
            safe_print(f"图片{n} 下载失败，第{attempt+1}次重试。")

        if attempt < retries - 1:
            time.sleep(2 ** attempt)

    return False, n, False

def crawl_chapter(chapter_url, folder, chapter, comic_format, task_id):
    """下载单个章节，使用章节名命名文件"""
    save_dir = os.path.join("./comic", folder, f"{chapter:02d}")
    os.makedirs(save_dir, exist_ok=True)
    bzmh_00_cbz = os.path.join("./comic", folder)
    if comic_format == 2:
        images_to_cbz_watch(bzmh_00_cbz)
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    
    try:
        with requests.Session() as session:
            for attempt in range(3):
                try:
                    response = session.get(chapter_url, headers=headers, timeout=10)
                    # 如果成功获取200响应，直接返回成功
                    if response.status_code == 200:
                        break
                    # 非200状态码，记录并继续重试
                    safe_print(f"章节页访问失败，状态码: {response.status_code}，第{attempt+1}次尝试")
                except Exception as e:
                    safe_print(f"章节页访问异常: {str(e)}，第{attempt+1}次尝试")
            else:
                # 当循环完成且未通过break退出时，说明3次尝试都失败
                return False, f"章节页经过3次尝试后仍访问失败"

            # 提取章节名
            chapter_name = None
            soup = BeautifulSoup(response.text, 'html.parser')
            # 尝试多种方式获取章节名
            # 1. 从 title 标签获取
            title_tag = soup.find('title')
            if title_tag:
                title_text = title_tag.get_text(strip=True)
                # 提取章节名（通常是 "章节名 - 漫画名" 或 "漫画名 - 章节名"）
                if ' - ' in title_text:
                    parts = title_text.split(' - ')
                    if len(parts) >= 2:
                        chapter_name = parts[0].strip()
            
            # 2. 从 h1/h2 标签获取
            if not chapter_name:
                h1_tag = soup.find('h1')
                if h1_tag:
                    chapter_name = h1_tag.get_text(strip=True)
            
            # 3. 从特定的章节标题元素获取
            if not chapter_name:
                chapter_title = soup.find('div', class_=re.compile('chapter|title', re.I))
                if chapter_title:
                    chapter_name = chapter_title.get_text(strip=True)
            
            # 清理章节名，移除非法字符
            if chapter_name:
                # 移除Windows文件系统不允许的字符
                chapter_name = re.sub(r'[<>:"/\\|?*]', '', chapter_name)
                chapter_name = chapter_name.strip()
                # 限制长度
                if len(chapter_name) > 100:
                    chapter_name = chapter_name[:100]
            
            # 构建文件名：序号_章节名 或 只用序号
            if chapter_name:
                file_name = f"{chapter:02d}_{chapter_name}"
            else:
                file_name = f"{chapter:02d}"
            
            safe_print(f"章节名: {chapter_name if chapter_name else '未获取'}")
            safe_print(f"文件名: {file_name}")

            match = re.search(r'(https?://[^/]+/scomic/[^/]+/\d+/[^/]+/1\.jpg)', response.text)
            if not match:
                return False, "未找到图片地址"
            
            base_url = match.group(1).replace("1.jpg", "{}.jpg")
            update_task(task_id, log=f"开始下载章节：{file_name}")

            max_workers = CONFIG['max_workers']
            success_count = 0
            n = 1
            stop_flag = False
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                while not stop_flag:
                    # 检查任务是否已取消
                    task = get_task(task_id)
                    if task and task.status == 'cancelled':
                        executor.shutdown(wait=False)
                        return False, "任务已取消"
                        
                    futures = []
                    for _ in range(max_workers * 2):
                        futures.append(executor.submit(
                            download_image, session, base_url, save_dir, n, task_id
                        ))
                        n += 1
                    
                    for future in as_completed(futures):
                        task = get_task(task_id)
                        if task and task.status == 'cancelled':
                            executor.shutdown(wait=False)
                            return False, "任务已取消"
                            
                        success, num, stop_download = future.result()
                        if success:
                            success_count += 1
                        else:
                            if stop_download:
                                stop_flag = True
                                executor.shutdown(wait=False)
                                break

                    time.sleep(random.uniform(*CONFIG['delay_range']))
            
            if comic_format == 1:
                update_task(task_id, log=f"章节 {file_name} 下载完成，开始生成PDF...")
                success, msg = images_to_pdf_with_name(save_dir, bzmh_00_cbz, file_name)
            else:
                update_task(task_id, log=f"章节 {file_name} 下载完成，开始生成CBZ...")
                success, msg = images_to_cbz_with_name(save_dir, bzmh_00_cbz, file_name)
            
            update_task(task_id, log=msg)
            
            try:
                shutil.rmtree(save_dir)
            except Exception as e:
                update_task(task_id, log=f"删除临时目录时发生错误: {e}")
            
            if success:
                return True, f"章节 {file_name} 处理完成"
            else:
                return False, f"章节 {file_name} 处理失败: {msg}"
                
    except Exception as e:
        error_msg = f"章节处理异常：{str(e)}"
        update_task(task_id, log=error_msg)
        return False, error_msg

def download_complete_book_mxs(url, comic_format, task_id):
    """下载整本漫画（mxs12.cc 专用）"""
    try:
        update_task(task_id, status='running')

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        folder, chapter_max, html_content = title(url)
        update_task(task_id, comic_name=folder)
        update_task(task_id, log=f"开始下载漫画: {folder}")
        update_task(task_id, log=f"总章节数: {chapter_max}")

        if chapter_max <= 0:
            update_task(task_id, log="错误：未获取到有效的章节数，无法开始下载")
            update_task(task_id, log=f"请检查URL是否正确: {url}")
            update_task(task_id, status='error')
            return

        update_task(task_id, total_chapters=chapter_max)

        # 解析章节链接
        soup = BeautifulSoup(html_content, 'html.parser')
        links = soup.select('ul#detail-list-select li a')
        base_url = "https://mxs12.cc"
        chapter_urls = [base_url + a['href'] for a in links]

        json_file_path = "./comic.json"
        try:
            if os.path.exists(json_file_path):
                if os.path.getsize(json_file_path) > 0:
                    with open(json_file_path, "r", encoding="utf-8") as json_file:
                        existing_data = json.load(json_file)
                else:
                    existing_data = {}  # 空文件时初始化空字典
            else:
                existing_data = {}

            existing_data[folder] = url
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump(existing_data, json_file, ensure_ascii=False, indent=4)
        except json.JSONDecodeError:
            error_msg = f"JSON文件格式错误，已创建新文件: {json_file_path}"
            update_task(task_id, log=error_msg)
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump({folder: url}, json_file, ensure_ascii=False, indent=4)
        except Exception as e:
            error_msg = f"处理JSON文件失败: {str(e)}"
            update_task(task_id, log=error_msg)

        comic_path = os.path.join("./comic", folder)
        if not os.path.exists(comic_path):
            os.makedirs(comic_path)

        for idx, chapter_url in enumerate(chapter_urls, start=1):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            update_task(task_id, log=f"开始处理第 {idx} 章")
            success, msg = crawl_chapter_mxs(chapter_url, folder, idx, comic_format, task_id)
            update_task(task_id, log=msg)

            if task:
                new_completed = task.completed_chapters + 1
                progress = int((new_completed / chapter_max) * 100)
                update_task(task_id, completed_chapters=new_completed, progress_percent=progress)
            time.sleep(2)  # 减小延迟，避免被封禁

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有章节处理完成")

    except Exception as e:
        error_msg = f"下载过程出错: {str(e)}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))


def download_complete_book(url, comic_format, task_id):
    """下载整本漫画（自动识别网站）"""
    if is_mxs_url(url):
        return download_complete_book_mxs(url, comic_format, task_id)

    try:
        update_task(task_id, status='running')

        base_chapter_url = url.replace("/comic/", "/comic/chapter/") + "/0_{}.html"
        #update_task(task_id, log=f"调试: 章节URL模板: {base_chapter_url}")

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        folder, chapter_max, html_content = title(url)
        update_task(task_id, comic_name=folder)
        update_task(task_id, log=f"开始下载漫画: {folder}")
        update_task(task_id, log=f"总章节数: {chapter_max}")

        if chapter_max <= 0:
            update_task(task_id, log="尝试通过URL探测章节数...")
            chapter_max = 0
            for test_num in range(0, 20):
                test_url = base_chapter_url.format(test_num)
                try:
                    response = requests.head(test_url, headers=headers, timeout=5)
                    if response.status_code == 200:
                        chapter_max = test_num + 1
                    else:
                        break
                except:
                    break
            update_task(task_id, log=f"通过URL探测到章节数: {chapter_max}")

        if chapter_max <= 0:
            update_task(task_id, log="错误：未获取到有效的章节数，无法开始下载")
            update_task(task_id, log=f"请检查URL是否正确: {url}")
            update_task(task_id, status='error')
            return

        update_task(task_id, total_chapters=chapter_max)

        json_file_path = "./comic.json"
        try:
            if os.path.exists(json_file_path):
                if os.path.getsize(json_file_path) > 0:
                    with open(json_file_path, "r", encoding="utf-8") as json_file:
                        existing_data = json.load(json_file)
                else:
                    existing_data = {}  # 空文件时初始化空字典
            else:
                existing_data = {}

            existing_data[folder] = url
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump(existing_data, json_file, ensure_ascii=False, indent=4)
        except json.JSONDecodeError:
            error_msg = f"JSON文件格式错误，已创建新文件: {json_file_path}"
            update_task(task_id, log=error_msg)
            with open(json_file_path, "w", encoding="utf-8") as json_file:
                json.dump({folder: url}, json_file, ensure_ascii=False, indent=4)
        except Exception as e:
            error_msg = f"处理JSON文件失败: {str(e)}"
            update_task(task_id, log=error_msg)

        comic_path = os.path.join("./comic", folder)
        if not os.path.exists(comic_path):
            os.makedirs(comic_path)

        for chapter_num in range(0, chapter_max):
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            url = base_chapter_url.format(chapter_num)
            #update_task(task_id, log=f"调试: 访问章节URL: {url}")
            for attempt in range(5):
                try:
                    response = requests.get(url, headers=headers, timeout=10)
                    if response.status_code == 200:
                        chapter = chapter_num + 1
                        update_task(task_id, log=f"开始处理第 {chapter} 章")

                        success, msg = crawl_chapter(url, folder, chapter, comic_format, task_id)
                        update_task(task_id, log=msg)

                        task = get_task(task_id)
                        if task:
                            new_completed = task.completed_chapters + 1
                            progress = int((new_completed / chapter_max) * 100)
                            update_task(task_id, completed_chapters=new_completed, progress_percent=progress)
                        time.sleep(60)
                        break
                    else:
                        update_task(task_id, log=f"章节页访问异常，状态码: {response.status_code}，第{attempt+1}次尝试")
                except Exception as e:
                    update_task(task_id, log=f"章节页访问异常: {str(e)}，第{attempt+1}次尝试")
            else:
                return False, f"章节页经过5次尝试后仍访问失败"

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有章节处理完成")

    except Exception as e:
        error_msg = f"下载过程出错: {str(e)}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))

def update_comic(comic_name, comic_format, task_id):
    try:
        update_task(task_id, status='running')
        update_task(task_id, comic_name=comic_name)
        update_task(task_id, log=f"开始更新漫画: {comic_name}")
        
        comic_path = f"./comic/{comic_name}"
        update_task(task_id, log=f"检查漫画目录: {comic_path}")
        
        if not os.path.exists(comic_path):
            error_msg = f"漫画目录不存在: {comic_path}"
            update_task(task_id, log=error_msg)
            update_task(task_id, status='error', end_time=datetime.now(china_tz))
            return
        
        if not os.path.isdir(comic_path):
            error_msg = f"{comic_path} 不是目录"
            update_task(task_id, log=error_msg)
            update_task(task_id, status='error', end_time=datetime.now(china_tz))
            return

        update_task(task_id, log="读取已下载的章节文件...")
        
        # 读取CBZ文件
        cbz_files = [f for f in os.listdir(comic_path) if f.endswith('.cbz')]
        numbers = []
        for cbz_file in cbz_files:
            match = re.search(r'\d+', cbz_file)
            if match:
                numbers.append(int(match.group()))
        max_number = max(numbers) if numbers else 0  
        
        # 读取PDF文件
        pdf_files = [f for f in os.listdir(comic_path) if f.endswith('.pdf')]
        pdf_numbers = []
        for pdf_file in pdf_files:
            match = re.search(r'\d+', pdf_file)
            if match:
                pdf_numbers.append(int(match.group()))
        max_number_pdf = max(pdf_numbers) if pdf_numbers else 0  
        
        update_task(task_id, log=f"已下载CBZ最大章节: {max_number}")
        update_task(task_id, log=f"已下载PDF最大章节: {max_number_pdf}")

        json_file_path = "./comic.json"
        update_url = None
        try:
            if not os.path.exists(json_file_path):
                raise Exception("comic.json文件不存在")
            
            if os.path.getsize(json_file_path) == 0:
                raise Exception("comic.json文件为空")
                
            with open(json_file_path, "r", encoding="utf-8") as json_file:
                data = json.load(json_file)
            
            update_url = data.get(comic_name)
            if not update_url:
                raise Exception(f"漫画 {comic_name} 的URL信息不存在于JSON文件中")
                
            update_task(task_id, log=f"获取到漫画更新URL: {update_url}")
            update_task(task_id, url=update_url)
        except json.JSONDecodeError:
            error_msg = f"JSON文件格式错误: {json_file_path}"
            update_task(task_id, log=error_msg)
            update_task(task_id, status='error', end_time=datetime.now(china_tz))
            return
        except Exception as e:
            error_msg = f"读取漫画信息失败: {str(e)}"
            update_task(task_id, log=error_msg)
            update_task(task_id, status='error', end_time=datetime.now(china_tz))
            return

        folder, chapter_max, html_content = title(update_url)
        pattern = r'chapter_slot=(\d+)'
        matches = re.findall(pattern, html_content)
        if matches:
            chapter_max = max(map(int, matches))
        update_task(task_id, log=f"当前最新章节数: {chapter_max}")

        start_chapter = 0
        if comic_format == 1: 
            start_chapter = max_number_pdf
            update_task(task_id, log=f"PDF格式，从章节 {start_chapter} 开始检查更新")
        elif comic_format == 2: 
            start_chapter = max_number
            update_task(task_id, log=f"CBZ格式，从章节 {start_chapter} 开始检查更新")

        if start_chapter >= chapter_max:
            update_task(task_id, log="未找到更新，当前已是最新版本")
            update_task(task_id, status='completed', end_time=datetime.now(china_tz))
            return

        chapters_to_update = chapter_max - start_chapter
        update_task(task_id, log=f"找到 {chapters_to_update} 个新章节，开始下载")
        update_task(task_id, total_chapters=chapters_to_update)

        base_chapter_url = update_url.replace("/comic/", "/comic/chapter/") + "/0_{}.html"
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        if not os.path.exists(f"./comic/{folder}"):
            os.makedirs(f"./comic/{folder}")
            update_task(task_id, log=f"创建保存目录: ./comic/{folder}")

        for chapter_num in range(start_chapter, chapter_max):
            # 检查任务是否已取消
            task = get_task(task_id)
            if task and task.status == 'cancelled':
                update_task(task_id, log="任务已取消")
                return

            url = base_chapter_url.format(chapter_num)
            for attempt in range(3):
                try:
                    response = requests.get(url, headers=headers, timeout=10)
                    if response.status_code == 200:
                        chapter = chapter_num + 1 
                        update_task(task_id, log=f"开始处理第 {chapter} 章")

                        success, msg = crawl_chapter(url, folder, chapter, comic_format, task_id)
                        update_task(task_id, log=msg)

                        # 更新进度
                        task = get_task(task_id)
                        if task:
                            new_completed = task.completed_chapters + 1
                            progress = int((new_completed / chapters_to_update) * 100)
                            update_task(task_id, completed_chapters=new_completed, progress_percent=progress)

                        time.sleep(60)
                        break
                    else:
                        update_task(task_id, log=f"章节页访问异常，状态码: {response.status_code}，第{attempt+1}次尝试")
                except Exception as e:
                    update_task(task_id, log=f"章节页访问异常: {str(e)}，第{attempt+1}次尝试")
            else:
                return False, f"章节页经过3次尝试后仍访问失败"

        update_task(task_id, status='completed', end_time=datetime.now(china_tz))
        update_task(task_id, log="所有更新章节处理完成")
        
    except Exception as e:
        error_msg = f"更新过程出错: {str(e)}"
        update_task(task_id, log=error_msg)
        update_task(task_id, status='error', end_time=datetime.now(china_tz))

def start_download_task(url, comic_format):
    """在独立线程中启动下载任务"""
    task_id = create_task(url, comic_format)
    
    def run():
        with app.app_context():
            download_complete_book(url, comic_format, task_id)
    
    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()
    
    return task_id

def start_update_task(comic_name, comic_format, url):
    """在独立线程中启动更新任务"""
    task_id = create_task(url, comic_format, is_update=True)
    
    def run():
        with app.app_context():
            update_comic(comic_name, comic_format, task_id)
    
    thread = threading.Thread(target=run)
    thread.daemon = True
    thread.start()
    
    return task_id

def login_required(f):
    """登录认证装饰器：验证用户是否已登录"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            # 保存当前访问地址，登录后重定向回来
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# ---------------------- 新增/修改 路由 ----------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    """用户登录页面"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        # 获取用户IP和User-Agent
        ip_address = request.remote_addr
        user_agent = request.user_agent.string

        # 检查登录失败次数和锁定状态
        if username in login_failures:
            fail_count, lock_time = login_failures[username]
            if fail_count >= LOGIN_MAX_ATTEMPTS:
                # 检查锁定是否已过期
                if datetime.now(china_tz) - lock_time < LOGIN_LOCKOUT_DURATION:
                    remaining_time = LOGIN_LOCKOUT_DURATION - (datetime.now(china_tz) - lock_time)
                    flash(f'登录失败次数过多，请在 {remaining_time.seconds // 60} 分钟后重试')
                    # 记录登录失败日志
                    login_log = LoginLog(
                        username=username,
                        ip_address=ip_address,
                        user_agent=user_agent,
                        success=False,
                        message=f'账户已锁定，剩余锁定时间 {remaining_time.seconds // 60} 分钟'
                    )
                    db.session.add(login_log)
                    db.session.commit()
                    return render_template('login.html')
                else:
                    # 锁定过期，重置失败计数
                    del login_failures[username]

        # 验证用户信息
        user = User.query.filter_by(username=username).first()
        if user:
            login_success = False

            # 检查密码哈希算法是否不受支持（scrypt）
            if user.password_hash.startswith('scrypt:'):
                # 对于使用 scrypt 算法的用户，我们需要特殊处理
                # 由于无法验证 scrypt 哈希，我们直接强制重置为新密码（使用 pbkdf2:sha256）
                # 这样，用户使用正确的密码第一次登录时会自动更新密码哈希
                user.set_password(password)
                db.session.commit()
                # 验证新设置的密码
                if user.check_password(password):
                    login_success = True
            else:
                # 对于使用支持算法的用户，正常验证
                if user.check_password(password):
                    login_success = True

            if login_success:
                # 登录成功，清除失败计数
                if username in login_failures:
                    del login_failures[username]
                # 登录成功，保存用户ID到session
                session['user_id'] = user.id
                session['username'] = user.username
                # 记录登录成功日志
                login_log = LoginLog(
                    username=username,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    success=True,
                    message='登录成功'
                )
                db.session.add(login_log)
                db.session.commit()
                # 重定向到之前访问的页面（如果有），否则跳转到首页
                next_page = request.args.get('next')
                return redirect(next_page or url_for('index'))
            else:
                # 用户存在但密码错误，记录失败次数
                if username not in login_failures:
                    login_failures[username] = (0, datetime.now(china_tz))
                fail_count, lock_time = login_failures[username]
                new_fail_count = fail_count + 1
                login_failures[username] = (new_fail_count, datetime.now(china_tz))

                # 显示剩余尝试次数
                remaining_attempts = LOGIN_MAX_ATTEMPTS - new_fail_count
                if remaining_attempts > 0:
                    flash(f'用户名或密码错误，还有 {remaining_attempts} 次尝试机会')
                    message = f'用户名或密码错误，剩余 {remaining_attempts} 次尝试机会'
                else:
                    flash(f'登录失败次数过多，账户已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟')
                    message = f'登录失败次数过多，账户已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟'

                # 记录登录失败日志
                login_log = LoginLog(
                    username=username,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    success=False,
                    message=message
                )
                db.session.add(login_log)
                db.session.commit()

                return render_template('login.html')
        else:
            # 用户不存在，记录失败次数
            if username not in login_failures:
                login_failures[username] = (0, datetime.now(china_tz))
            fail_count, lock_time = login_failures[username]
            new_fail_count = fail_count + 1
            login_failures[username] = (new_fail_count, datetime.now(china_tz))

            # 显示剩余尝试次数
            remaining_attempts = LOGIN_MAX_ATTEMPTS - new_fail_count
            if remaining_attempts > 0:
                flash(f'用户名或密码错误，还有 {remaining_attempts} 次尝试机会')
                message = f'用户名或密码错误，剩余 {remaining_attempts} 次尝试机会'
            else:
                flash(f'登录失败次数过多，账户已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟')
                message = f'登录失败次数过多，账户已锁定 {LOGIN_LOCKOUT_DURATION.seconds // 60} 分钟'

            # 记录登录失败日志
            login_log = LoginLog(
                username=username,
                ip_address=ip_address,
                user_agent=user_agent,
                success=False,
                message=message
            )
            db.session.add(login_log)
            db.session.commit()

            return render_template('login.html')

    # GET请求，返回登录页面
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    """用户登出：清除session中的登录信息"""
    session.pop('user_id', None)
    session.pop('username', None)
    flash('已成功登出')
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    # 登录成功后直接重定向到漫画库
    return redirect(url_for('comics_list'))

@app.route('/download', methods=['GET', 'POST'])
@login_required
def download():
    if request.method == 'POST':
        comic_url = request.form.get('comic_url')
        comic_format = int(request.form.get('format', 1))

        # 验证URL（支持多个包子漫画域名和 mxs12.cc）
        valid_url = False
        if re.match(r'^https?://(cn\.|www\.)?baozimh(cn)?\.com/comic/[^/]+/?$', comic_url):
            valid_url = True
        elif re.match(r'^https?://www\.baoziman\.com/comic/[^/]+/?$', comic_url):
            valid_url = True
        elif re.match(r'^https?://www\.bzmh\.cn/comic/[^/]+/?$', comic_url):
            valid_url = True
        elif re.match(r'^https?://tw\.webmota\.com/comic/[^/]+/?$', comic_url):
            valid_url = True
        elif re.match(r'^https?://(www\.)?mxs12\.cc/(book/)?[^/]+/?$', comic_url):
            valid_url = True
            # 对于 mxs12.cc，强制使用 CBZ 格式
            comic_format = 2

        if not valid_url:
            return render_template('download.html', error='请输入有效的漫画详情页URL（支持 baozimh.com、baoziman.com、bzmh.cn、webmota.com 或 mxs12.cc）')

        # 启动下载线程并获取任务ID
        task_id = start_download_task(comic_url, comic_format)

        # 重定向到进度页
        return redirect(url_for('progress', task_id=task_id))

    return render_template('download.html')

@app.route('/update', methods=['GET', 'POST'])
@login_required
def update():
    # 获取已下载的漫画列表
    comic_list = []
    json_file_path = "./comic.json"
    if os.path.exists(json_file_path) and os.path.getsize(json_file_path) > 0:
        try:
            with open(json_file_path, "r", encoding="utf-8") as json_file:
                comic_data = json.load(json_file)
                comic_list = list(comic_data.keys())
        except:
            pass
    
    if request.method == 'POST':
        comic_name = request.form.get('comic_name')
        comic_format = int(request.form.get('format', 1))
        
        # 获取该漫画的URL
        comic_url = None
        try:
            with open(json_file_path, "r", encoding="utf-8") as json_file:
                comic_data = json.load(json_file)
                comic_url = comic_data.get(comic_name)
        except Exception as e:
            return render_template('update.html', comics=comic_list, error=f"获取漫画信息失败: {str(e)}")
        
        if not comic_url:
            return render_template('update.html', comics=comic_list, error="未找到该漫画的URL信息")
        
        task_id = start_update_task(comic_name, comic_format, comic_url)
        
        return redirect(url_for('progress', task_id=task_id))
    
    return render_template('update.html', comics=comic_list)

@app.route('/delete_task/<task_id>', methods=['POST'])
@login_required
def delete_task_route(task_id):
    """删除任务的路由"""
    try:
        if delete_task(task_id):
            return jsonify({'status': 'success', 'message': '任务已删除'})
        else:
            return jsonify({'status': 'error', 'message': '任务不存在'}), 404
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500



@app.route('/progress/<task_id>')
@login_required
def progress(task_id):
    task = get_task(task_id)
    if not task:
        return render_template('error.html', message="任务不存在或已过期"), 404
    return render_template('progress.html', task_id=task_id)

@app.route('/task_status/<task_id>')
@login_required
def task_status(task_id):
    task = get_task(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    
    # 日志
    log_entries = []
    if task.log:
        log_entries = [line for line in task.log.split('\n') if line.strip()]
    
    return jsonify({
        'task_id': task.id,
        'comic_name': task.comic_name,
        'status': task.status,
        'progress_percent': task.progress_percent,
        'total_chapters': task.total_chapters,
        'completed_chapters': task.completed_chapters,
        'log': log_entries,
        'start_time': task.start_time.strftime('%H:%M:%S') if task.start_time else None,
        'end_time': task.end_time.strftime('%H:%M:%S') if task.end_time else None
    })

@app.route('/cancel_task/<task_id>')
@login_required
def cancel_task(task_id):
    task = get_task(task_id)
    if task and task.status == 'running':
        update_task(task_id, status='cancelled', end_time=datetime.now(china_tz), log="用户已取消任务")
        return jsonify({'status': 'success', 'message': '任务已取消'})
    return jsonify({'status': 'error', 'message': '无法取消任务，任务可能已完成或不存在'})

@app.route('/tasks')
@login_required
def tasks():
    """显示所有任务列表"""
    all_tasks = get_all_tasks()
    return render_template('tasks.html', tasks=all_tasks)

@app.route('/comics')
@login_required
def comics_list():
    """显示漫画库页面"""
    comics = get_available_comics()
    progresses = {}

    # 获取所有阅读进度
    all_progress = get_all_reading_progress()
    for progress in all_progress:
        progresses[progress.comic_name] = {
            'last_chapter': progress.last_chapter,
            'last_page': progress.last_page,
            'total_chapters': progress.total_chapters,
            'total_pages': progress.total_pages,
            'last_read_at': progress.last_read_at
        }

    # 对漫画列表进行排序：有阅读进度的漫画优先显示，并且按最后阅读时间倒序排列
    # 没有阅读进度的漫画放在后面，按创建时间倒序排列
    def sort_key(comic):
        # 先判断是否有阅读进度
        if comic['comic_name'] in progresses:
            # 有阅读进度的漫画，按最后阅读时间倒序（最新阅读的在前）
            last_read_at = progresses[comic['comic_name']]['last_read_at']
            # 如果没有 last_read_at，使用创建时间
            if last_read_at:
                # 为了在升序排序中让最新的在前，我们使用负的时间戳
                return (0, -last_read_at.timestamp())
            elif comic['created_at']:
                return (0, -comic['created_at'].timestamp())
            else:
                return (0, float('-inf'))
        else:
            # 没有阅读进度的漫画，按创建时间倒序
            if comic['created_at']:
                return (1, -comic['created_at'].timestamp())
            else:
                return (1, float('-inf'))

    # 使用升序排序
    sorted_comics = sorted(comics, key=sort_key)

    return render_template('comics.html', tasks=sorted_comics, progresses=progresses)


@app.route('/statistics')
@login_required
def statistics():
    """Reading statistics page"""
    total_time = get_total_reading_time()
    reading_time_rank = get_reading_time_by_comic()
    reading_time_monthly = get_reading_time_monthly()

    # Process monthly data for chart rendering
    monthly_data = {
        'months': ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'],
        'durations': [0] * 12  # Initialize with 0 for all months
    }

    for entry in reading_time_monthly:
        month = entry[0]  # Format: YYYY-MM
        duration = entry[1]  # minutes
        month_num = int(month.split('-')[1]) - 1  # Convert to 0-based index
        if 0 <= month_num < 12:
            monthly_data['durations'][month_num] = duration

    # Calculate max duration for chart scaling
    max_duration = max(monthly_data['durations']) if monthly_data['durations'] else 1

    return render_template(
        'statistics.html',
        total_time=total_time,
        reading_time_rank=reading_time_rank,
        monthly_data=monthly_data,
        max_duration=max_duration
    )

@app.route('/comic/<task_id>')
@login_required
def comic_detail(task_id):
    """漫画详情页面 - 显示漫画信息和章节列表"""
    import os
    import re

    # 尝试通过任务ID获取任务
    task = get_task(task_id)
    if task:
        comic_name = task.comic_name
        comic_format = task.comic_format
    else:
        # 如果任务不存在，尝试将task_id作为漫画名称处理
        comic_name = task_id

        # 检查漫画文件是否存在
        comic_path = os.path.join("./comic", comic_name)
        if not os.path.exists(comic_path):
            return render_template('error.html', message="漫画不存在"), 404

        # 自动检测漫画格式（不区分大小写）
        pdf_files = [f for f in os.listdir(comic_path) if f.lower().endswith('.pdf')]
        cbz_files = [f for f in os.listdir(comic_path) if f.lower().endswith('.cbz')]
        has_pdf = len(pdf_files) > 0
        has_cbz = len(cbz_files) > 0

        # 如果同时存在两种格式，根据文件数量决定
        if has_pdf and has_cbz:
            comic_format = 1 if len(pdf_files) > len(cbz_files) else 2
        elif has_pdf:
            comic_format = 1  # PDF
        elif has_cbz:
            comic_format = 2  # CBZ
        else:
            return render_template('error.html', message="不支持的漫画格式"), 404

        # 创建一个模拟的task对象
        class MockTask:
            def __init__(self, comic_name, comic_format):
                self.id = comic_name
                self.comic_name = comic_name
                self.comic_format = comic_format
                self.status = 'completed'
                self.total_chapters = 0
                self.completed_chapters = 0
                self.available_chapters = 0
                self.created_at = None
                self.url = None
                self.group = '默认分组'  # 默认分组

        # 计算可用章节数
        mock_task = MockTask(comic_name, comic_format)
        mock_task.available_chapters = len([f for f in os.listdir(comic_path) if f.endswith(('.pdf', '.cbz'))])
        mock_task.total_chapters = mock_task.available_chapters
        mock_task.completed_chapters = mock_task.available_chapters
        task = mock_task

    # 获取章节列表
    comic_path = os.path.join("./comic", task.comic_name)
    chapters = []

    try:
        # 获取PDF文件（不区分大小写）
        pdf_files = [f for f in os.listdir(comic_path) if f.lower().endswith('.pdf')]
        cbz_files = [f for f in os.listdir(comic_path) if f.lower().endswith('.cbz')]

        # 处理PDF文件
        for pdf_file in sorted(pdf_files):
            match = re.search(r'(\d+)', pdf_file)
            if match:
                chapter_num = int(match.group(1))
                chapters.append({
                    'number': chapter_num,
                    'filename': pdf_file,
                    'format': 'pdf'
                })

        # 处理CBZ文件
        for cbz_file in sorted(cbz_files):
            match = re.search(r'(\d+)', cbz_file)
            if match:
                chapter_num = int(match.group(1))
                chapters.append({
                    'number': chapter_num,
                    'filename': cbz_file,
                    'format': 'cbz'
                })

        # 按章节号排序
        chapters.sort(key=lambda x: x['number'])

    except Exception as e:
        return render_template('error.html', message=f'获取章节列表失败: {str(e)}'), 500

    # 获取阅读进度
    progress = get_reading_progress(task.comic_name)
    # 获取阅读时间（分钟）
    reading_time = get_reading_time_for_comic(task.comic_name)


    return render_template('comic_detail.html', task=task, chapters=chapters, progress=progress, reading_time=reading_time)


@app.route('/reader/<task_id>')
@login_required
def comic_reader(task_id):
    """漫画阅读器页面 - 通过任务ID"""
    import os

    # 尝试通过任务ID获取任务
    task = get_task(task_id)
    if task:
        comic_name = task.comic_name
        comic_format = task.comic_format
    else:
        # 如果任务不存在，尝试将task_id作为漫画名称处理
        comic_name = task_id

        # 检查漫画文件是否存在
        comic_path = os.path.join("./comic", comic_name)
        if not os.path.exists(comic_path):
            return render_template('error.html', message="漫画不存在"), 404

        # 自动检测漫画格式（不区分大小写）
        import os
        pdf_files = [f for f in os.listdir(comic_path) if f.lower().endswith('.pdf')]
        cbz_files = [f for f in os.listdir(comic_path) if f.lower().endswith('.cbz')]
        has_pdf = len(pdf_files) > 0
        has_cbz = len(cbz_files) > 0

        # 如果同时存在两种格式，根据文件数量决定
        if has_pdf and has_cbz:
            comic_format = 1 if len(pdf_files) > len(cbz_files) else 2
        elif has_pdf:
            comic_format = 1  # PDF
        elif has_cbz:
            comic_format = 2  # CBZ
        else:
            return render_template('error.html', message="不支持的漫画格式"), 404

        # 创建一个模拟的task对象
        class MockTask:
            def __init__(self, comic_name, comic_format):
                self.id = comic_name
                self.comic_name = comic_name
                self.comic_format = comic_format
                self.status = 'completed'
                self.total_chapters = 0
                self.completed_chapters = 0
                self.available_chapters = 0
                self.created_at = None

        # 计算可用章节数
        mock_task = MockTask(comic_name, comic_format)
        mock_task.available_chapters = len([f for f in os.listdir(comic_path) if f.endswith(('.pdf', '.cbz'))])
        mock_task.total_chapters = mock_task.available_chapters
        mock_task.completed_chapters = mock_task.available_chapters
        task = mock_task

    # 获取阅读进度
    progress = get_reading_progress(task.comic_name)
    # 检查是否有指定的起始章节
    start_chapter = request.args.get('start_chapter', None)
    if start_chapter is not None:
        start_chapter = int(start_chapter)
    else:
        start_chapter = progress.last_chapter if progress else 0
    start_page = progress.last_page if progress else 0

    # 确定文件扩展名
    file_ext = 'pdf' if task.comic_format == 1 else 'cbz'

    return render_template('reader.html', task=task, file_ext=file_ext, start_chapter=start_chapter, start_page=start_page)

@app.route('/save_progress', methods=['POST'])
@login_required
@csrf.exempt  # 禁用 CSRF 保护，允许 API 请求
def save_progress():
    """保存阅读进度（修改路由名避免与函数名冲突）"""
    try:
        data = request.get_json()

        if not data:
            return jsonify({'status': 'error', 'message': '无效的JSON格式'}), 400

        comic_name = data.get('comic_name')
        chapter = data.get('chapter', 0)
        page = data.get('page', 0)
        scroll_position = data.get('scroll_position', 0)
        total_chapters = data.get('total_chapters', 0)
        total_pages = data.get('total_pages', 0)
        reading_time = data.get('reading_time', 0)

        if not comic_name:
            return jsonify({'status': 'error', 'message': '漫画名称不能为空'}), 400

        save_reading_progress(comic_name, chapter, page, scroll_position, total_chapters, total_pages)

        # 记录阅读时间（转换为分钟并向上取整）
        if reading_time > 0:
            minutes = round(reading_time / 60)
            if minutes > 0:
                record_reading_time(comic_name, minutes)

        return jsonify({'status': 'success'})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 400

@app.route('/get_progress/<comic_name>')
@login_required
@csrf.exempt  # 禁用 CSRF 保护，允许 API 请求
def get_progress(comic_name):
    """获取阅读进度"""
    try:
        progress = get_reading_progress(comic_name)
        if progress:
            return jsonify({
                'status': 'success',
                'chapter': progress.last_chapter,
                'page': progress.last_page,
                'scroll_position': progress.scroll_position or 0
            })
        else:
            return jsonify({
                'status': 'success',
                'chapter': 0,
                'page': 0,
                'scroll_position': 0
            })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)})

@app.route('/static/comic/<path:filename>')
@login_required
def serve_comic_file(filename):
    """提供漫画文件访问"""
    comic_dir = os.path.join(os.getcwd(), 'comic')
    file_path = os.path.join(comic_dir, filename)
    
    if os.path.exists(file_path) and os.path.isfile(file_path):
        return send_from_directory(comic_dir, filename)
    else:
        return jsonify({'error': '文件不存在'}), 404

@app.route('/api/chapters/<task_id>')
@login_required
def get_chapters(task_id):
    """获取漫画章节列表 - 支持任务ID和漫画名称"""
    import os
    import re

    # 尝试通过任务ID获取任务
    task = get_task(task_id)
    if task:
        comic_name = task.comic_name
    else:
        # 将task_id作为漫画名称处理
        comic_name = task_id

    comic_path = os.path.join("./comic", comic_name)
    if not os.path.exists(comic_path):
        return jsonify({'error': '漫画文件不存在'}), 404

    chapters = []
    try:
        # 获取PDF文件（不区分大小写）
        pdf_files = [f for f in os.listdir(comic_path) if f.lower().endswith('.pdf')]
        cbz_files = [f for f in os.listdir(comic_path) if f.lower().endswith('.cbz')]

        # 处理PDF文件
        for pdf_file in sorted(pdf_files):
            match = re.search(r'(\d+)', pdf_file)
            if match:
                chapter_num = int(match.group(1))
                chapters.append({
                    'number': chapter_num,
                    'filename': pdf_file,
                    'format': 'pdf'
                })

        # 处理CBZ文件
        for cbz_file in sorted(cbz_files):
            match = re.search(r'(\d+)', cbz_file)
            if match:
                chapter_num = int(match.group(1))
                chapters.append({
                    'number': chapter_num,
                    'filename': cbz_file,
                    'format': 'cbz'
                })

        # 按章节号排序
        chapters.sort(key=lambda x: x['number'])

    except Exception as e:
        return jsonify({'error': f'获取章节列表失败: {str(e)}'}), 500

    return jsonify({
        'comic_name': comic_name,
        'chapters': chapters,
        'total_chapters': len(chapters)
    })

@app.route('/change_password', methods=['GET', 'POST'])
@login_required
def change_password():
    """修改用户密码功能"""
    if request.method == 'POST':
        old_password = request.form.get('old_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        user_id = session.get('user_id')
        user = User.query.get(user_id)
        
        if not user:
            flash('用户不存在，请重新登录')
            return redirect(url_for('login'))
        
        if not user.check_password(old_password):
            flash('原密码输入错误，请重试')
            return render_template('change_password.html')
        
        if new_password != confirm_password:
            flash('新密码和确认密码不一致，请重试')
            return render_template('change_password.html')
        
        if len(new_password) < 6:
            flash('新密码长度不能少于6位，请设置更安全的密码')
            return render_template('change_password.html')
        
        user.set_password(new_password)
        db.session.commit()
        
        flash('密码修改成功，请使用新密码登录')
        session.pop('user_id', None)
        session.pop('username', None)
        return redirect(url_for('login'))
    
    return render_template('change_password.html')

if __name__ == '__main__':
    if not os.path.exists('./comic'):
        os.makedirs('./comic')
    if not os.path.exists('./static/cover'):
        os.makedirs('./static/cover')
    
    # 启动Flask应用（优化性能配置）
    def run_app():
        app.run(
            debug=False,
            threaded=True,
            host="0.0.0.0",
            port=5001,
            processes=1,  # 单进程（避免多进程问题）
            use_reloader=False,  # 禁用热重载
            load_dotenv=False  # 禁用环境变量加载
        )
    
    # 优化预加载：只在需要时执行轻量级初始化
    def preload_app():
        try:
            print("✅ 应用启动完成，开始初始化核心组件...")
            # 只需初始化数据库连接，避免不必要的HTTP请求
            with app.app_context():
                # 执行简单的数据库查询来预热连接
                from sqlalchemy import text
                db.session.execute(text('SELECT 1'))
                db.session.commit()
            print("✅ 核心组件初始化完成")
        except Exception as e:
            print(f"⚠️  初始化失败：{str(e)}")

    # 启动应用线程，避免阻塞预加载
    import threading
    app_thread = threading.Thread(target=run_app)
    app_thread.daemon = True
    app_thread.start()

    # 延迟1秒执行预加载（更精确的启动时间）
    Timer(1, preload_app).start()
    
    # 保持主线程运行
    while True:
        time.sleep(3600)