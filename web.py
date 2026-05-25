from flask import Flask, render_template_string, request, redirect, url_for, flash, send_file, session, abort, jsonify
import pymysql
from pymysql import OperationalError, ProgrammingError
import os
import json
import uuid
import pandas as pd
from io import BytesIO
import re
from datetime import datetime, date, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import cloudinary
import cloudinary.uploader

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "devpzlgvg"),
    api_key=os.environ.get("CLOUDINARY_API_KEY", "262731876599449"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "wEe1TrqGdkieRuwS24viPUDpAz8"),
    secure=True,
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'a_secret_key_for_flask_flash_messages')

# 数据库配置（优先读取环境变量，本地开发回退到默认值）
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", "tlxsdy8823166"),
    "database": os.environ.get("DB_NAME", "telecom_maintenance"),
    "charset": 'utf8mb4',
    "cursorclass": pymysql.cursors.DictCursor
}

USER_DB_NAME = os.environ.get("USER_DB_NAME", "telecom_user_system")
USER_DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", "tlxsdy8823166"),
    "database": USER_DB_NAME,
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
}


def _get_db_server_config():
    cfg = USER_DB_CONFIG.copy()
    cfg.pop("database", None)
    return cfg


def ensure_user_system_ready():
    """自动创建账号数据库与账号表"""
    server_conn = None
    user_conn = None
    server_cur = None
    user_cur = None
    try:
        server_conn = pymysql.connect(**_get_db_server_config())
        server_cur = server_conn.cursor()
        server_cur.execute(
            f"CREATE DATABASE IF NOT EXISTS {USER_DB_NAME} "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        server_conn.commit()

        user_conn = pymysql.connect(**USER_DB_CONFIG)
        user_cur = user_conn.cursor()
        user_cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sys_user (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) NOT NULL UNIQUE,
                password VARCHAR(255) NOT NULL,
                role_level INT NOT NULL DEFAULT 1,
                real_name VARCHAR(30) DEFAULT '',
                phone VARCHAR(11) DEFAULT '',
                staff_id INT NULL,
                status TINYINT DEFAULT 1,
                create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                update_time DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        user_cur.execute("SHOW COLUMNS FROM sys_user LIKE 'password'")
        pwd_col = user_cur.fetchone() or {}
        col_type = str(pwd_col.get("Type", "")).lower()
        if col_type.startswith("varchar("):
            try:
                size = int(col_type[col_type.find("(") + 1 : col_type.find(")")])
            except Exception:
                size = 0
            if size < 255:
                user_cur.execute("ALTER TABLE sys_user MODIFY COLUMN password VARCHAR(255) NOT NULL")
        # 兼容旧表结构：补充 staff_id 字段用于账号-员工绑定
        user_cur.execute("SHOW COLUMNS FROM sys_user LIKE 'staff_id'")
        if not user_cur.fetchone():
            user_cur.execute("ALTER TABLE sys_user ADD COLUMN staff_id INT NULL AFTER phone")
        # 保留 users 表，兼容旧版本登录数据
        user_cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                display_name VARCHAR(100) NOT NULL,
                role VARCHAR(20) NOT NULL DEFAULT 'user',
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_users_active (is_active)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

        user_cur.execute("SELECT COUNT(*) AS cnt FROM sys_user")
        user_count = user_cur.fetchone()["cnt"]
        if user_count == 0:
            user_cur.execute(
                """
                INSERT INTO sys_user (username, password, role_level, real_name, phone, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                ("admin", generate_password_hash("admin123456"), 3, "系统管理员", "", 1),
            )
        user_conn.commit()
        return True
    except Exception as e:
        print(f"用户系统初始化失败: {e}")
        return False
    finally:
        if user_cur:
            user_cur.close()
        if user_conn and getattr(user_conn, "open", False):
            user_conn.close()
        if server_cur:
            server_cur.close()
        if server_conn and getattr(server_conn, "open", False):
            server_conn.close()


def get_user_db_connection():
    """获取用户系统数据库连接"""
    if not ensure_user_system_ready():
        return None
    try:
        return pymysql.connect(**USER_DB_CONFIG)
    except OperationalError as e:
        print(f"用户数据库连接失败: {e}")
        return None


def is_logged_in():
    return bool(session.get("user_id"))


def get_current_user():
    if not is_logged_in():
        return None
    return {
        "user_id": session.get("user_id"),
        "username": session.get("username"),
        "display_name": session.get("display_name"),
        "role": session.get("role"),
        "role_level": int(session.get("role_level") or 1),
        "staff_id": session.get("staff_id"),
    }


def get_current_role_level():
    return int(session.get("role_level") or 1)


def require_role_level(min_level):
    """最小权限等级校验装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if get_current_role_level() < min_level:
                flash(f"权限不足：需权限等级{min_level}及以上", "danger")
                return redirect(url_for("tasks_page"))
            return func(*args, **kwargs)
        return wrapper
    return decorator


@app.context_processor
def inject_current_user():
    return {"current_user": get_current_user()}


@app.before_request
def require_login():
    public_endpoints = {"login", "register", "static"}
    if request.endpoint is None:
        return None
    if request.endpoint in public_endpoints:
        return None
    if not is_logged_in():
        next_url = request.path
        return redirect(url_for("login", next=next_url))
    return None

def get_db_connection():
    """获取数据库连接"""
    try:
        connection = pymysql.connect(**DB_CONFIG)
        return connection
    except OperationalError as e:
        print(f"数据库连接失败: {e}")
        return None


def ensure_maintenance_tasks_table(connection):
    """若不存在则创建任务表（与员工表独立）"""
    ddl = """
    CREATE TABLE IF NOT EXISTS maintenance_tasks (
        task_id INT AUTO_INCREMENT PRIMARY KEY,
        title VARCHAR(200) NOT NULL,
        description TEXT,
        priority VARCHAR(10) NOT NULL DEFAULT '中',
        status VARCHAR(20) NOT NULL DEFAULT '待派单',
        due_date DATE NULL,
        assigned_staff_id INT NULL,
        assigned_at DATETIME NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_tasks_status (status),
        INDEX idx_tasks_staff (assigned_staff_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    cur = connection.cursor()
    cur.execute(ddl)
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'photo_paths'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE maintenance_tasks ADD COLUMN photo_paths TEXT NULL "
            "COMMENT 'legacy JSON, migrated to task_images table' AFTER assigned_at"
        )
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'fault_type'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE maintenance_tasks ADD COLUMN fault_type VARCHAR(50) NULL AFTER description"
        )
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'customer_address'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE maintenance_tasks ADD COLUMN customer_address VARCHAR(500) NULL AFTER fault_type"
        )
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'fault_phenomenon'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE maintenance_tasks ADD COLUMN fault_phenomenon VARCHAR(50) NULL AFTER customer_address"
        )
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'resolution_method'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE maintenance_tasks ADD COLUMN resolution_method VARCHAR(50) NULL AFTER fault_phenomenon"
        )
    connection.commit()
    cur.close()


def ensure_task_images_table(connection):
    """工单现场照片元数据表 task_images（避免与库内已有 images 表冲突）"""
    ensure_maintenance_tasks_table(connection)
    ddl = """
    CREATE TABLE IF NOT EXISTS task_images (
        image_id INT AUTO_INCREMENT PRIMARY KEY,
        task_id INT NOT NULL,
        file_path VARCHAR(500) NOT NULL COMMENT 'relative to task_uploads',
        original_filename VARCHAR(255) NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_task_images_task (task_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    cur = connection.cursor()
    cur.execute(ddl)
    connection.commit()
    cur.close()


def migrate_legacy_task_photos(connection):
    """将 maintenance_tasks.photo_paths JSON 迁入 task_images 表（幂等）"""
    cur = connection.cursor()
    try:
        cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'photo_paths'")
        if not cur.fetchone():
            return
        cur.execute(
            """
            SELECT task_id, photo_paths FROM maintenance_tasks
            WHERE photo_paths IS NOT NULL AND TRIM(photo_paths) NOT IN ('', '[]')
            """
        )
        for row in cur.fetchall() or []:
            task_id = row.get("task_id")
            for rel in _parse_legacy_photo_paths(row.get("photo_paths")):
                cur.execute(
                    "SELECT 1 FROM task_images WHERE task_id = %s AND file_path = %s LIMIT 1",
                    (task_id, rel),
                )
                if not cur.fetchone():
                    cur.execute(
                        "INSERT INTO task_images (task_id, file_path) VALUES (%s, %s)",
                        (task_id, rel),
                    )
        connection.commit()
    finally:
        cur.close()


TASK_PRIORITIES = ("低", "中", "高")
TASK_STATUSES = ("待派单", "已派单", "处理中", "待回执", "已完成", "已取消")
FAULT_BUSINESS_TYPES = ("宽带新装", "宽带修障", "宽带移机", "宽带提速", "IPTV新装", "IPTV修障", "电话新装", "电话修障", "智能组网", "设备更换", "线路维护")
FAULT_PHENOMENA = ("光衰过大", "ONU离线", "网速不达标", "IPTV卡顿", "WiFi覆盖差", "电话无声", "线路中断", "其他")
RESOLUTION_METHODS = ("更换光猫", "重新熔纤", "更换尾纤", "重置OLT端口", "更换分光器", "更换网线", "路由器配置", "上门测速", "其他")
FAULT_TYPES_NEED_PHENOMENON = {"宽带修障", "IPTV修障", "电话修障", "线路维护"}
TASK_PHOTO_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
TASK_PHOTO_MAX_BYTES = 5 * 1024 * 1024
TASK_PHOTO_MAX_PER_TASK = 20


def task_upload_base_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_uploads")


def _parse_legacy_photo_paths(raw):
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x]
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data if x]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return []


def delete_task_photo_file(rel_path):
    """删除 Cloudinary 上的图片（public_id 即 rel_path）"""
    if not rel_path:
        return
    try:
        cloudinary.uploader.destroy(rel_path)
    except Exception:
        pass


def save_uploaded_task_photos(task_id, files_storage_list, max_count=None):
    """上传至 Cloudinary，返回 [(public_id, 原始文件名), ...]"""
    saved = []
    limit = max_count if max_count is not None else TASK_PHOTO_MAX_PER_TASK
    if not files_storage_list or limit <= 0:
        return saved
    for f in files_storage_list:
        if not f or not getattr(f, "filename", None):
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in TASK_PHOTO_ALLOWED_EXT:
            continue
        data = f.read()
        if len(data) > TASK_PHOTO_MAX_BYTES:
            continue
        try:
            result = cloudinary.uploader.upload(
                data,
                folder=f"telecom/task_{task_id}",
                resource_type="image",
            )
            public_id = result["public_id"]
            saved.append((public_id, f.filename))
        except Exception:
            continue
        if len(saved) >= limit:
            break
    return saved


def _format_image_created_at(value):
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def images_to_json_list(rows):
    """将 task_images 表记录转为前端展示用的列表"""
    out = []
    for r in rows or []:
        fp = r.get("file_path") or ""
        # fp 存的是 Cloudinary public_id，生成安全 CDN URL
        try:
            url = cloudinary.utils.cloudinary_url(fp, secure=True)[0]
        except Exception:
            url = ""
        out.append(
            {
                "image_id": r.get("image_id"),
                "task_id": r.get("task_id"),
                "file_path": fp,
                "url": url,
                "original_filename": r.get("original_filename") or "",
                "created_at": _format_image_created_at(r.get("created_at")),
            }
        )
    return out


def fetch_task_image_rows(cursor, task_id):
    cursor.execute(
        """
        SELECT image_id, task_id, file_path, original_filename, created_at
        FROM task_images WHERE task_id = %s ORDER BY image_id ASC
        """,
        (task_id,),
    )
    return cursor.fetchall() or []


def count_task_images(cursor, task_id):
    cursor.execute("SELECT COUNT(*) AS c FROM task_images WHERE task_id = %s", (task_id,))
    row = cursor.fetchone() or {}
    return int(row.get("c") or 0)


def insert_task_images(cursor, task_id, files_storage_list):
    """上传文件写入磁盘并插入 task_images 表，返回 (新增行列表, 提示信息)"""
    current = count_task_images(cursor, task_id)
    remaining = TASK_PHOTO_MAX_PER_TASK - current
    if remaining <= 0:
        return [], f"该工单照片已达上限（{TASK_PHOTO_MAX_PER_TASK} 张）"
    disk_saved = save_uploaded_task_photos(task_id, files_storage_list, max_count=remaining)
    if not disk_saved:
        return [], "未保存任何有效图片（请检查格式 jpg/png/gif/webp 与单张 5MB 限制）"
    inserted = []
    for rel, orig in disk_saved:
        cursor.execute(
            """
            INSERT INTO task_images (task_id, file_path, original_filename)
            VALUES (%s, %s, %s)
            """,
            (task_id, rel, orig[:255] if orig else None),
        )
        inserted.append(
            {
                "image_id": cursor.lastrowid,
                "task_id": task_id,
                "file_path": rel,
                "original_filename": orig,
            }
        )
    msg = f"成功上传 {len(inserted)} 张"
    if len(files_storage_list) > len(disk_saved):
        msg += "（部分文件未通过校验或超出数量上限）"
    return inserted, msg


def delete_task_image_by_id(cursor, task_id, image_id):
    cursor.execute(
        "SELECT file_path FROM task_images WHERE image_id = %s AND task_id = %s",
        (image_id, task_id),
    )
    row = cursor.fetchone()
    if not row:
        return False
    delete_task_photo_file(row.get("file_path"))
    cursor.execute(
        "DELETE FROM task_images WHERE image_id = %s AND task_id = %s",
        (image_id, task_id),
    )
    return cursor.rowcount > 0


def fetch_task_for_detail(cursor, task_id):
    cursor.execute(
        """
        SELECT t.*, s.full_name AS assigned_staff_name
        FROM maintenance_tasks t
        LEFT JOIN staff_basic_info s ON t.assigned_staff_id = s.staff_id
        WHERE t.task_id = %s
        """,
        (task_id,),
    )
    return cursor.fetchone()


STAT_PERIOD_LABELS = {"day": "日报", "week": "周报", "month": "月报"}
STAT_VIEW_LABELS = {
    "time": "时间趋势",
    "staff": "人员对比",
    "team": "班组汇总",
    "region": "区域密度",
}
STAT_TASK_AGG_SQL = """
    COUNT(*) AS total,
    SUM(CASE WHEN t.status = '已完成' THEN 1 ELSE 0 END) AS done,
    SUM(CASE WHEN t.status = '已取消' THEN 1 ELSE 0 END) AS cancelled,
    SUM(CASE WHEN t.status = '处理中' THEN 1 ELSE 0 END) AS in_progress,
    SUM(CASE WHEN t.status = '待派单' THEN 1 ELSE 0 END) AS pending,
    SUM(CASE WHEN t.status = '已派单' THEN 1 ELSE 0 END) AS assigned,
    SUM(CASE WHEN t.status = '待回执' THEN 1 ELSE 0 END) AS awaiting_receipt
"""


def _parse_stat_date(value, default):
    if not value:
        return default
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return default


def _completion_rate(done, total, cancelled):
    effective = int(total or 0) - int(cancelled or 0)
    if effective <= 0:
        return None
    return round(100.0 * int(done or 0) / effective, 1)


def parse_task_stat_filters(req):
    """解析统计筛选：时间粒度、分析维度、日期范围"""
    period = req.args.get("period", "day").strip()
    if period not in STAT_PERIOD_LABELS:
        period = "day"
    view = req.args.get("view", "time").strip()
    if view not in STAT_VIEW_LABELS:
        view = "time"
    today = date.today()
    date_to = _parse_stat_date(req.args.get("date_to"), today)
    date_from = _parse_stat_date(req.args.get("date_from"), None)
    if date_from is None:
        if period == "day":
            date_from = date_to - timedelta(days=29)
        elif period == "week":
            date_from = date_to - timedelta(days=83)
        else:
            date_from = date_to - timedelta(days=364)
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    start_dt = datetime.combine(date_from, datetime.min.time())
    end_dt = datetime.combine(date_to, datetime.max.time())
    return {
        "period": period,
        "view": view,
        "date_from": date_from,
        "date_to": date_to,
        "date_from_str": date_from.isoformat(),
        "date_to_str": date_to.isoformat(),
        "start_dt": start_dt,
        "end_dt": end_dt,
        "period_label": STAT_PERIOD_LABELS[period],
        "view_label": STAT_VIEW_LABELS[view],
    }


def _stat_row_from_record(rec, label=None):
    total = int(rec.get("total") or 0)
    done = int(rec.get("done") or 0)
    cancelled = int(rec.get("cancelled") or 0)
    in_progress = int(rec.get("in_progress") or 0)
    pending = int(rec.get("pending") or 0)
    assigned = int(rec.get("assigned") or 0)
    awaiting_receipt = int(rec.get("awaiting_receipt") or 0)
    effective = total - cancelled
    row = {
        "label": label if label is not None else (rec.get("label") or "—"),
        "total": total,
        "done": done,
        "cancelled": cancelled,
        "in_progress": in_progress,
        "pending": pending,
        "assigned": assigned,
        "awaiting_receipt": awaiting_receipt,
        "effective": effective,
        "completion_pct": _completion_rate(done, total, cancelled),
        "team_id": rec.get("team_id"),
        "region_id": rec.get("region_id"),
        "staff_id": rec.get("staff_id"),
    }
    return row


def _fetch_task_stat_summary(cursor, start_dt, end_dt):
    cursor.execute(
        f"""
        SELECT {STAT_TASK_AGG_SQL}
        FROM maintenance_tasks t
        WHERE t.created_at >= %s AND t.created_at <= %s
        """,
        (start_dt, end_dt),
    )
    raw = cursor.fetchone() or {}
    total = int(raw.get("total") or 0)
    done = int(raw.get("done") or 0)
    cancelled = int(raw.get("cancelled") or 0)
    return {
        "total": total,
        "done": done,
        "cancelled": cancelled,
        "in_progress": int(raw.get("in_progress") or 0),
        "pending": int(raw.get("pending") or 0),
        "assigned": int(raw.get("assigned") or 0),
        "awaiting_receipt": int(raw.get("awaiting_receipt") or 0),
        "effective": total - cancelled,
        "completion_pct": _completion_rate(done, total, cancelled),
    }


def _fetch_task_stat_rows(cursor, period, view, start_dt, end_dt):
    params = [start_dt, end_dt]
    if view == "time":
        if period == "day":
            cursor.execute(
                f"""
                SELECT DATE_FORMAT(t.created_at, '%%Y-%%m-%%d') AS label,
                       DATE(t.created_at) AS sort_key,
                       {STAT_TASK_AGG_SQL}
                FROM maintenance_tasks t
                WHERE t.created_at >= %s AND t.created_at <= %s
                GROUP BY DATE(t.created_at), label
                ORDER BY sort_key ASC
                """,
                params,
            )
        elif period == "week":
            cursor.execute(
                f"""
                SELECT
                    DATE_FORMAT(
                        DATE_SUB(DATE(t.created_at), INTERVAL WEEKDAY(t.created_at) DAY),
                        '%%Y-%%m-%%d'
                    ) AS sort_key,
                    CONCAT(
                        DATE_FORMAT(
                            DATE_SUB(DATE(t.created_at), INTERVAL WEEKDAY(t.created_at) DAY),
                            '%%Y-%%m-%%d'
                        ),
                        ' 周'
                    ) AS label,
                    {STAT_TASK_AGG_SQL}
                FROM maintenance_tasks t
                WHERE t.created_at >= %s AND t.created_at <= %s
                GROUP BY sort_key, label
                ORDER BY sort_key ASC
                """,
                params,
            )
        else:
            cursor.execute(
                f"""
                SELECT DATE_FORMAT(t.created_at, '%%Y-%%m') AS label,
                       DATE_FORMAT(t.created_at, '%%Y-%%m') AS sort_key,
                       {STAT_TASK_AGG_SQL}
                FROM maintenance_tasks t
                WHERE t.created_at >= %s AND t.created_at <= %s
                GROUP BY label, sort_key
                ORDER BY sort_key ASC
                """,
                params,
            )
    elif view == "staff":
        cursor.execute(
            f"""
            SELECT
                COALESCE(s.full_name, '未指派') AS label,
                t.assigned_staff_id AS staff_id,
                s.team_id,
                s.region_id,
                {STAT_TASK_AGG_SQL}
            FROM maintenance_tasks t
            LEFT JOIN staff_basic_info s ON t.assigned_staff_id = s.staff_id
            WHERE t.created_at >= %s AND t.created_at <= %s
            GROUP BY t.assigned_staff_id, s.full_name, s.team_id, s.region_id
            ORDER BY total DESC, done DESC
            """,
            params,
        )
    elif view == "team":
        cursor.execute(
            f"""
            SELECT
                CASE
                    WHEN s.team_id IS NULL THEN '未分班'
                    ELSE CONCAT('班组 ', s.team_id)
                END AS label,
                COALESCE(s.team_id, -1) AS team_id,
                {STAT_TASK_AGG_SQL}
            FROM maintenance_tasks t
            LEFT JOIN staff_basic_info s ON t.assigned_staff_id = s.staff_id
            WHERE t.created_at >= %s AND t.created_at <= %s
            GROUP BY COALESCE(s.team_id, -1), label
            ORDER BY total DESC, done DESC
            """,
            params,
        )
    else:
        cursor.execute(
            f"""
            SELECT
                CASE
                    WHEN s.region_id IS NULL THEN '未分区'
                    ELSE CONCAT('区域 ', s.region_id)
                END AS label,
                COALESCE(s.region_id, -1) AS region_id,
                {STAT_TASK_AGG_SQL}
            FROM maintenance_tasks t
            LEFT JOIN staff_basic_info s ON t.assigned_staff_id = s.staff_id
            WHERE t.created_at >= %s AND t.created_at <= %s
            GROUP BY COALESCE(s.region_id, -1), label
            ORDER BY total DESC, done DESC
            """,
            params,
        )
    return [_stat_row_from_record(r) for r in (cursor.fetchall() or [])]


def _apply_region_density(rows):
    if not rows:
        return rows
    grand = sum(r["total"] for r in rows)
    if grand <= 0:
        return rows
    for r in rows:
        r["density_pct"] = round(100.0 * r["total"] / grand, 1)
    rows_sorted = sorted(rows, key=lambda x: x["total"], reverse=True)
    for i, r in enumerate(rows_sorted, start=1):
        r["density_rank"] = i
    return rows_sorted


def build_task_statistics(cursor, filters):
    summary = _fetch_task_stat_summary(cursor, filters["start_dt"], filters["end_dt"])
    rows = _fetch_task_stat_rows(
        cursor, filters["period"], filters["view"], filters["start_dt"], filters["end_dt"]
    )
    if filters["view"] == "region":
        rows = _apply_region_density(rows)

    chart_limit = 20 if filters["view"] == "time" else 15
    chart_rows = rows[-chart_limit:] if filters["view"] == "time" else rows[:chart_limit]

    labels = [r["label"] for r in chart_rows]
    totals = [r["total"] for r in chart_rows]
    done_vals = [r["done"] for r in chart_rows]
    rates = [r["completion_pct"] if r["completion_pct"] is not None else 0 for r in chart_rows]

    table_columns = _stat_table_columns(filters["view"], filters["period"])
    export_rows = _stat_rows_for_export(rows, filters["view"])
    return {
        **filters,
        "summary": summary,
        "rows": rows,
        "table_columns": table_columns,
        "export_rows": export_rows,
        "chart": {
            "labels": labels,
            "totals": totals,
            "done": done_vals,
            "rates": rates,
        },
    }


def _fetch_kpi_summary(cursor, start_dt, end_dt):
    """电信行业KPI：及时率、平均修复时长、超时工单数"""
    # 及时率：有截止日期且已完成的工单中，在截止日期前完成的比例
    cursor.execute(
        """
        SELECT
            COUNT(*) AS with_due,
            SUM(CASE WHEN DATE(updated_at) <= due_date THEN 1 ELSE 0 END) AS on_time
        FROM maintenance_tasks
        WHERE status = '已完成'
          AND due_date IS NOT NULL
          AND created_at >= %s AND created_at <= %s
        """,
        (start_dt, end_dt),
    )
    r = cursor.fetchone() or {}
    with_due = int(r.get("with_due") or 0)
    on_time = int(r.get("on_time") or 0)
    on_time_rate = round(100.0 * on_time / with_due, 1) if with_due > 0 else None

    # 平均修复时长（小时）：从派单到完工
    cursor.execute(
        """
        SELECT AVG(TIMESTAMPDIFF(MINUTE, assigned_at, updated_at)) AS avg_min
        FROM maintenance_tasks
        WHERE status = '已完成'
          AND assigned_at IS NOT NULL
          AND created_at >= %s AND created_at <= %s
        """,
        (start_dt, end_dt),
    )
    r2 = cursor.fetchone() or {}
    avg_min = r2.get("avg_min")
    avg_hours = round(float(avg_min) / 60, 1) if avg_min is not None else None

    # 超时工单：有截止日期、未完成未取消、截止日期已过
    cursor.execute(
        """
        SELECT COUNT(*) AS overdue
        FROM maintenance_tasks
        WHERE due_date < CURDATE()
          AND status NOT IN ('已完成', '已取消')
        """
    )
    r3 = cursor.fetchone() or {}
    overdue = int(r3.get("overdue") or 0)

    return {
        "on_time_rate": on_time_rate,
        "on_time": on_time,
        "with_due": with_due,
        "avg_repair_hours": avg_hours,
        "overdue_count": overdue,
    }


def _stat_table_columns(view, period):
    if view == "time":
        period_name = {"day": "日期", "week": "周次", "month": "月份"}[period]
        return [
            {"key": "label", "title": period_name},
            {"key": "total", "title": "工单量"},
            {"key": "done", "title": "已完成"},
            {"key": "in_progress", "title": "处理中"},
            {"key": "assigned", "title": "已派单"},
            {"key": "pending", "title": "待派单"},
            {"key": "awaiting_receipt", "title": "待回执"},
            {"key": "cancelled", "title": "已取消"},
            {"key": "completion_pct", "title": "完成率(%)"},
        ]
    if view == "staff":
        return [
            {"key": "label", "title": "装维人员"},
            {"key": "team_id", "title": "班组ID"},
            {"key": "region_id", "title": "区域ID"},
            {"key": "total", "title": "工单量"},
            {"key": "done", "title": "已完成"},
            {"key": "completion_pct", "title": "完成率(%)"},
        ]
    if view == "team":
        return [
            {"key": "label", "title": "班组"},
            {"key": "total", "title": "工单量"},
            {"key": "done", "title": "已完成"},
            {"key": "effective", "title": "有效工单"},
            {"key": "completion_pct", "title": "完成率(%)"},
        ]
    return [
        {"key": "density_rank", "title": "密度排名"},
        {"key": "label", "title": "区域"},
        {"key": "total", "title": "工单量"},
        {"key": "density_pct", "title": "占比(%)"},
        {"key": "done", "title": "已完成"},
        {"key": "completion_pct", "title": "完成率(%)"},
    ]


def _stat_rows_for_export(rows, view):
    out = []
    for r in rows:
        item = {
            "维度": r["label"],
            "工单量": r["total"],
            "已完成": r["done"],
            "处理中": r["in_progress"],
            "已派单": r["assigned"],
            "待派单": r["pending"],
            "待回执": r["awaiting_receipt"],
            "已取消": r["cancelled"],
            "有效工单": r["effective"],
            "完成率(%)": r["completion_pct"] if r["completion_pct"] is not None else "",
        }
        if view == "staff":
            item["班组ID"] = r.get("team_id") if r.get("team_id") is not None else ""
            item["区域ID"] = r.get("region_id") if r.get("region_id") is not None else ""
            item["员工ID"] = r.get("staff_id") if r.get("staff_id") is not None else ""
        if view == "region":
            item["密度排名"] = r.get("density_rank", "")
            item["占比(%)"] = r.get("density_pct", "")
        if view == "team":
            item.pop("处理中", None)
            item.pop("待派单", None)
            item.pop("已派单", None)
            item.pop("待回执", None)
        out.append(item)
    return out


def export_task_statistics_xlsx(stat_data):
    output = BytesIO()
    summary = stat_data["summary"]
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        meta = pd.DataFrame(
            [
                ["装维任务数据统计报表", ""],
                ["统计视图", stat_data["view_label"]],
                ["时间粒度", stat_data["period_label"]],
                ["开始日期", stat_data["date_from_str"]],
                ["结束日期", stat_data["date_to_str"]],
                ["导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                ["", ""],
                ["指标", "数值"],
                ["工单总量", summary["total"]],
                ["已完成", summary["done"]],
                ["已取消", summary["cancelled"]],
                ["有效工单", summary["effective"]],
                ["处理中", summary["in_progress"]],
                ["已派单", summary.get("assigned", 0)],
                ["待派单", summary["pending"]],
                ["待回执", summary.get("awaiting_receipt", 0)],
                [
                    "完成率(%)",
                    summary["completion_pct"] if summary["completion_pct"] is not None else "",
                ],
            ]
        )
        meta.to_excel(writer, sheet_name="统计说明", index=False, header=False)
        detail_df = pd.DataFrame(stat_data["export_rows"])
        if detail_df.empty:
            detail_df = pd.DataFrame([{"提示": "所选条件下暂无数据"}])
        detail_df.to_excel(writer, sheet_name="统计数据", index=False)
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col_idx, column_cells in enumerate(ws.columns, 1):
                max_len = 0
                col_letter = ws.cell(row=1, column=col_idx).column_letter
                for cell in column_cells:
                    try:
                        max_len = max(max_len, len(str(cell.value or "")))
                    except Exception:
                        pass
                ws.column_dimensions[col_letter].width = min(max(max_len + 2, 10), 36)
    output.seek(0)
    return output


# 员工信息导入管理类
class StaffInfoManager:
    def __init__(self):
        self.connection = get_db_connection()
        if self.connection:
            self.cursor = self.connection.cursor()
    
    def _check_duplicates(self, row_data):
        """检查数据库中是否存在重复数据"""
        check_fields = [
            ("id_card", "身份证号", row_data["id_card"]),
            ("full_name", "姓名", row_data["full_name"]),
            ("private_phone", "私人电话号", row_data.get("private_phone")),
            ("work_phone", "工作电话号", row_data.get("work_phone")),
            ("emergency_contact", "紧急联系人", row_data.get("emergency_contact")),
            ("emergency_phone", "紧急联系电话", row_data.get("emergency_phone"))
        ]
        
        for field, field_name, value in check_fields:
            if not value:
                continue
                
            try:
                self.cursor.execute(f"SELECT COUNT(*) as count FROM staff_basic_info WHERE {field} = %s", (value,))
                result = self.cursor.fetchone()
                
                if result['count'] > 0:
                    return f"{field_name}'{value}'已存在"
            except ProgrammingError as e:
                return f"检查{field_name}重复时出错: {str(e)}"
        
        return None
    
    def import_from_excel(self, file_path, batch_size=100):
        """从Excel文件导入员工基础信息"""
        try:
            # 自动判断文件格式并选择合适的引擎
            file_ext = os.path.splitext(file_path)[1].lower()
            if file_ext == '.xlsx':
                engine = 'openpyxl'
            elif file_ext == '.xls':
                engine = 'xlrd'
            else:
                return {"status": "error", "message": f"不支持的文件格式: {file_ext}，仅支持.xls和.xlsx"}

            # 读取Excel文件
            df = pd.read_excel(
                file_path,
                engine=engine,
                dtype=str,
                keep_default_na=False,
                header=0
            )

            # 手动过滤空行
            df = df.dropna(how='all')

            # 校验必填列
            required_columns = ["full_name", "gender", "id_card", "entry_date", "position"]
            missing_cols = [col for col in required_columns if col not in df.columns]
            if missing_cols:
                return {"status": "error", "message": f"缺少必填列：{','.join(missing_cols)}"}

            success_count = 0
            fail_count = 0
            fail_reasons = []
            batch_count = 0

            for idx, row in df.iterrows():
                # 统一清理所有字段的前后空格
                row_data = {k: v.strip() if isinstance(v, str) else v for k, v in row.to_dict().items()}
                row_num = idx + 2

                # 检查重复数据
                duplicate_reason = self._check_duplicates(row_data)
                if duplicate_reason:
                    fail_reasons.append(f"行{row_num}：{duplicate_reason}，不导入")
                    fail_count += 1
                    continue

                # 验证身份证号
                if not re.match(r'^\d{17}[\dXx]$', str(row_data["id_card"])):
                    fail_reasons.append(f"行{row_num}：身份证号格式错误（需18位，含数字或X）")
                    fail_count += 1
                    continue

                # 验证性别
                valid_genders = ["男", "女", "其他"]
                if row_data["gender"] not in valid_genders:
                    fail_reasons.append(f"行{row_num}：性别必须为{','.join(valid_genders)}")
                    fail_count += 1
                    continue

                # 验证手机号（11位数字）
                phone_pattern = r'^\d{11}$'
                if row_data.get("private_phone") and row_data.get("private_phone") != "" and not re.match(phone_pattern, row_data["private_phone"]):
                    fail_reasons.append(f"行{row_num}：私人电话格式错误（需11位数字）")
                    fail_count += 1
                    continue
                if row_data.get("work_phone") and row_data.get("work_phone") != "" and not re.match(phone_pattern, row_data["work_phone"]):
                    fail_reasons.append(f"行{row_num}：工作电话格式错误（需11位数字）")
                    fail_count += 1
                    continue
                if row_data.get("emergency_phone") and row_data.get("emergency_phone") != "" and not re.match(phone_pattern, row_data["emergency_phone"]):
                    fail_reasons.append(f"行{row_num}：紧急联系电话格式错误（需11位数字）")
                    fail_count += 1
                    continue

                # 验证学历
                valid_education = ["高中及以下", "大专", "本科", "硕士", "博士"]
                if row_data.get("education") and row_data.get("education") != "" and row_data["education"] not in valid_education:
                    fail_reasons.append(f"行{row_num}：学历必须为{','.join(valid_education)}")
                    fail_count += 1
                    continue

                # 验证岗位
                valid_positions = ["装维工程师", "班组长", "区域主管", "HR专员", "财务专员", "系统管理员"]
                if row_data["position"] not in valid_positions:
                    fail_reasons.append(f"行{row_num}：岗位必须为：{','.join(valid_positions)}（当前值：{row_data['position']}）")
                    fail_count += 1
                    continue

                # 验证入职日期
                try:
                    entry_date = pd.to_datetime(row_data["entry_date"]).strftime("%Y-%m-%d")
                except:
                    fail_reasons.append(f"行{row_num}：入职日期格式错误（建议YYYY-MM-DD）")
                    fail_count += 1
                    continue

                # 处理离职日期
                departure_date = row_data.get("departure_date")
                if departure_date and departure_date != "":
                    try:
                        departure_date = pd.to_datetime(departure_date).strftime("%Y-%m-%d")
                    except:
                        fail_reasons.append(f"行{row_num}：离职日期格式错误（建议YYYY-MM-DD）")
                        fail_count += 1
                        continue
                else:
                    departure_date = None

                # 处理在职状态
                is_active = True if not departure_date else False

                # 处理team_id
                team_id = row_data.get("team_id")
                if team_id and team_id != "":
                    try:
                        team_id = int(team_id)
                    except:
                        fail_reasons.append(f"行{row_num}：班组ID（team_id）必须为整数")
                        fail_count += 1
                        continue
                else:
                    team_id = None

                # 处理region_id
                region_id = row_data.get("region_id")
                if region_id and region_id != "":
                    try:
                        region_id = int(region_id)
                    except:
                        fail_reasons.append(f"行{row_num}：区域ID（region_id）必须为整数")
                        fail_count += 1
                        continue
                else:
                    region_id = None

                # 插入数据
                try:
                    self.cursor.execute('''
                    INSERT INTO staff_basic_info 
                    (full_name, gender, id_card, private_phone, work_phone, emergency_contact, emergency_phone,
                     education, entry_date, departure_date, is_active, region_id, team_id, position)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                    full_name = %s, gender = %s, private_phone = %s, work_phone = %s,
                    emergency_contact = %s, emergency_phone = %s, education = %s, entry_date = %s,
                    departure_date = %s, is_active = %s, region_id = %s, team_id = %s, position = %s
                    ''', (
                        row_data["full_name"],
                        row_data["gender"],
                        row_data["id_card"],
                        row_data.get("private_phone", ""),
                        row_data.get("work_phone", ""),
                        row_data.get("emergency_contact", ""),
                        row_data.get("emergency_phone", ""),
                        row_data.get("education", ""),
                        entry_date,
                        departure_date,
                        is_active,
                        region_id,
                        team_id,
                        row_data["position"],
                        # 更新部分参数
                        row_data["full_name"],
                        row_data["gender"],
                        row_data.get("private_phone", ""),
                        row_data.get("work_phone", ""),
                        row_data.get("emergency_contact", ""),
                        row_data.get("emergency_phone", ""),
                        row_data.get("education", ""),
                        entry_date,
                        departure_date,
                        is_active,
                        region_id,
                        team_id,
                        row_data["position"]
                    ))
                    success_count += 1
                    batch_count += 1

                    # 达到批量提交阈值时提交事务
                    if batch_count >= batch_size:
                        self.connection.commit()
                        batch_count = 0

                except ProgrammingError as e:
                    fail_reasons.append(f"行{row_num}：数据库错误 - {str(e)}")
                    fail_count += 1
                    self.connection.rollback()
                    continue

            # 提交剩余未提交的事务
            if batch_count > 0:
                self.connection.commit()

            return {
                "status": "success",
                "success_count": success_count,
                "fail_count": fail_count,
                "fail_reasons": fail_reasons,
                "message": f"导入完成，成功{success_count}条，失败{fail_count}条"
            }

        except Exception as e:
            return {"status": "error", "message": f"导入失败：{str(e)}"}
    
    def close(self):
        """关闭连接"""
        if self.connection and self.connection.open:
            self.cursor.close()
            self.connection.close()

# 基础信息导出类
class StaffInfoExporter:
    def __init__(self):
        self.connection = get_db_connection()
        if self.connection:
            self.cursor = self.connection.cursor()
    
    def export_to_xlsx(self, condition=None):
        """导出为xlsx格式"""
        try:
            # 查询数据
            query_sql = """
            SELECT 
                staff_id, full_name, gender, id_card, birth_date,
                private_phone, work_phone, emergency_contact, emergency_phone,
                education, entry_date, departure_date, is_active,
                region_id, team_id, position, created_at, updated_at
            FROM staff_basic_info
            """
            params = []
            
            if condition and isinstance(condition, dict):
                where_clause = []
                valid_fields = ["staff_id", "full_name", "gender", "id_card", "private_phone",
                               "work_phone", "emergency_contact", "emergency_phone", "education",
                               "entry_date", "departure_date", "is_active", "region_id", "team_id", "position"]
                for key, value in condition.items():
                    if key in valid_fields:
                        where_clause.append(f"{key} = %s")
                        params.append(value)
                if where_clause:
                    query_sql += " WHERE " + " AND ".join(where_clause)
            
            self.cursor.execute(query_sql, params)
            data = self.cursor.fetchall()
            
            if not data:
                return {"status": "warning", "message": "没有查询到数据", "record_count": 0}
            
            # 处理数据格式
            df = pd.DataFrame(data)
            
            # 格式化日期
            date_columns = ["birth_date", "entry_date", "departure_date", "created_at", "updated_at"]
            for col in date_columns:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda x: x.strftime("%Y-%m-%d")
                        if isinstance(x, (pd.Timestamp, datetime, date)) else ""
                    )
            
            # 格式化在职状态
            if "is_active" in df.columns:
                df["is_active"] = df["is_active"].map({True: "在职", False: "离职", None: ""})
            
            # 创建 Excel 文件
            output = BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name="员工基础信息", index=False)
            
            output.seek(0)
            
            return {
                "status": "success",
                "data": output,
                "message": f"导出成功（{len(data)}条记录）",
                "record_count": len(data)
            }
            
        except Exception as e:
            return {"status": "error", "message": f"导出失败：{str(e)}", "record_count": 0}
    
    def close(self):
        """关闭连接"""
        if self.connection and self.connection.open:
            self.cursor.close()
            self.connection.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect(url_for("tasks_page"))

    next_url = request.args.get("next") or request.form.get("next") or url_for("tasks_page")
    if not str(next_url).startswith("/"):
        next_url = url_for("tasks_page")
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if not username or not password:
            flash("请输入账号和密码", "danger")
            return redirect(url_for("login", next=next_url))

        connection = get_user_db_connection()
        if not connection:
            flash("账号数据库连接失败", "danger")
            return redirect(url_for("login", next=next_url))

        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT id AS user_id, username, real_name AS display_name, role_level, status, password, staff_id
                FROM sys_user
                WHERE username = %s
                """,
                (username,),
            )
            user = cursor.fetchone()
            source = "sys_user"
            if not user:
                # 兼容旧 users 表数据
                cursor.execute(
                    """
                    SELECT user_id, username, display_name, role, is_active, password_hash
                    FROM users
                    WHERE username = %s
                    """,
                    (username,),
                )
                user = cursor.fetchone()
                source = "users"
            if not user:
                flash("账号不存在", "danger")
                return redirect(url_for("login", next=next_url))

            if source == "sys_user":
                if int(user.get("status", 0)) != 1:
                    flash("账号已被禁用，请联系管理员", "danger")
                    return redirect(url_for("login", next=next_url))
                # 兼容明文旧密码与哈希密码
                raw_pwd = user.get("password") or ""
                verified = False
                try:
                    verified = check_password_hash(raw_pwd, password)
                except Exception:
                    verified = False
                if not verified and raw_pwd == password:
                    verified = True
                if not verified:
                    flash("密码错误", "danger")
                    return redirect(url_for("login", next=next_url))

                role_level = int(user.get("role_level") or 1)
                role = "admin" if role_level >= 3 else ("leader" if role_level == 2 else "user")
                display_name = user.get("display_name") or user["username"]
            else:
                if not user["is_active"]:
                    flash("账号已被禁用，请联系管理员", "danger")
                    return redirect(url_for("login", next=next_url))
                if not check_password_hash(user["password_hash"], password):
                    flash("密码错误", "danger")
                    return redirect(url_for("login", next=next_url))
                role = user["role"]
                display_name = user["display_name"]
                role_level = 3 if role == "admin" else (2 if role == "leader" else 1)

            session["user_id"] = user["user_id"]
            session["username"] = user["username"]
            session["display_name"] = display_name
            session["role"] = role
            session["role_level"] = role_level
            session["staff_id"] = user.get("staff_id")
            flash(f"欢迎回来，{display_name}", "success")
            return redirect(next_url)
        except Exception as e:
            flash(f"登录失败: {str(e)}", "danger")
            return redirect(url_for("login", next=next_url))
        finally:
            if cursor:
                cursor.close()
            if connection and getattr(connection, "open", False):
                connection.close()

    return render_template_string(LOGIN_HTML, next_url=next_url)


@app.route("/register", methods=["GET", "POST"])
def register():
    if is_logged_in():
        return redirect(url_for("tasks_page"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        display_name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "").strip()
        confirm_password = request.form.get("confirm_password", "").strip()

        if not re.match(r"^[a-zA-Z0-9_]{4,30}$", username):
            flash("账号需为4-30位字母/数字/下划线", "danger")
            return redirect(url_for("register"))
        if len(display_name) < 2 or len(display_name) > 20:
            flash("姓名需为2-20个字符", "danger")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("密码至少6位", "danger")
            return redirect(url_for("register"))
        if password != confirm_password:
            flash("两次输入密码不一致", "danger")
            return redirect(url_for("register"))

        connection = get_user_db_connection()
        if not connection:
            flash("账号数据库连接失败", "danger")
            return redirect(url_for("register"))

        cursor = None
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT id FROM sys_user WHERE username = %s", (username,))
            if cursor.fetchone():
                flash("账号已存在，请更换", "danger")
                return redirect(url_for("register"))

            cursor.execute(
                """
                INSERT INTO sys_user (username, password, role_level, real_name, phone, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (username, generate_password_hash(password), 1, display_name, "", 1),
            )
            connection.commit()
            flash("注册成功，请登录", "success")
            return redirect(url_for("login"))
        except Exception as e:
            if connection:
                connection.rollback()
            flash(f"注册失败: {str(e)}", "danger")
            return redirect(url_for("register"))
        finally:
            if cursor:
                cursor.close()
            if connection and getattr(connection, "open", False):
                connection.close()

    return render_template_string(REGISTER_HTML)


@app.route("/logout")
def logout():
    session.clear()
    flash("已退出登录", "success")
    return redirect(url_for("login"))


@app.route("/uploads/task")
def serve_task_photo():
    """兼容旧链接，重定向到 Cloudinary CDN"""
    if not is_logged_in():
        return redirect(url_for("login", next=request.full_path))
    public_id = request.args.get("p", "").strip()
    if not public_id:
        abort(404)
    try:
        cdn_url = cloudinary.utils.cloudinary_url(public_id, secure=True)[0]
    except Exception:
        abort(404)
    return redirect(cdn_url)


def _json_task_images(cursor, task_id):
    return images_to_json_list(fetch_task_image_rows(cursor, task_id))


@app.route("/task/<int:task_id>")
def task_detail(task_id):
    """工单详情：展示信息及关联现场照片，支持 FormData 上传"""
    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("tasks_page"))
    cursor = None
    try:
        ensure_task_images_table(connection)
        migrate_legacy_task_photos(connection)
        cursor = connection.cursor()
        task = fetch_task_for_detail(cursor, task_id)
        if not task:
            flash("未找到该工单", "danger")
            return redirect(url_for("tasks_page"))
        images = _json_task_images(cursor, task_id)
        return render_template_string(
            TASK_DETAIL_HTML,
            task=task,
            images=images,
            max_photos=TASK_PHOTO_MAX_PER_TASK,
        )
    except Exception as e:
        flash(f"读取工单失败: {str(e)}", "danger")
        return redirect(url_for("tasks_page"))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/api/task/<int:task_id>/images", methods=["GET"])
def api_task_images_list(task_id):
    """按工单 ID 返回关联图片路径列表（JSON）"""
    connection = get_db_connection()
    if not connection:
        return jsonify({"ok": False, "message": "数据库连接失败"}), 500
    cursor = None
    try:
        ensure_task_images_table(connection)
        migrate_legacy_task_photos(connection)
        cursor = connection.cursor()
        if not fetch_task_for_detail(cursor, task_id):
            return jsonify({"ok": False, "message": "工单不存在"}), 404
        return jsonify({"ok": True, "images": _json_task_images(cursor, task_id)})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/api/task/<int:task_id>/images", methods=["POST"])
def api_task_images_upload(task_id):
    """FormData 上传现场照片，校验后落盘并写入 task_images 表"""
    connection = get_db_connection()
    if not connection:
        return jsonify({"ok": False, "message": "数据库连接失败"}), 500
    cursor = None
    try:
        ensure_task_images_table(connection)
        cursor = connection.cursor()
        if not fetch_task_for_detail(cursor, task_id):
            return jsonify({"ok": False, "message": "工单不存在"}), 404
        files = request.files.getlist("photos")
        if not files or not any(getattr(f, "filename", None) for f in files):
            return jsonify({"ok": False, "message": "请选择要上传的图片"}), 400
        inserted, msg = insert_task_images(cursor, task_id, files)
        if not inserted:
            return jsonify({"ok": False, "message": msg}), 400
        cursor.execute(
            "UPDATE maintenance_tasks SET status = '待回执', updated_at = NOW() WHERE task_id = %s AND status NOT IN ('已完成', '已取消')",
            (task_id,),
        )
        connection.commit()
        all_images = _json_task_images(cursor, task_id)
        return jsonify({"ok": True, "message": msg, "images": all_images})
    except Exception as e:
        if connection:
            connection.rollback()
        return jsonify({"ok": False, "message": str(e)}), 500
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/api/task/<int:task_id>/images/<int:image_id>", methods=["DELETE"])
def api_task_image_delete(task_id, image_id):
    connection = get_db_connection()
    if not connection:
        return jsonify({"ok": False, "message": "数据库连接失败"}), 500
    cursor = None
    try:
        ensure_task_images_table(connection)
        cursor = connection.cursor()
        if not delete_task_image_by_id(cursor, task_id, image_id):
            return jsonify({"ok": False, "message": "图片不存在"}), 404
        connection.commit()
        return jsonify({"ok": True, "images": _json_task_images(cursor, task_id)})
    except Exception as e:
        if connection:
            connection.rollback()
        return jsonify({"ok": False, "message": str(e)}), 500
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/account_management")
@require_role_level(3)
def account_management():
    user_connection = get_user_db_connection()
    if not user_connection:
        flash("账号数据库连接失败", "danger")
        return redirect(url_for("tasks_page"))
    user_cursor = None
    staff_list = []
    try:
        # 读取员工列表用于绑定下拉
        maintenance_conn = get_db_connection()
        if maintenance_conn:
            maintenance_cursor = maintenance_conn.cursor()
            maintenance_cursor.execute(
                "SELECT staff_id, full_name, position FROM staff_basic_info ORDER BY staff_id"
            )
            staff_list = maintenance_cursor.fetchall()
            maintenance_cursor.close()
            maintenance_conn.close()

        user_cursor = user_connection.cursor()
        user_cursor.execute(
            """
            SELECT u.id, u.username, u.role_level, u.real_name, u.phone, u.staff_id,
                   u.status, u.create_time, u.update_time, s.full_name AS bound_staff_name
            FROM sys_user u
            LEFT JOIN telecom_maintenance.staff_basic_info s ON u.staff_id = s.staff_id
            ORDER BY u.role_level DESC, u.id ASC
            """
        )
        users = user_cursor.fetchall()
        return render_template_string(ACCOUNT_MANAGEMENT_HTML, users=users, staff_list=staff_list)
    except Exception as e:
        flash(f"读取账号列表失败: {str(e)}", "danger")
        return redirect(url_for("tasks_page"))
    finally:
        if user_cursor:
            user_cursor.close()
        if user_connection and getattr(user_connection, "open", False):
            user_connection.close()


@app.route("/account_management/bind_staff/<int:user_id>", methods=["POST"])
@require_role_level(3)
def bind_account_staff(user_id):
    staff_raw = request.form.get("staff_id", "").strip()
    staff_id = None
    if staff_raw:
        try:
            staff_id = int(staff_raw)
        except ValueError:
            flash("员工ID格式错误", "danger")
            return redirect(url_for("account_management"))

        maintenance_conn = get_db_connection()
        if not maintenance_conn:
            flash("员工数据库连接失败", "danger")
            return redirect(url_for("account_management"))
        m_cur = None
        try:
            m_cur = maintenance_conn.cursor()
            m_cur.execute("SELECT staff_id FROM staff_basic_info WHERE staff_id = %s", (staff_id,))
            if not m_cur.fetchone():
                flash("要绑定的员工不存在", "danger")
                return redirect(url_for("account_management"))
        finally:
            if m_cur:
                m_cur.close()
            if maintenance_conn and getattr(maintenance_conn, "open", False):
                maintenance_conn.close()

    user_conn = get_user_db_connection()
    if not user_conn:
        flash("账号数据库连接失败", "danger")
        return redirect(url_for("account_management"))
    u_cur = None
    try:
        u_cur = user_conn.cursor()
        u_cur.execute("UPDATE sys_user SET staff_id = %s, update_time = NOW() WHERE id = %s", (staff_id, user_id))
        if u_cur.rowcount == 0:
            user_conn.rollback()
            flash("未找到该账号", "warning")
        else:
            user_conn.commit()
            flash("账号与员工绑定已更新", "success")
    except Exception as e:
        if user_conn:
            user_conn.rollback()
        flash(f"绑定失败: {str(e)}", "danger")
    finally:
        if u_cur:
            u_cur.close()
        if user_conn and getattr(user_conn, "open", False):
            user_conn.close()

    return redirect(url_for("account_management"))


@app.route("/account_management/update/<int:user_id>", methods=["POST"])
@require_role_level(3)
def update_account_permission(user_id):
    role_level_raw = request.form.get("role_level", "").strip()
    status_raw = request.form.get("status", "").strip()
    try:
        role_level = int(role_level_raw)
        status = int(status_raw)
    except ValueError:
        flash("参数格式错误", "danger")
        return redirect(url_for("account_management"))

    if role_level not in (1, 2, 3):
        flash("权限等级仅支持 1/2/3", "danger")
        return redirect(url_for("account_management"))
    if status not in (0, 1):
        flash("状态仅支持 0 或 1", "danger")
        return redirect(url_for("account_management"))

    current_user_id = int(session.get("user_id") or 0)
    if user_id == current_user_id and role_level < 3:
        flash("不能降低自己的管理员权限", "danger")
        return redirect(url_for("account_management"))

    connection = get_user_db_connection()
    if not connection:
        flash("账号数据库连接失败", "danger")
        return redirect(url_for("account_management"))

    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE sys_user SET role_level = %s, status = %s, update_time = NOW() WHERE id = %s",
            (role_level, status, user_id),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            flash("未找到该账号", "warning")
        else:
            connection.commit()
            flash("账号权限已更新", "success")
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f"更新账号权限失败: {str(e)}", "danger")
    finally:
        if cursor:
            cursor.close()
        if connection and getattr(connection, "open", False):
            connection.close()

    return redirect(url_for("account_management"))


# 下载导入模板
@app.route('/download_template')
def download_template():
    try:
        # 创建符合导入要求的模板
        data = {
            "full_name": ["张三", "李四"],  # 姓名(必填)
            "gender": ["男", "女"],  # 性别(必填)
            "id_card": ["110101199001011234", "110101199202025678"],  # 身份证号(必填)
            "private_phone": ["13800138000", ""],  # 私人电话
            "work_phone": ["13900139000", ""],  # 工作电话
            "emergency_contact": ["王五", "赵六"],  # 紧急联系人
            "emergency_phone": ["13700137000", "13600136000"],  # 紧急联系电话
            "education": ["本科", "大专"],  # 学历
            "entry_date": ["2020-01-15", "2021-03-20"],  # 入职日期(必填)
            "departure_date": ["", ""],  # 离职日期
            "region_id": [1, 2],  # 区域ID
            "team_id": [3, 4],  # 班组ID
            "position": ["装维工程师", "班组长"]  # 岗位(必填)
        }
        
        df = pd.DataFrame(data)
        output = BytesIO()
        
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name="员工信息模板", index=False)
        
        output.seek(0)
        return send_file(
            output,
            download_name="员工信息导入模板.xlsx",
            as_attachment=True,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    except Exception as e:
        flash(f'模板下载失败: {str(e)}', 'danger')
        return redirect(url_for('staff_page'))


@app.route('/')
def index():
    """根路径进入任务管理页（与员工页可随时切换）"""
    return redirect(url_for('tasks_page'))


@app.route('/tasks')
def tasks_page():
    """任务管理（独立页面）"""
    connection = get_db_connection()
    if not connection:
        flash('数据库连接失败', 'danger')
        return render_template_string(
            TASKS_PAGE_HTML,
            task_list=[],
            task_stats={"total": 0, "pending": 0, "in_progress": 0, "done": 0},
            filters={"status": "", "priority": "", "keyword": ""},
        )

    try:
        ensure_maintenance_tasks_table(connection)
        ensure_task_images_table(connection)
        migrate_legacy_task_photos(connection)
        cursor = connection.cursor()
        status_filter = request.args.get("status", "").strip()
        priority_filter = request.args.get("priority", "").strip()
        keyword = request.args.get("keyword", "").strip()

        where_parts = []
        params = []

        # 权限1只能看待派单及派给自己的工单；权限2和3可查看全部
        if get_current_role_level() == 1:
            current_staff_id = session.get("staff_id")
            if current_staff_id:
                where_parts.append("(t.assigned_staff_id = %s OR t.status = '待派单')")
                params.append(current_staff_id)
            else:
                where_parts.append("t.status = '待派单'")

        if status_filter in TASK_STATUSES:
            where_parts.append("t.status = %s")
            params.append(status_filter)
        else:
            status_filter = ""

        if priority_filter in TASK_PRIORITIES:
            where_parts.append("t.priority = %s")
            params.append(priority_filter)
        else:
            priority_filter = ""

        if keyword:
            where_parts.append("(t.title LIKE %s OR t.description LIKE %s)")
            like_kw = f"%{keyword}%"
            params.extend([like_kw, like_kw])

        where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        cursor.execute(
            f"""
            SELECT t.*, s.full_name AS assigned_staff_name,
                   (SELECT COUNT(*) FROM task_images i WHERE i.task_id = t.task_id) AS photo_count
            FROM maintenance_tasks t
            LEFT JOIN staff_basic_info s ON t.assigned_staff_id = s.staff_id
            {where_sql}
            ORDER BY
                CASE t.status
                    WHEN '待派单' THEN 1
                    WHEN '已派单' THEN 2
                    WHEN '处理中' THEN 3
                    WHEN '待回执' THEN 4
                    WHEN '已完成' THEN 5
                    WHEN '已取消' THEN 6
                    ELSE 7
                END,
                CASE t.priority
                    WHEN '高' THEN 1
                    WHEN '中' THEN 2
                    WHEN '低' THEN 3
                    ELSE 4
                END,
                t.task_id DESC
            """,
            params,
        )
        task_list = cursor.fetchall()
        for row in task_list:
            row["photo_count"] = int(row.get("photo_count") or 0)

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = '待派单' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = '已派单' THEN 1 ELSE 0 END) AS assigned,
                SUM(CASE WHEN status = '处理中' THEN 1 ELSE 0 END) AS in_progress,
                SUM(CASE WHEN status = '待回执' THEN 1 ELSE 0 END) AS awaiting_receipt,
                SUM(CASE WHEN status = '已完成' THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN status = '已取消' THEN 1 ELSE 0 END) AS cancelled
            FROM maintenance_tasks
            """
        )
        stats_raw = cursor.fetchone() or {}
        task_stats = {
            "total": int(stats_raw.get("total") or 0),
            "pending": int(stats_raw.get("pending") or 0),
            "assigned": int(stats_raw.get("assigned") or 0),
            "in_progress": int(stats_raw.get("in_progress") or 0),
            "awaiting_receipt": int(stats_raw.get("awaiting_receipt") or 0),
            "done": int(stats_raw.get("done") or 0),
        }

        cursor.close()
        connection.close()
        return render_template_string(
            TASKS_PAGE_HTML,
            task_list=task_list,
            task_stats=task_stats,
            filters={"status": status_filter, "priority": priority_filter, "keyword": keyword},
        )
    except Exception as e:
        flash(f'获取任务数据失败: {str(e)}', 'danger')
        return render_template_string(
            TASKS_PAGE_HTML,
            task_list=[],
            task_stats={"total": 0, "pending": 0, "in_progress": 0, "done": 0},
            filters={"status": "", "priority": "", "keyword": ""},
        )


@app.route("/task_statistics")
@require_role_level(3)
def task_statistics():
    """管理员：多维度任务统计（工作量、完成率、趋势）"""
    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("tasks_page"))
    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        filters = parse_task_stat_filters(request)
        cursor = connection.cursor()
        stat_data = build_task_statistics(cursor, filters)
        return render_template_string(TASK_STATISTICS_HTML, stat=stat_data)
    except Exception as e:
        flash(f"统计数据加载失败: {str(e)}", "danger")
        return redirect(url_for("tasks_page"))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/task_statistics/export")
@require_role_level(3)
def task_statistics_export():
    """导出当前统计视图为 Excel"""
    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("task_statistics"))
    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        filters = parse_task_stat_filters(request)
        cursor = connection.cursor()
        stat_data = build_task_statistics(cursor, filters)
        output = export_task_statistics_xlsx(stat_data)
        fname = (
            f"装维任务统计_{stat_data['view_label']}_{stat_data['period_label']}_"
            f"{stat_data['date_from_str']}_{stat_data['date_to_str']}.xlsx"
        )
        return send_file(
            output,
            download_name=fname,
            as_attachment=True,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        flash(f"导出失败: {str(e)}", "danger")
        return redirect(url_for("task_statistics", **request.args))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


def ensure_staff_columns(connection):
    """幂等地为 staff_basic_info 补充 department 和 team_name 列"""
    cur = connection.cursor()
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'department'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN department VARCHAR(50) NULL AFTER position")
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'team_name'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN team_name VARCHAR(50) NULL AFTER team_id")
    connection.commit()
    cur.close()


@app.route('/staff')
def staff_page():
    """员工管理（独立页面）"""
    connection = get_db_connection()
    if not connection:
        flash('数据库连接失败', 'danger')
        return render_template_string(STAFF_PAGE_HTML, staff_list=[])

    try:
        ensure_staff_columns(connection)
        cursor = connection.cursor()
        view_mode = request.args.get('view', '')
        if view_mode == 'all':
            cursor.execute("SELECT * FROM staff_basic_info ORDER BY staff_id")
        else:
            cursor.execute("SELECT * FROM staff_basic_info ORDER BY staff_id LIMIT 100")

        staff_list = cursor.fetchall()
        cursor.close()
        connection.close()
        return render_template_string(STAFF_PAGE_HTML, staff_list=staff_list)
    except Exception as e:
        flash(f'获取员工数据失败: {str(e)}', 'danger')
        return render_template_string(STAFF_PAGE_HTML, staff_list=[])

# 添加员工路由
@app.route('/add_staff', methods=['GET', 'POST'])
@require_role_level(3)
def add_staff():
    if request.method == 'POST':
        connection = get_db_connection()
        if not connection:
            flash('数据库连接失败', 'danger')
            return redirect(url_for('add_staff'))
        cursor = None
        try:
            ensure_staff_columns(connection)
            cursor = connection.cursor()

            # 获取表单数据
            full_name = request.form.get('full_name', '').strip()
            gender = request.form.get('gender', '').strip()
            id_card = request.form.get('id_card', '').strip()
            private_phone = request.form.get('private_phone', '').strip()
            work_phone = request.form.get('work_phone', '').strip()
            emergency_contact = request.form.get('emergency_contact', '').strip()
            emergency_phone = request.form.get('emergency_phone', '').strip()
            education = request.form.get('education', '').strip()
            entry_date = request.form.get('entry_date', '').strip()
            departure_date = request.form.get('departure_date', '').strip()
            region_id = request.form.get('region_id', '').strip()
            team_id = request.form.get('team_id', '').strip()
            position = request.form.get('position', '').strip()
            department = request.form.get('department', '').strip()
            team_name = request.form.get('team_name', '').strip()

            # 验证必填字段
            if not all([full_name, gender, id_card, entry_date, position]):
                flash('请填写所有必填字段', 'danger')
                return redirect(url_for('add_staff'))

            # 验证身份证号
            if not re.match(r'^\d{17}[\dXx]$', id_card):
                flash('身份证号格式错误（需18位，含数字或X）', 'danger')
                return redirect(url_for('add_staff'))

            # 处理日期
            try:
                entry_date = datetime.strptime(entry_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except:
                flash('入职日期格式错误（建议YYYY-MM-DD）', 'danger')
                return redirect(url_for('add_staff'))

            # 处理离职日期
            departure_date_val = None
            if departure_date:
                try:
                    departure_date_val = datetime.strptime(departure_date, "%Y-%m-%d").strftime("%Y-%m-%d")
                except:
                    flash('离职日期格式错误（建议YYYY-MM-DD）', 'danger')
                    return redirect(url_for('add_staff'))

            # 处理在职状态
            is_active = True if not departure_date_val else False

            # 处理ID字段
            region_id_val = int(region_id) if region_id else None
            team_id_val = int(team_id) if team_id else None
            education_val = education if education else None
            department_val = department if department else None
            team_name_val = team_name if team_name else None

            # 插入数据
            cursor.execute('''
            INSERT INTO staff_basic_info
            (full_name, gender, id_card, private_phone, work_phone, emergency_contact, emergency_phone,
             education, entry_date, departure_date, is_active, region_id, team_id, team_name, position, department)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                full_name, gender, id_card, private_phone, work_phone,
                emergency_contact, emergency_phone, education_val, entry_date,
                departure_date_val, is_active, region_id_val, team_id_val, team_name_val, position, department_val
            ))
            
            connection.commit()
            flash('员工添加成功', 'success')
            return redirect(url_for('staff_page'))
            
        except Exception as e:
            if connection:
                connection.rollback()
            flash(f'添加员工失败: {str(e)}', 'danger')
            return redirect(url_for('add_staff'))
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection and getattr(connection, 'open', False):
                connection.close()
    
    # 显示添加员工表单
    return render_template_string(ADD_STAFF_HTML)

# 编辑员工路由
@app.route('/edit_staff/<int:staff_id>', methods=['GET', 'POST'])
@require_role_level(3)
def edit_staff(staff_id):
    if request.method == 'POST':
        connection = get_db_connection()
        if not connection:
            flash('数据库连接失败', 'danger')
            return redirect(url_for('staff_page'))
        cursor = None
        try:
            cursor = connection.cursor()
            
            # 获取表单数据
            full_name = request.form.get('full_name', '').strip()
            gender = request.form.get('gender', '').strip()
            id_card = request.form.get('id_card', '').strip()
            private_phone = request.form.get('private_phone', '').strip()
            work_phone = request.form.get('work_phone', '').strip()
            emergency_contact = request.form.get('emergency_contact', '').strip()
            emergency_phone = request.form.get('emergency_phone', '').strip()
            education = request.form.get('education', '').strip()
            entry_date = request.form.get('entry_date', '').strip()
            departure_date = request.form.get('departure_date', '').strip()
            region_id = request.form.get('region_id', '').strip()
            team_id = request.form.get('team_id', '').strip()
            position = request.form.get('position', '').strip()
            department = request.form.get('department', '').strip()
            team_name = request.form.get('team_name', '').strip()

            # 验证必填字段
            if not all([full_name, gender, id_card, entry_date, position]):
                flash('请填写所有必填字段', 'danger')
                return redirect(url_for('edit_staff', staff_id=staff_id))

            # 验证身份证号
            if not re.match(r'^\d{17}[\dXx]$', id_card):
                flash('身份证号格式错误（需18位，含数字或X）', 'danger')
                return redirect(url_for('edit_staff', staff_id=staff_id))

            # 处理日期
            try:
                entry_date = datetime.strptime(entry_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            except:
                flash('入职日期格式错误（建议YYYY-MM-DD）', 'danger')
                return redirect(url_for('edit_staff', staff_id=staff_id))

            # 处理离职日期
            departure_date_val = None
            if departure_date:
                try:
                    departure_date_val = datetime.strptime(departure_date, "%Y-%m-%d").strftime("%Y-%m-%d")
                except:
                    flash('离职日期格式错误（建议YYYY-MM-DD）', 'danger')
                    return redirect(url_for('edit_staff', staff_id=staff_id))

            # 处理在职状态
            is_active = True if not departure_date_val else False

            # 处理ID字段
            region_id_val = int(region_id) if region_id else None
            team_id_val = int(team_id) if team_id else None
            education_val = education if education else None
            department_val = department if department else None
            team_name_val = team_name if team_name else None

            # 更新数据
            cursor.execute('''
            UPDATE staff_basic_info SET
                full_name = %s, gender = %s, id_card = %s, private_phone = %s,
                work_phone = %s, emergency_contact = %s, emergency_phone = %s,
                education = %s, entry_date = %s, departure_date = %s,
                is_active = %s, region_id = %s, team_id = %s, team_name = %s,
                position = %s, department = %s,
                updated_at = NOW()
            WHERE staff_id = %s
            ''', (
                full_name, gender, id_card, private_phone, work_phone,
                emergency_contact, emergency_phone, education_val, entry_date,
                departure_date_val, is_active, region_id_val, team_id_val, team_name_val,
                position, department_val, staff_id
            ))
            
            connection.commit()
            flash('员工信息更新成功', 'success')
            return redirect(url_for('staff_page'))
            
        except Exception as e:
            if connection:
                connection.rollback()
            flash(f'更新员工信息失败: {str(e)}', 'danger')
            return redirect(url_for('edit_staff', staff_id=staff_id))
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection and getattr(connection, 'open', False):
                connection.close()
    
    # GET请求：显示编辑表单
    connection = get_db_connection()
    if not connection:
        flash('数据库连接失败', 'danger')
        return redirect(url_for('staff_page'))
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM staff_basic_info WHERE staff_id = %s", (staff_id,))
        staff = cursor.fetchone()
        
        if not staff:
            flash('未找到该员工信息', 'danger')
            return redirect(url_for('staff_page'))
            
        return render_template_string(EDIT_STAFF_HTML, staff=staff)
        
    except Exception as e:
        flash(f'获取员工信息失败: {str(e)}', 'danger')
        return redirect(url_for('staff_page'))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, 'open', False):
            connection.close()

# 删除员工路由
@app.route('/delete_staff/<int:staff_id>', methods=['POST'])
@require_role_level(3)
def delete_staff(staff_id):
    connection = get_db_connection()
    if not connection:
        flash('数据库连接失败', 'danger')
        return redirect(url_for('staff_page'))
        
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("DELETE FROM staff_basic_info WHERE staff_id = %s", (staff_id,))
        deleted = cursor.rowcount
        connection.commit()
        
        if deleted > 0:
            flash('员工信息已成功删除', 'success')
        else:
            flash('未找到该员工信息', 'warning')
            
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f'删除员工信息失败: {str(e)}', 'danger')
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, 'open', False):
            connection.close()
        
    return redirect(url_for('staff_page'))

# 导出员工信息路由
@app.route('/export_staff')
def export_staff():
    exporter = StaffInfoExporter()
    result = exporter.export_to_xlsx()
    exporter.close()
    
    if result["status"] == "success":
        return send_file(
            result["data"],
            download_name=f"装维部门员工信息_{datetime.now().strftime('%Y%m%d')}.xlsx",
            as_attachment=True,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    else:
        flash(result["message"], 'danger' if result["status"] == "error" else 'warning')
        return redirect(url_for('staff_page'))

# 导入员工信息路由
@app.route('/import_staff', methods=['POST'])
def import_staff():
    if 'file' not in request.files:
        flash('未选择文件', 'danger')
        return redirect(url_for('staff_page'))
    
    file = request.files['file']
    if file.filename == '':
        flash('未选择文件', 'danger')
        return redirect(url_for('staff_page'))
    
    if file and (file.filename.endswith('.xlsx') or file.filename.endswith('.xls')):
        try:
            # 保存上传的文件
            file_path = f"temp_{datetime.now().timestamp()}.xlsx"
            file.save(file_path)
            
            # 导入数据
            manager = StaffInfoManager()
            result = manager.import_from_excel(file_path)
            manager.close()
            
            # 删除临时文件
            if os.path.exists(file_path):
                os.remove(file_path)
            
            # 处理导入结果
            if result["status"] == "success":
                flash(result["message"], 'success')
                if result["fail_count"] > 0:
                    for reason in result["fail_reasons"]:
                        flash(reason, 'warning')
            else:
                flash(result["message"], 'danger')
                
        except Exception as e:
            flash(f'导入过程出错: {str(e)}', 'danger')
    else:
        flash('请上传.xls或.xlsx格式的文件', 'danger')
    
    return redirect(url_for('staff_page'))


def _fetch_staff_options(connection, active_only=True):
    """派单用：按部门+班组分组的在职员工列表，返回 {group_label: [staff, ...]} 有序结构"""
    cur = connection.cursor()
    where = "WHERE is_active = 1" if active_only else ""
    cur.execute(
        f"""SELECT staff_id, full_name, position, department, team_id, team_name
            FROM staff_basic_info {where}
            ORDER BY
                COALESCE(department, 'zzz'),
                COALESCE(team_name, CONCAT('班组 ', team_id), 'zzz'),
                staff_id"""
    )
    rows = cur.fetchall()
    cur.close()
    groups = {}
    for r in rows:
        dept = r["department"] or "未分配部门"
        team = r["team_name"] or (f"班组 {r['team_id']}" if r["team_id"] else "未分配班组")
        label = f"{dept} · {team}"
        groups.setdefault(label, []).append(r)
    return groups


@app.route("/add_task", methods=["GET", "POST"])
def add_task():
    if request.method == "POST":
        connection = get_db_connection()
        if not connection:
            flash("数据库连接失败", "danger")
            return redirect(url_for("add_task"))
        cursor = None
        try:
            ensure_maintenance_tasks_table(connection)
            cursor = connection.cursor()
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            priority = request.form.get("priority", "中").strip()
            due_raw = request.form.get("due_date", "").strip()
            fault_type = request.form.get("fault_type", "").strip()
            fault_phenomenon = request.form.get("fault_phenomenon", "").strip()
            resolution_method = request.form.get("resolution_method", "").strip()
            customer_address = request.form.get("customer_address", "").strip()

            if not title:
                flash("请填写任务标题", "danger")
                return redirect(url_for("add_task"))
            if priority not in TASK_PRIORITIES:
                priority = "中"

            due_date = None
            if due_raw:
                try:
                    due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
                except ValueError:
                    flash("截止日期格式错误（请使用 YYYY-MM-DD）", "danger")
                    return redirect(url_for("add_task"))

            ensure_task_images_table(connection)
            cursor.execute(
                """
                INSERT INTO maintenance_tasks
                (title, description, fault_type, customer_address, fault_phenomenon, resolution_method, priority, status, due_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, '待派单', %s)
                """,
                (title, description or None, fault_type or None, customer_address or None,
                 fault_phenomenon or None, resolution_method or None, priority, due_date),
            )
            new_id = cursor.lastrowid
            connection.commit()
            flash("任务已添加，可在工单详情页上传现场照片", "success")
            return redirect(url_for("task_detail", task_id=new_id))
        except Exception as e:
            if connection:
                connection.rollback()
            flash(f"添加任务失败: {str(e)}", "danger")
            return redirect(url_for("add_task"))
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection and getattr(connection, "open", False):
                connection.close()

    return render_template_string(ADD_TASK_HTML, priorities=TASK_PRIORITIES,
        fault_business_types=FAULT_BUSINESS_TYPES, fault_phenomena=FAULT_PHENOMENA,
        resolution_methods=RESOLUTION_METHODS, fault_types_need_phenomenon=list(FAULT_TYPES_NEED_PHENOMENON))


@app.route("/edit_task/<int:task_id>", methods=["GET", "POST"])
def edit_task(task_id):
    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("tasks_page"))

    if request.method == "POST":
        cursor = None
        try:
            ensure_maintenance_tasks_table(connection)
            cursor = connection.cursor()
            title = request.form.get("title", "").strip()
            description = request.form.get("description", "").strip()
            priority = request.form.get("priority", "中").strip()
            status = request.form.get("status", "待派单").strip()
            due_raw = request.form.get("due_date", "").strip()
            fault_type = request.form.get("fault_type", "").strip()
            fault_phenomenon = request.form.get("fault_phenomenon", "").strip()
            resolution_method = request.form.get("resolution_method", "").strip()
            customer_address = request.form.get("customer_address", "").strip()

            if not title:
                flash("请填写任务标题", "danger")
                return redirect(url_for("edit_task", task_id=task_id))
            if priority not in TASK_PRIORITIES:
                priority = "中"
            if status not in TASK_STATUSES:
                status = "待派单"

            due_date = None
            if due_raw:
                try:
                    due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
                except ValueError:
                    flash("截止日期格式错误", "danger")
                    return redirect(url_for("edit_task", task_id=task_id))

            # 改回待派单时可清空执行人与派单时间（与派单流程一致）
            if status == "待派单":
                cursor.execute(
                    """
                    UPDATE maintenance_tasks SET
                        title = %s, description = %s, fault_type = %s, customer_address = %s,
                        fault_phenomenon = %s, resolution_method = %s,
                        priority = %s, status = %s, due_date = %s,
                        assigned_staff_id = NULL, assigned_at = NULL, updated_at = NOW()
                    WHERE task_id = %s
                    """,
                    (title, description or None, fault_type or None, customer_address or None,
                     fault_phenomenon or None, resolution_method or None, priority, status, due_date, task_id),
                )
            else:
                cursor.execute(
                    """
                    UPDATE maintenance_tasks SET
                        title = %s, description = %s, fault_type = %s, customer_address = %s,
                        fault_phenomenon = %s, resolution_method = %s,
                        priority = %s, status = %s, due_date = %s,
                        updated_at = NOW()
                    WHERE task_id = %s
                    """,
                    (title, description or None, fault_type or None, customer_address or None,
                     fault_phenomenon or None, resolution_method or None, priority, status, due_date, task_id),
                )

            if cursor.rowcount == 0:
                connection.rollback()
                flash("未找到该任务", "warning")
            else:
                connection.commit()
                flash("任务已更新", "success")
            return redirect(url_for("tasks_page"))
        except Exception as e:
            if connection:
                connection.rollback()
            flash(f"更新任务失败: {str(e)}", "danger")
            return redirect(url_for("edit_task", task_id=task_id))
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection and getattr(connection, "open", False):
                connection.close()

    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM maintenance_tasks WHERE task_id = %s", (task_id,))
        task = cursor.fetchone()
        if not task:
            flash("未找到该任务", "danger")
            return redirect(url_for("tasks_page"))
        return render_template_string(
            EDIT_TASK_HTML,
            task=task,
            priorities=TASK_PRIORITIES,
            statuses=TASK_STATUSES,
            fault_business_types=FAULT_BUSINESS_TYPES,
            fault_phenomena=FAULT_PHENOMENA,
            resolution_methods=RESOLUTION_METHODS,
            fault_types_need_phenomenon=list(FAULT_TYPES_NEED_PHENOMENON),
        )
    except Exception as e:
        flash(f"读取任务失败: {str(e)}", "danger")
        return redirect(url_for("tasks_page"))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/assign_task/<int:task_id>", methods=["GET", "POST"])
@require_role_level(2)
def assign_task(task_id):
    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("tasks_page"))

    if request.method == "POST":
        cursor = None
        try:
            ensure_maintenance_tasks_table(connection)
            cursor = connection.cursor()
            staff_raw = request.form.get("assigned_staff_id", "").strip()
            if not staff_raw:
                flash("请选择执行人", "danger")
                return redirect(url_for("assign_task", task_id=task_id))
            try:
                staff_id = int(staff_raw)
            except ValueError:
                flash("执行人无效", "danger")
                return redirect(url_for("assign_task", task_id=task_id))

            cursor.execute(
                "SELECT staff_id FROM staff_basic_info WHERE staff_id = %s AND is_active = 1",
                (staff_id,),
            )
            if not cursor.fetchone():
                flash("所选员工不存在或已离职", "danger")
                return redirect(url_for("assign_task", task_id=task_id))

            cursor.execute(
                """
                UPDATE maintenance_tasks SET
                    assigned_staff_id = %s,
                    assigned_at = NOW(),
                    status = '已派单',
                    updated_at = NOW()
                WHERE task_id = %s AND status NOT IN ('已完成', '已取消')
                """,
                (staff_id, task_id),
            )
            if cursor.rowcount == 0:
                connection.rollback()
                flash("任务不存在，或已完成/已取消，无法派单", "warning")
            else:
                connection.commit()
                flash("任务已派单", "success")
            return redirect(url_for("tasks_page"))
        except Exception as e:
            if connection:
                connection.rollback()
            flash(f"派单失败: {str(e)}", "danger")
            return redirect(url_for("assign_task", task_id=task_id))
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection and getattr(connection, "open", False):
                connection.close()

    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM maintenance_tasks WHERE task_id = %s", (task_id,))
        task = cursor.fetchone()
        if not task:
            flash("未找到该任务", "danger")
            return redirect(url_for("tasks_page"))
        if task.get("status") in ("已完成", "已取消"):
            flash("已完成或已取消的任务不能派单", "warning")
            return redirect(url_for("tasks_page"))
        cursor.close()
        cursor = None
        staff_options = _fetch_staff_options(connection, active_only=True)
        return render_template_string(ASSIGN_TASK_HTML, task=task, staff_options=staff_options)
    except Exception as e:
        flash(f"打开派单页失败: {str(e)}", "danger")
        return redirect(url_for("tasks_page"))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/claim_task/<int:task_id>", methods=["POST"])
def claim_task(task_id):
    """非管理员用户主动接取待派单的工单"""
    current_staff_id = session.get("staff_id")
    if not current_staff_id:
        flash("您的账号未绑定员工信息，无法接取工单，请联系管理员绑定", "danger")
        return redirect(url_for("tasks_page"))

    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("tasks_page"))
    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE maintenance_tasks SET
                assigned_staff_id = %s,
                assigned_at = NOW(),
                status = '处理中',
                updated_at = NOW()
            WHERE task_id = %s AND status = '待派单'
            """,
            (current_staff_id, task_id),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            flash("接取失败：工单不存在或已被他人接取", "warning")
        else:
            connection.commit()
            flash("工单接取成功，已进入处理中", "success")
        return redirect(url_for("tasks_page"))
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f"接取工单失败: {str(e)}", "danger")
        return redirect(url_for("tasks_page"))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/return_task/<int:task_id>", methods=["POST"])
def return_task(task_id):
    """普通用户退回自己执行的工单到待派单状态"""
    current_staff_id = session.get("staff_id")
    if not current_staff_id:
        flash("您的账号未绑定员工信息，无法操作", "danger")
        return redirect(url_for("tasks_page"))

    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("tasks_page"))
    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        cursor = connection.cursor()
        cursor.execute(
            """
            UPDATE maintenance_tasks SET
                assigned_staff_id = NULL,
                assigned_at = NULL,
                status = '待派单',
                updated_at = NOW()
            WHERE task_id = %s AND assigned_staff_id = %s AND status NOT IN ('已完成', '已取消')
            """,
            (task_id, current_staff_id),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            flash("退回失败：工单不存在、不属于您、或已完成/已取消", "warning")
        else:
            connection.commit()
            flash("工单已退回待派单", "success")
        return redirect(url_for("tasks_page"))
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f"退回工单失败: {str(e)}", "danger")
        return redirect(url_for("tasks_page"))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


LOGIN_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录 - 装维部门管理系统</title>
    <style>
        body { margin: 0; font-family: "Microsoft YaHei", sans-serif; background: #f5f7fb; }
        .page { min-height: 100vh; display: grid; place-items: center; padding: 24px; }
        .card { width: 100%; max-width: 420px; background: #fff; border-radius: 10px; box-shadow: 0 8px 30px rgba(0, 0, 0, 0.08); padding: 24px; }
        h1 { margin: 0 0 6px 0; font-size: 24px; color: #1f2937; }
        .sub { margin: 0 0 18px 0; color: #6b7280; font-size: 14px; }
        .form-group { margin-bottom: 14px; }
        label { display: block; margin-bottom: 6px; font-size: 13px; color: #374151; font-weight: 600; }
        input { width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 6px; }
        .btn { width: 100%; margin-top: 6px; border: none; border-radius: 6px; padding: 10px 12px; font-size: 14px; cursor: pointer; background: #2563eb; color: white; }
        .helper { margin-top: 14px; text-align: center; font-size: 13px; color: #6b7280; }
        .helper a { color: #2563eb; text-decoration: none; }
        .flash-messages { margin-bottom: 14px; }
        .alert-success { background-color: #dff0d8; color: #3c763d; border: 1px solid #d6e9c6; padding: 8px; border-radius: 4px; margin-bottom: 8px; }
        .alert-danger { background-color: #f2dede; color: #a94442; border: 1px solid #ebccd1; padding: 8px; border-radius: 4px; margin-bottom: 8px; }
    </style>
</head>
<body>
    <div class="page">
        <div class="card">
            <h1>账号登录</h1>
            <p class="sub">首次使用默认管理员账号：admin / admin123456</p>

            {% with messages = get_flashed_messages(with_categories=true) %}
              {% if messages %}
                <div class="flash-messages">
                  {% for category, message in messages %}
                    <div class="alert-{{ category }}">{{ message }}</div>
                  {% endfor %}
                </div>
              {% endif %}
            {% endwith %}

            <form method="post">
                <input type="hidden" name="next" value="{{ next_url }}">
                <div class="form-group">
                    <label for="username">账号</label>
                    <input id="username" name="username" type="text" required>
                </div>
                <div class="form-group">
                    <label for="password">密码</label>
                    <input id="password" name="password" type="password" required>
                </div>
                <button type="submit" class="btn">登录</button>
            </form>
            <p class="helper">没有账号？<a href="{{ url_for('register') }}">去注册</a></p>
        </div>
    </div>
</body>
</html>
'''


REGISTER_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>注册 - 装维部门管理系统</title>
    <style>
        body { margin: 0; font-family: "Microsoft YaHei", sans-serif; background: #f5f7fb; }
        .page { min-height: 100vh; display: grid; place-items: center; padding: 24px; }
        .card { width: 100%; max-width: 460px; background: #fff; border-radius: 10px; box-shadow: 0 8px 30px rgba(0, 0, 0, 0.08); padding: 24px; }
        h1 { margin: 0 0 6px 0; font-size: 24px; color: #1f2937; }
        .sub { margin: 0 0 18px 0; color: #6b7280; font-size: 14px; }
        .form-group { margin-bottom: 14px; }
        label { display: block; margin-bottom: 6px; font-size: 13px; color: #374151; font-weight: 600; }
        input { width: 100%; box-sizing: border-box; padding: 10px 12px; border: 1px solid #d1d5db; border-radius: 6px; }
        .btn { width: 100%; margin-top: 6px; border: none; border-radius: 6px; padding: 10px 12px; font-size: 14px; cursor: pointer; background: #16a34a; color: white; }
        .helper { margin-top: 14px; text-align: center; font-size: 13px; color: #6b7280; }
        .helper a { color: #2563eb; text-decoration: none; }
        .flash-messages { margin-bottom: 14px; }
        .alert-success { background-color: #dff0d8; color: #3c763d; border: 1px solid #d6e9c6; padding: 8px; border-radius: 4px; margin-bottom: 8px; }
        .alert-danger { background-color: #f2dede; color: #a94442; border: 1px solid #ebccd1; padding: 8px; border-radius: 4px; margin-bottom: 8px; }
    </style>
</head>
<body>
    <div class="page">
        <div class="card">
            <h1>注册账号</h1>
            <p class="sub">创建后可直接登录系统</p>

            {% with messages = get_flashed_messages(with_categories=true) %}
              {% if messages %}
                <div class="flash-messages">
                  {% for category, message in messages %}
                    <div class="alert-{{ category }}">{{ message }}</div>
                  {% endfor %}
                </div>
              {% endif %}
            {% endwith %}

            <form method="post">
                <div class="form-group">
                    <label for="username">账号（4-30位字母/数字/下划线）</label>
                    <input id="username" name="username" type="text" required>
                </div>
                <div class="form-group">
                    <label for="display_name">姓名</label>
                    <input id="display_name" name="display_name" type="text" required>
                </div>
                <div class="form-group">
                    <label for="password">密码（至少6位）</label>
                    <input id="password" name="password" type="password" required>
                </div>
                <div class="form-group">
                    <label for="confirm_password">确认密码</label>
                    <input id="confirm_password" name="confirm_password" type="password" required>
                </div>
                <button type="submit" class="btn">注册</button>
            </form>
            <p class="helper">已有账号？<a href="{{ url_for('login') }}">去登录</a></p>
        </div>
    </div>
</body>
</html>
'''


TASK_STATISTICS_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>数据统计 - 装维部门</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        header { background-color: #2c3e50; color: white; padding: 15px 0; border-radius: 5px; margin-bottom: 16px; }
        .header-content { padding: 0 20px; }
        .header-content h1 { margin: 0; font-size: 1.35rem; font-weight: 600; }
        .header-content p { margin: 6px 0 0 0; font-size: 13px; opacity: 0.9; }
        .module-nav { display: flex; gap: 4px; margin-bottom: 20px; background: white; border-radius: 5px; padding: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.06); width: fit-content; flex-wrap: wrap; max-width: 100%; }
        .module-nav a { padding: 10px 22px; text-decoration: none; color: #555; border-radius: 4px; font-weight: 500; font-size: 14px; white-space: nowrap; }
        .module-nav a.active { background: #3498db; color: white; }
        .module-nav a:not(.active):hover { background: #ecf0f1; color: #2c3e50; }
        .dim-nav { display: flex; gap: 4px; margin-top: 14px; flex-wrap: wrap; background: #f8fafc; border: 1px solid #e8edf3; border-radius: 5px; padding: 5px; width: fit-content; max-width: 100%; }
        .dim-nav a { padding: 8px 16px; text-decoration: none; color: #555; border-radius: 4px; font-size: 13px; font-weight: 500; }
        .dim-nav a.active { background: #3498db; color: white; }
        .dim-nav a:not(.active):hover { background: #ecf0f1; color: #2c3e50; }
        .top-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
        .user-bar { display: flex; align-items: center; gap: 10px; }
        .user-tag { font-size: 13px; background: rgba(255,255,255,0.16); padding: 6px 10px; border-radius: 20px; }
        .btn { display: inline-block; padding: 8px 16px; background-color: #3498db; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 14px; }
        .btn-primary { background-color: #3498db; }
        .btn-success { background-color: #2ecc71; }
        .btn-light { background: #ecf0f1; color: #2c3e50; }
        .card { background-color: white; border-radius: 5px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 12px; }
        .summary-item { background: #f8fafc; border: 1px solid #e8edf3; border-radius: 8px; padding: 14px; }
        .summary-label { margin: 0; font-size: 12px; color: #7f8c8d; }
        .summary-value { margin: 6px 0 0 0; font-size: 24px; font-weight: 700; color: #2c3e50; line-height: 1; }
        .filters { display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: 10px; align-items: end; margin-top: 10px; }
        .filters .field { display: flex; flex-direction: column; gap: 6px; }
        .filters label { font-size: 12px; color: #6b7280; font-weight: 600; }
        .filters input, .filters select { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        .page-subtitle { color: #7f8c8d; font-size: 14px; margin: 0 0 12px 0; }
        .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
        .chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 12px; }
        .chart-box { background: #f8fafc; border: 1px solid #e8edf3; border-radius: 8px; padding: 14px; }
        .chart-box h3 { margin: 0 0 10px 0; font-size: 14px; color: #2c3e50; font-weight: 600; }
        .chart-canvas-wrap { position: relative; height: 300px; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #ddd; vertical-align: middle; }
        th { background-color: #ecf0f1; }
        .flash-messages { margin-bottom: 20px; padding: 10px; border-radius: 4px; }
        .alert-success { background-color: #dff0d8; color: #3c763d; border: 1px solid #d6e9c6; }
        .alert-danger { background-color: #f2dede; color: #a94442; border: 1px solid #ebccd1; }
        .alert-warning { background-color: #fcf8e3; color: #8a6d3b; border: 1px solid #faebcc; }
        @media (max-width: 980px) {
            .filters { grid-template-columns: 1fr; }
            .chart-row { grid-template-columns: 1fr; }
            .summary-grid { grid-template-columns: repeat(2, minmax(140px, 1fr)); }
        }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .card { padding: 14px; }
            .module-nav { width: 100%; }
            .module-nav a { padding: 8px 14px; font-size: 13px; }
            .summary-grid { grid-template-columns: repeat(2, 1fr); }
            table { font-size: 13px; }
            th, td { padding: 8px 8px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-content top-row">
                <div>
                    <h1>装维部门管理系统</h1>
                    <p>当前：数据统计 · {{ stat.view_label }} · {{ stat.period_label }}（{{ stat.date_from_str }} 至 {{ stat.date_to_str }}）</p>
                </div>
                <div class="user-bar">
                    <span class="user-tag">登录账号：{{ current_user.display_name or current_user.username }}</span>
                    <a href="{{ url_for('logout') }}" class="btn btn-light">退出</a>
                </div>
            </div>
        </header>

        <nav class="module-nav" aria-label="模块切换">
            <a href="{{ url_for('tasks_page') }}">任务管理</a>
            <a href="{{ url_for('staff_page') }}">员工管理</a>
            <a href="{{ url_for('task_statistics') }}" class="active">数据统计</a>
            <a href="{{ url_for('account_management') }}">账号管理</a>
        </nav>

        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="flash-messages">
              {% for category, message in messages %}
                <div class="alert-{{ category }}">{{ message }}</div>
              {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        <div class="card">
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;">
                <h2 style="margin:0;">筛选与导出</h2>
                <a class="btn btn-success" href="{{ url_for('task_statistics_export', period=stat.period, view=stat.view, date_from=stat.date_from_str, date_to=stat.date_to_str) }}">导出 Excel</a>
            </div>
            <p class="page-subtitle">完成率 = 已完成 ÷（工单量 − 已取消）；按工单创建时间统计。</p>
            <form method="get" class="filters" action="{{ url_for('task_statistics') }}">
                <input type="hidden" name="view" value="{{ stat.view }}">
                <div class="field">
                    <label for="period">时间粒度</label>
                    <select id="period" name="period">
                        <option value="day" {% if stat.period == 'day' %}selected{% endif %}>日报</option>
                        <option value="week" {% if stat.period == 'week' %}selected{% endif %}>周报</option>
                        <option value="month" {% if stat.period == 'month' %}selected{% endif %}>月报</option>
                    </select>
                </div>
                <div class="field">
                    <label for="date_from">开始日期</label>
                    <input type="date" id="date_from" name="date_from" value="{{ stat.date_from_str }}">
                </div>
                <div class="field">
                    <label for="date_to">结束日期</label>
                    <input type="date" id="date_to" name="date_to" value="{{ stat.date_to_str }}">
                </div>
                <button type="submit" class="btn btn-primary">筛选</button>
            </form>
            <nav class="dim-nav" aria-label="分析维度">
                <a class="{% if stat.view == 'time' %}active{% endif %}" href="{{ url_for('task_statistics', period=stat.period, view='time', date_from=stat.date_from_str, date_to=stat.date_to_str) }}">时间趋势</a>
                <a class="{% if stat.view == 'staff' %}active{% endif %}" href="{{ url_for('task_statistics', period=stat.period, view='staff', date_from=stat.date_from_str, date_to=stat.date_to_str) }}">人员对比</a>
                <a class="{% if stat.view == 'team' %}active{% endif %}" href="{{ url_for('task_statistics', period=stat.period, view='team', date_from=stat.date_from_str, date_to=stat.date_to_str) }}">班组汇总</a>
                <a class="{% if stat.view == 'region' %}active{% endif %}" href="{{ url_for('task_statistics', period=stat.period, view='region', date_from=stat.date_from_str, date_to=stat.date_to_str) }}">区域密度</a>
            </nav>
        </div>

        <div class="card">
            <h2 style="margin:0 0 12px 0;">区间汇总</h2>
            <div class="summary-grid">
                <div class="summary-item">
                    <p class="summary-label">工单总量</p>
                    <p class="summary-value">{{ stat.summary.total }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">已完成</p>
                    <p class="summary-value">{{ stat.summary.done }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">有效工单</p>
                    <p class="summary-value">{{ stat.summary.effective }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">完成率</p>
                    <p class="summary-value">{% if stat.summary.completion_pct is not none %}{{ stat.summary.completion_pct }}%{% else %}—{% endif %}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">处理中</p>
                    <p class="summary-value">{{ stat.summary.in_progress }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">已派单</p>
                    <p class="summary-value">{{ stat.summary.assigned }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">待派单</p>
                    <p class="summary-value">{{ stat.summary.pending }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">待回执</p>
                    <p class="summary-value">{{ stat.summary.awaiting_receipt }}</p>
                </div>
            </div>
        </div>

        {% if stat.rows %}
        <div class="card">
            <h2 style="margin-top:0;">图表分析</h2>
            <div class="chart-row">
                <div class="chart-box">
                    <h3>工单量{% if stat.view == 'time' %}趋势{% else %}对比{% endif %}</h3>
                    <div class="chart-canvas-wrap"><canvas id="chartVolume"></canvas></div>
                </div>
                <div class="chart-box">
                    <h3>完成率{% if stat.view == 'time' %}趋势{% else %}对比{% endif %}（%）</h3>
                    <div class="chart-canvas-wrap"><canvas id="chartRate"></canvas></div>
                </div>
            </div>
        </div>

        <div class="card">
            <h2 style="margin-top:0;">统计明细表</h2>
            <table>
                <tr>
                    {% for col in stat.table_columns %}
                    <th>{{ col.title }}</th>
                    {% endfor %}
                </tr>
                {% for row in stat.rows %}
                <tr>
                    {% for col in stat.table_columns %}
                    <td>
                        {% if col.key == 'completion_pct' %}
                            {% if row.completion_pct is not none %}{{ row.completion_pct }}%{% else %}—{% endif %}
                        {% elif col.key == 'density_pct' %}
                            {% if row.density_pct is not none %}{{ row.density_pct }}%{% else %}—{% endif %}
                        {% elif col.key == 'team_id' or col.key == 'region_id' %}
                            {{ row[col.key] if row[col.key] is not none and row[col.key] != -1 else '—' }}
                        {% else %}
                            {{ row[col.key] if row[col.key] is not none else '—' }}
                        {% endif %}
                    </td>
                    {% endfor %}
                </tr>
                {% endfor %}
            </table>
        </div>
        {% else %}
        <div class="card">
            <p style="color:#9ca3af;text-align:center;padding:24px 0;">所选日期与维度下暂无工单数据，请调整筛选后重试。</p>
        </div>
        {% endif %}
    </div>
    {% if stat.rows %}
    <script>
    (function() {
        const isTimeView = {{ 'true' if stat.view == 'time' else 'false' }};
        const labels = {{ stat.chart.labels | tojson }};
        const totals = {{ stat.chart.totals | tojson }};
        const doneVals = {{ stat.chart.done | tojson }};
        const rates = {{ stat.chart.rates | tojson }};
        const barColor = 'rgba(52, 152, 219, 0.8)';
        const doneColor = 'rgba(46, 204, 113, 0.8)';
        const rateColor = 'rgba(243, 156, 18, 0.9)';

        const volOpts = {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: isTimeView ? 'x' : 'y',
            plugins: { legend: { position: 'top' } },
            scales: { x: { beginAtZero: true }, y: { beginAtZero: true } }
        };
        new Chart(document.getElementById('chartVolume'), {
            type: 'bar',
            data: {
                labels: labels,
                datasets: [
                    { label: '工单量', data: totals, backgroundColor: barColor },
                    { label: '已完成', data: doneVals, backgroundColor: doneColor }
                ]
            },
            options: volOpts
        });
        new Chart(document.getElementById('chartRate'), {
            type: isTimeView ? 'line' : 'bar',
            data: {
                labels: labels,
                datasets: [{ label: '完成率(%)', data: rates, borderColor: rateColor, backgroundColor: rateColor, fill: false, tension: 0.25 }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                indexAxis: isTimeView ? 'x' : 'y',
                plugins: { legend: { display: false } },
                scales: { y: { beginAtZero: true, max: 100 } }
            }
        });
    })();
    </script>
    {% endif %}
</body>
</html>
'''

# 任务管理页（与员工页通过顶部导航切换）
TASKS_PAGE_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>任务管理 - 装维部门</title>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        header { background-color: #2c3e50; color: white; padding: 15px 0; border-radius: 5px; margin-bottom: 16px; }
        .header-content { padding: 0 20px; }
        .header-content h1 { margin: 0; font-size: 1.35rem; font-weight: 600; }
        .header-content p { margin: 6px 0 0 0; font-size: 13px; opacity: 0.9; }
        .module-nav { display: flex; gap: 4px; margin-bottom: 20px; background: white; border-radius: 5px; padding: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.06); width: fit-content; }
        .module-nav a { padding: 10px 22px; text-decoration: none; color: #555; border-radius: 4px; font-weight: 500; font-size: 14px; }
        .module-nav a.active { background: #3498db; color: white; }
        .module-nav a:not(.active):hover { background: #ecf0f1; color: #2c3e50; }
        .top-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
        .user-bar { display: flex; align-items: center; gap: 10px; }
        .user-tag { font-size: 13px; background: rgba(255,255,255,0.16); padding: 6px 10px; border-radius: 20px; }
        .btn { display: inline-block; padding: 8px 16px; background-color: #3498db; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 14px; }
        .btn-primary { background-color: #3498db; }
        .btn-success { background-color: #2ecc71; }
        .btn-warning { background-color: #f39c12; }
        .btn-danger { background-color: #e74c3c; }
        .btn-light { background: #ecf0f1; color: #2c3e50; }
        .card { background-color: white; border-radius: 5px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .summary-grid { display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 12px; margin-bottom: 12px; }
        .summary-item { background: #f8fafc; border: 1px solid #e8edf3; border-radius: 8px; padding: 14px; }
        .summary-label { margin: 0; font-size: 12px; color: #7f8c8d; }
        .summary-value { margin: 6px 0 0 0; font-size: 24px; font-weight: 700; color: #2c3e50; line-height: 1; }
        .filters { display: grid; grid-template-columns: 1.4fr 1fr 1fr auto auto; gap: 10px; align-items: end; margin-top: 10px; }
        .filters .field { display: flex; flex-direction: column; gap: 6px; }
        .filters label { font-size: 12px; color: #6b7280; font-weight: 600; }
        .filters input, .filters select { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #ddd; vertical-align: middle; }
        th { background-color: #ecf0f1; }
        .task-title { display: flex; flex-direction: column; gap: 4px; }
        .task-title strong { color: #2c3e50; }
        .task-title small { color: #7f8c8d; }
        .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; line-height: 18px; }
        .status-待派单 { color: #9a6700; background: #fff4d6; }
        .status-已派单 { color: #0550ae; background: #ddf4ff; }
        .status-处理中 { color: #1971c2; background: #d0ebff; }
        .status-待回执 { color: #862e9c; background: #f3d9fa; }
        .status-已完成 { color: #1a7f37; background: #dafbe1; }
        .status-已取消 { color: #cf222e; background: #ffebe9; }
        .priority-高 { color: #cf222e; background: #ffebe9; }
        .priority-中 { color: #9a6700; background: #fff4d6; }
        .priority-低 { color: #1a7f37; background: #dafbe1; }
        .action-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
        .flash-messages { margin-bottom: 20px; padding: 10px; border-radius: 4px; }
        .alert-success { background-color: #dff0d8; color: #3c763d; border: 1px solid #d6e9c6; }
        .alert-danger { background-color: #f2dede; color: #a94442; border: 1px solid #ebccd1; }
        .alert-warning { background-color: #fcf8e3; color: #8a6d3b; border: 1px solid #faebcc; }
        .admin-dashboard { border-left: 4px solid #3498db; }
        .admin-dashboard h2 { margin-top: 0; color: #1f2937; }
        .admin-metric-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin: 12px 0 16px 0; }
        .thumb-link { font-size: 13px; }
        /* 手机端卡片布局 */
        .task-cards { display: none; }
        .task-card { background: white; border-radius: 8px; padding: 14px; margin-bottom: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); border-left: 4px solid #3498db; }
        .task-card-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 8px; }
        .task-card-title { font-weight: 600; font-size: 15px; color: #1f2937; flex: 1; }
        .task-card-meta { display: flex; flex-wrap: wrap; gap: 6px; font-size: 13px; color: #6b7280; margin-bottom: 10px; }
        .task-card-meta span { display: flex; align-items: center; gap: 3px; }
        .task-card-actions { display: flex; gap: 8px; flex-wrap: wrap; }
        .task-card-actions .btn { padding: 6px 14px; font-size: 13px; }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .module-nav { width: 100%; overflow-x: auto; }
            .module-nav a { padding: 8px 14px; font-size: 13px; white-space: nowrap; }
            .summary-grid { grid-template-columns: repeat(3, 1fr); gap: 8px; }
            .summary-item { padding: 10px; }
            .summary-value { font-size: 20px; }
            .filters { grid-template-columns: 1fr 1fr; gap: 8px; }
            .filters .field:first-child { grid-column: 1 / -1; }
            table.task-table { display: none; }
            .task-cards { display: block; }
            .admin-dashboard { flex-direction: column; }
            .user-tag { display: none; }
            header h1 { font-size: 1.1rem; }
        }
        @media (max-width: 400px) {
            .summary-grid { grid-template-columns: repeat(2, 1fr); }
            .filters { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-content top-row">
                <div>
                    <h1>装维部门管理系统</h1>
                    <p>当前：任务管理</p>
                </div>
                <div class="user-bar">
                    <span class="user-tag">登录账号：{{ current_user.display_name or current_user.username }}</span>
                    <a href="{{ url_for('logout') }}" class="btn btn-light">退出</a>
                </div>
            </div>
        </header>

        <nav class="module-nav" aria-label="模块切换">
            <a href="{{ url_for('tasks_page') }}" class="active">任务管理</a>
            <a href="{{ url_for('staff_page') }}">员工管理</a>
            {% if current_user and current_user.role_level >= 3 %}
            <a href="{{ url_for('task_statistics') }}">数据统计</a>
            <a href="{{ url_for('account_management') }}">账号管理</a>
            {% endif %}
        </nav>

        {% if current_user and current_user.role_level >= 3 %}
        <div class="card admin-dashboard" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
            <div>
                <h2 style="margin:0 0 6px 0;">数据统计中心</h2>
                <p style="color:#6b7280;font-size:14px;margin:0;">工作量、完成率、任务趋势；支持日报/周报/月报，按人员、班组、区域多维分析并导出 Excel。</p>
            </div>
            <a href="{{ url_for('task_statistics') }}" class="btn btn-primary">进入统计</a>
        </div>
        {% endif %}

        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="flash-messages">
              {% for category, message in messages %}
                <div class="alert-{{ category }}">{{ message }}</div>
              {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                <h2 style="margin:0;">装维任务列表</h2>
                <a href="{{ url_for('add_task') }}" class="btn btn-success">添加任务</a>
            </div>

            <div class="summary-grid">
                <div class="summary-item">
                    <p class="summary-label">任务总数</p>
                    <p class="summary-value">{{ task_stats.total }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">待派单</p>
                    <p class="summary-value">{{ task_stats.pending }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">已派单</p>
                    <p class="summary-value">{{ task_stats.assigned }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">处理中</p>
                    <p class="summary-value">{{ task_stats.in_progress }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">待回执</p>
                    <p class="summary-value">{{ task_stats.awaiting_receipt }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">已完成</p>
                    <p class="summary-value">{{ task_stats.done }}</p>
                </div>
            </div>

            <form method="get" class="filters">
                <div class="field">
                    <label for="keyword">关键字</label>
                    <input id="keyword" name="keyword" type="text" value="{{ filters.keyword }}" placeholder="任务标题/任务说明">
                </div>
                <div class="field">
                    <label for="status">状态</label>
                    <select id="status" name="status">
                        <option value="">全部状态</option>
                        <option value="待派单" {% if filters.status == '待派单' %}selected{% endif %}>待派单</option>
                        <option value="已派单" {% if filters.status == '已派单' %}selected{% endif %}>已派单</option>
                        <option value="处理中" {% if filters.status == '处理中' %}selected{% endif %}>处理中</option>
                        <option value="待回执" {% if filters.status == '待回执' %}selected{% endif %}>待回执</option>
                        <option value="已完成" {% if filters.status == '已完成' %}selected{% endif %}>已完成</option>
                        <option value="已取消" {% if filters.status == '已取消' %}selected{% endif %}>已取消</option>
                    </select>
                </div>
                <div class="field">
                    <label for="priority">优先级</label>
                    <select id="priority" name="priority">
                        <option value="">全部优先级</option>
                        <option value="高" {% if filters.priority == '高' %}selected{% endif %}>高</option>
                        <option value="中" {% if filters.priority == '中' %}selected{% endif %}>中</option>
                        <option value="低" {% if filters.priority == '低' %}selected{% endif %}>低</option>
                    </select>
                </div>
                <button type="submit" class="btn btn-primary">筛选</button>
                <a href="{{ url_for('tasks_page') }}" class="btn btn-light">重置</a>
            </form>

            <table class="task-table">
                    <th>优先级</th>
                    <th>截止日期</th>
                    <th>状态</th>
                    <th>执行人</th>
                    <th>派单时间</th>
                    <th>现场照片</th>
                    <th>操作</th>
                </tr>
                {% for t in task_list %}
                <tr>
                    <td>{{ t.task_id }}</td>
                    <td>
                        <div class="task-title">
                            <strong>{{ t.title }}</strong>
                            <small>{{ t.fault_type or '' }}{% if t.fault_type and t.fault_phenomenon %} · {{ t.fault_phenomenon }}{% endif %}{% if not t.fault_type %}{{ (t.description or '')[:36] }}{% if t.description and t.description|length > 36 %}...{% endif %}{% endif %}</small>
                        </div>
                    </td>
                    <td><span class="pill priority-{{ t.priority }}">{{ t.priority }}</span></td>
                    <td>{{ t.due_date or '—' }}</td>
                    <td><span class="pill status-{{ t.status }}">{{ t.status }}</span></td>
                    <td>{{ t.assigned_staff_name or '—' }}</td>
                    <td>{{ t.assigned_at or '—' }}</td>
                    <td>
                        <a class="thumb-link" href="{{ url_for('task_detail', task_id=t.task_id) }}">
                            {% if t.photo_count %}查看（{{ t.photo_count }}）{% else %}上传/查看{% endif %}
                        </a>
                    </td>
                    <td class="action-buttons">
                        <a href="{{ url_for('task_detail', task_id=t.task_id) }}" class="btn btn-light">详情</a>
                        {% if current_user and current_user.role_level >= 2 %}
                        <a href="{{ url_for('edit_task', task_id=t.task_id) }}" class="btn btn-warning">编辑</a>
                        {% endif %}
                        {% if current_user and current_user.role_level >= 2 and t.status not in ['已完成', '已取消'] %}
                        <a href="{{ url_for('assign_task', task_id=t.task_id) }}" class="btn btn-primary">派单</a>
                        {% endif %}
                        {% if t.status == '待派单' and current_user and current_user.role_level < 3 %}
                        <form method="post" action="{{ url_for('claim_task', task_id=t.task_id) }}" style="display:inline;">
                            <button type="submit" class="btn btn-success" onclick="return confirm('确认接取该工单？')">接取</button>
                        </form>
                        {% endif %}
                        {% if current_user and current_user.role_level < 3 and t.assigned_staff_id == current_user.staff_id and t.status not in ['已完成', '已取消', '待派单'] %}
                        <form method="post" action="{{ url_for('return_task', task_id=t.task_id) }}" style="display:inline;">
                            <button type="submit" class="btn btn-danger" onclick="return confirm('确认退回该工单？退回后将回到待派单状态。')">退回</button>
                        </form>
                        {% endif %}
                    </td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="9" style="text-align: center; padding: 20px;">暂无任务，点击「添加任务」创建</td>
                </tr>
                {% endfor %}
            </table>

            <!-- 手机端卡片列表 -->
            <div class="task-cards">
                {% for t in task_list %}
                <div class="task-card">
                    <div class="task-card-header">
                        <div class="task-card-title">{{ t.title }}</div>
                        <span class="pill status-{{ t.status }}">{{ t.status }}</span>
                    </div>
                    <div class="task-card-meta">
                        <span><span class="pill priority-{{ t.priority }}">{{ t.priority }}</span></span>
                        {% if t.fault_type %}<span>{{ t.fault_type }}{% if t.fault_phenomenon %} · {{ t.fault_phenomenon }}{% endif %}</span>{% endif %}
                        {% if t.customer_address %}<span>📍 {{ t.customer_address }}</span>{% endif %}
                        {% if t.due_date %}<span>截止 {{ t.due_date }}</span>{% endif %}
                        {% if t.assigned_staff_name %}<span>执行人：{{ t.assigned_staff_name }}</span>{% endif %}
                        {% if t.photo_count %}<span>📷 {{ t.photo_count }}张</span>{% endif %}
                    </div>
                    <div class="task-card-actions">
                        <a href="{{ url_for('task_detail', task_id=t.task_id) }}" class="btn btn-light">详情</a>
                        {% if current_user and current_user.role_level >= 2 %}
                        <a href="{{ url_for('edit_task', task_id=t.task_id) }}" class="btn btn-warning">编辑</a>
                        {% endif %}
                        {% if current_user and current_user.role_level >= 2 and t.status not in ['已完成', '已取消'] %}
                        <a href="{{ url_for('assign_task', task_id=t.task_id) }}" class="btn btn-primary">派单</a>
                        {% endif %}
                        {% if t.status == '待派单' and current_user and current_user.role_level < 3 %}
                        <form method="post" action="{{ url_for('claim_task', task_id=t.task_id) }}" style="display:inline;">
                            <button type="submit" class="btn btn-success" onclick="return confirm('确认接取该工单？')">接取</button>
                        </form>
                        {% endif %}
                        {% if current_user and current_user.role_level < 3 and t.assigned_staff_id == current_user.staff_id and t.status not in ['已完成', '已取消', '待派单'] %}
                        <form method="post" action="{{ url_for('return_task', task_id=t.task_id) }}" style="display:inline;">
                            <button type="submit" class="btn btn-danger" onclick="return confirm('确认退回该工单？')">退回</button>
                        </form>
                        {% endif %}
                    </div>
                </div>
                {% else %}
                <p style="text-align:center;color:#9ca3af;padding:24px 0;">暂无任务</p>
                {% endfor %}
            </div>
        </div>
    </div>
</body>
</html>
'''

# 员工管理页
STAFF_PAGE_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>员工管理 - 装维部门</title>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        header { background-color: #2c3e50; color: white; padding: 15px 0; border-radius: 5px; margin-bottom: 16px; }
        .header-content { padding: 0 20px; }
        .header-content h1 { margin: 0; font-size: 1.35rem; font-weight: 600; }
        .header-content p { margin: 6px 0 0 0; font-size: 13px; opacity: 0.9; }
        .module-nav { display: flex; gap: 4px; margin-bottom: 20px; background: white; border-radius: 5px; padding: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.06); width: fit-content; }
        .module-nav a { padding: 10px 22px; text-decoration: none; color: #555; border-radius: 4px; font-weight: 500; font-size: 14px; }
        .module-nav a.active { background: #3498db; color: white; }
        .module-nav a:not(.active):hover { background: #ecf0f1; color: #2c3e50; }
        .top-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
        .user-bar { display: flex; align-items: center; gap: 10px; }
        .user-tag { font-size: 13px; background: rgba(255,255,255,0.16); padding: 6px 10px; border-radius: 20px; }
        .btn { display: inline-block; padding: 8px 16px; background-color: #3498db; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 14px; }
        .btn-primary { background-color: #3498db; }
        .btn-success { background-color: #2ecc71; }
        .btn-warning { background-color: #f39c12; }
        .btn-danger { background-color: #e74c3c; }
        .card { background-color: white; border-radius: 5px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background-color: #ecf0f1; }
        .action-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
        .import-form { margin-top: 15px; padding: 15px; border: 1px dashed #bdc3c7; border-radius: 4px; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="file"] { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        .flash-messages { margin-bottom: 20px; padding: 10px; border-radius: 4px; }
        .alert-success { background-color: #dff0d8; color: #3c763d; border: 1px solid #d6e9c6; }
        .alert-danger { background-color: #f2dede; color: #a94442; border: 1px solid #ebccd1; }
        .alert-warning { background-color: #fcf8e3; color: #8a6d3b; border: 1px solid #faebcc; }
        .delete-form { display: inline; }
        .staff-cards { display: none; }
        .staff-card { background: white; border-radius: 8px; padding: 14px; margin-bottom: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
        .staff-card-name { font-weight: 600; font-size: 15px; color: #1f2937; margin-bottom: 6px; }
        .staff-card-meta { font-size: 13px; color: #6b7280; margin-bottom: 10px; line-height: 1.8; }
        .staff-card-actions { display: flex; gap: 8px; flex-wrap: wrap; }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .module-nav { width: 100%; overflow-x: auto; }
            .module-nav a { padding: 8px 14px; font-size: 13px; white-space: nowrap; }
            .card { padding: 14px; }
            table.staff-table { display: none; }
            .staff-cards { display: block; }
            .user-tag { display: none; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-content top-row">
                <div>
                    <h1>装维部门管理系统</h1>
                    <p>当前：员工管理</p>
                </div>
                <div class="user-bar">
                    <span class="user-tag">登录账号：{{ current_user.display_name or current_user.username }}</span>
                    <a href="{{ url_for('logout') }}" class="btn btn-danger">退出</a>
                </div>
            </div>
        </header>

        <nav class="module-nav" aria-label="模块切换">
            <a href="{{ url_for('tasks_page') }}">任务管理</a>
            <a href="{{ url_for('staff_page') }}" class="active">员工管理</a>
            {% if current_user and current_user.role_level >= 3 %}
            <a href="{{ url_for('task_statistics') }}">数据统计</a>
            <a href="{{ url_for('account_management') }}">账号管理</a>
            {% endif %}
        </nav>

        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="flash-messages">
              {% for category, message in messages %}
                <div class="alert-{{ category }}">{{ message }}</div>
              {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                <h2 style="margin:0;">装维人员信息列表</h2>
                <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                    {% if current_user and current_user.role_level >= 3 %}
                    <a href="{{ url_for('add_staff') }}" class="btn btn-success">添加员工</a>
                    {% endif %}
                    <a href="{{ url_for('export_staff') }}" class="btn btn-primary">导出员工信息</a>
                    <a href="{{ url_for('staff_page', view='all') }}" class="btn btn-primary">查看全部</a>
                </div>
            </div>
            <table class="staff-table">
                <tr>
                    <th>ID</th>
                    <th>姓名</th>
                    <th>岗位</th>
                    <th>所属部门</th>
                    <th>所属班组</th>
                    <th>入职日期</th>
                    <th>在职状态</th>
                    <th>操作</th>
                </tr>
                {% for staff in staff_list %}
                <tr>
                    <td>{{ staff.staff_id }}</td>
                    <td>{{ staff.full_name }}</td>
                    <td>{{ staff.position }}</td>
                    <td>{{ staff.department or '—' }}</td>
                    <td>{{ staff.team_name or (('班组 ' ~ staff.team_id) if staff.team_id else '—') }}</td>
                    <td>{{ staff.entry_date }}</td>
                    <td>{{ '在职' if staff.is_active else '离职' }}</td>
                    <td class="action-buttons">
                        <a href="#" class="btn btn-primary">查看</a>
                        {% if current_user and current_user.role_level >= 3 %}
                        <a href="{{ url_for('edit_staff', staff_id=staff.staff_id) }}" class="btn btn-warning">编辑</a>
                        <form class="delete-form" action="{{ url_for('delete_staff', staff_id=staff.staff_id) }}" method="post" onsubmit="return confirm('确定要删除该员工信息吗？');">
                            <button type="submit" class="btn btn-danger">删除</button>
                        </form>
                        {% endif %}
                    </td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="8" style="text-align: center; padding: 20px;">暂无员工信息</td>
                </tr>
                {% endfor %}
            </table>

            <!-- 手机端员工卡片列表 -->
            <div class="staff-cards">
                {% for staff in staff_list %}
                <div class="staff-card">
                    <div class="staff-card-name">{{ staff.full_name }} · {{ staff.position }}</div>
                    <div class="staff-card-meta">
                        {% if staff.department %}部门：{{ staff.department }}<br>{% endif %}
                        班组：{{ staff.team_name or (('班组 ' ~ staff.team_id) if staff.team_id else '未分配') }}<br>
                        入职：{{ staff.entry_date }} · {{ '在职' if staff.is_active else '离职' }}
                    </div>
                    <div class="staff-card-actions">
                        {% if current_user and current_user.role_level >= 3 %}
                        <a href="{{ url_for('edit_staff', staff_id=staff.staff_id) }}" class="btn btn-warning">编辑</a>
                        <form class="delete-form" action="{{ url_for('delete_staff', staff_id=staff.staff_id) }}" method="post" onsubmit="return confirm('确定要删除该员工信息吗？');">
                            <button type="submit" class="btn btn-danger">删除</button>
                        </form>
                        {% endif %}
                    </div>
                </div>
                {% else %}
                <p style="text-align:center;color:#9ca3af;padding:24px 0;">暂无员工信息</p>
                {% endfor %}
            </div>
        </div>

        <div class="card" id="staff-import">
            <h2>员工信息导入</h2>
            <div class="import-form">
                <form action="{{ url_for('import_staff') }}" method="post" enctype="multipart/form-data">
                    <div class="form-group">
                        <label for="file">选择Excel文件（.xls或.xlsx）</label>
                        <input type="file" id="file" name="file" accept=".xls,.xlsx" required>
                    </div>
                    <div style="display: flex; gap: 10px; flex-wrap: wrap; align-items: center;">
                        <button type="submit" class="btn btn-success">导入员工信息</button>
                        <a href="{{ url_for('download_template') }}" class="btn btn-warning">下载导入模板</a>
                    </div>
                </form>
            </div>
        </div>
    </div>
</body>
</html>
'''

ACCOUNT_MANAGEMENT_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>账号管理 - 装维部门</title>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        header { background-color: #2c3e50; color: white; padding: 15px 0; border-radius: 5px; margin-bottom: 16px; }
        .header-content { padding: 0 20px; }
        .header-content h1 { margin: 0; font-size: 1.35rem; font-weight: 600; }
        .header-content p { margin: 6px 0 0 0; font-size: 13px; opacity: 0.9; }
        .module-nav { display: flex; gap: 4px; margin-bottom: 20px; background: white; border-radius: 5px; padding: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.06); width: fit-content; }
        .module-nav a { padding: 10px 22px; text-decoration: none; color: #555; border-radius: 4px; font-weight: 500; font-size: 14px; }
        .module-nav a.active { background: #3498db; color: white; }
        .module-nav a:not(.active):hover { background: #ecf0f1; color: #2c3e50; }
        .btn { display: inline-block; padding: 8px 12px; background-color: #3498db; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 13px; }
        .btn-danger { background: #e74c3c; }
        .card { background: white; border-radius: 5px; padding: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background: #ecf0f1; }
        select { padding: 6px; border: 1px solid #ddd; border-radius: 4px; }
        .flash-messages { margin-bottom: 20px; padding: 10px; border-radius: 4px; }
        .alert-success { background-color: #dff0d8; color: #3c763d; border: 1px solid #d6e9c6; }
        .alert-danger { background-color: #f2dede; color: #a94442; border: 1px solid #ebccd1; }
        .alert-warning { background-color: #fcf8e3; color: #8a6d3b; border: 1px solid #faebcc; }
        .acct-cards { display: none; }
        .acct-card { background: white; border-radius: 8px; padding: 14px; margin-bottom: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
        .acct-card-name { font-weight: 600; font-size: 15px; color: #1f2937; margin-bottom: 6px; }
        .acct-card-meta { font-size: 13px; color: #6b7280; margin-bottom: 10px; line-height: 1.8; }
        .acct-card-forms { display: flex; flex-direction: column; gap: 8px; }
        .acct-card-forms form { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
        .acct-card-forms select { padding: 6px; border: 1px solid #ddd; border-radius: 4px; font-size: 14px; }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .module-nav { width: 100%; overflow-x: auto; }
            .module-nav a { padding: 8px 14px; font-size: 13px; white-space: nowrap; }
            .card { padding: 14px; }
            table.acct-table { display: none; }
            .acct-cards { display: block; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-content">
                <h1>装维部门管理系统</h1>
                <p>当前：账号管理（权限等级3及以上）</p>
            </div>
        </header>
        <nav class="module-nav" aria-label="模块切换">
            <a href="{{ url_for('tasks_page') }}">任务管理</a>
            <a href="{{ url_for('staff_page') }}">员工管理</a>
            <a href="{{ url_for('task_statistics') }}">数据统计</a>
            <a href="{{ url_for('account_management') }}" class="active">账号管理</a>
        </nav>

        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="flash-messages">
              {% for category, message in messages %}
                <div class="alert-{{ category }}">{{ message }}</div>
              {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        <div class="card">
            <h2 style="margin-top: 0;">账号权限列表</h2>
            <table class="acct-table">
                <tr>
                    <th>ID</th>
                    <th>账号</th>
                    <th>姓名</th>
                    <th>手机号</th>
                    <th>绑定员工</th>
                    <th>权限等级</th>
                    <th>状态</th>
                    <th>创建时间</th>
                    <th>操作</th>
                </tr>
                {% for u in users %}
                <tr>
                    <td>{{ u.id }}</td>
                    <td>{{ u.username }}</td>
                    <td>{{ u.real_name or '-' }}</td>
                    <td>{{ u.phone or '-' }}</td>
                    <td>{{ u.bound_staff_name or '未绑定' }}</td>
                    <td>{{ u.role_level }}</td>
                    <td>{{ '启用' if u.status == 1 else '禁用' }}</td>
                    <td>{{ u.create_time }}</td>
                    <td>
                        <form method="post" action="{{ url_for('update_account_permission', user_id=u.id) }}" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">
                            <select name="role_level" title="权限等级">
                                <option value="1" {% if u.role_level == 1 %}selected{% endif %}>1-普通</option>
                                <option value="2" {% if u.role_level == 2 %}selected{% endif %}>2-派发</option>
                                <option value="3" {% if u.role_level >= 3 %}selected{% endif %}>3-管理员</option>
                            </select>
                            <select name="status" title="账号状态">
                                <option value="1" {% if u.status == 1 %}selected{% endif %}>启用</option>
                                <option value="0" {% if u.status != 1 %}selected{% endif %}>禁用</option>
                            </select>
                            <button type="submit" class="btn">保存</button>
                        </form>
                        <form method="post" action="{{ url_for('bind_account_staff', user_id=u.id) }}" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                            <select name="staff_id" title="绑定员工">
                                <option value="">不绑定</option>
                                {% for s in staff_list %}
                                <option value="{{ s.staff_id }}" {% if u.staff_id == s.staff_id %}selected{% endif %}>
                                    {{ s.full_name }}（ID {{ s.staff_id }} · {{ s.position }}）
                                </option>
                                {% endfor %}
                            </select>
                            <button type="submit" class="btn">绑定</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </table>

            <!-- 手机端账号卡片列表 -->
            <div class="acct-cards">
                {% for u in users %}
                <div class="acct-card">
                    <div class="acct-card-name">{{ u.username }}{% if u.real_name %} · {{ u.real_name }}{% endif %}</div>
                    <div class="acct-card-meta">
                        手机：{{ u.phone or '-' }}<br>
                        绑定员工：{{ u.bound_staff_name or '未绑定' }}<br>
                        权限：{{ u.role_level }} · {{ '启用' if u.status == 1 else '禁用' }}<br>
                        创建：{{ u.create_time }}
                    </div>
                    <div class="acct-card-forms">
                        <form method="post" action="{{ url_for('update_account_permission', user_id=u.id) }}">
                            <select name="role_level" title="权限等级">
                                <option value="1" {% if u.role_level == 1 %}selected{% endif %}>1-普通</option>
                                <option value="2" {% if u.role_level == 2 %}selected{% endif %}>2-派发</option>
                                <option value="3" {% if u.role_level >= 3 %}selected{% endif %}>3-管理员</option>
                            </select>
                            <select name="status" title="账号状态">
                                <option value="1" {% if u.status == 1 %}selected{% endif %}>启用</option>
                                <option value="0" {% if u.status != 1 %}selected{% endif %}>禁用</option>
                            </select>
                            <button type="submit" class="btn">保存</button>
                        </form>
                        <form method="post" action="{{ url_for('bind_account_staff', user_id=u.id) }}">
                            <select name="staff_id" title="绑定员工" style="flex:1;min-width:0;">
                                <option value="">不绑定</option>
                                {% for s in staff_list %}
                                <option value="{{ s.staff_id }}" {% if u.staff_id == s.staff_id %}selected{% endif %}>{{ s.full_name }}（{{ s.position }}）</option>
                                {% endfor %}
                            </select>
                            <button type="submit" class="btn">绑定</button>
                        </form>
                    </div>
                </div>
                {% endfor %}
            </div>

            <p style="color:#6b7280;font-size:13px;margin-bottom:0;">规则：权限等级3及以上可管理员工信息与账号权限；权限等级2及以上可派单。</p>
        </div>
    </div>
</body>
</html>
'''

# 添加员工页面模板
ADD_STAFF_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>添加员工 - 装维部门员工信息管理系统</title>
    <style>
        /* 保持与主页面相同的样式 */
        body {
            font-family: "Microsoft YaHei", sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
        }
        header {
            background-color: #2c3e50;
            color: white;
            padding: 15px 0;
            border-radius: 5px;
            margin-bottom: 20px;
        }
        .header-content {
            padding: 0 20px;
        }
        .btn {
            display: inline-block;
            padding: 8px 16px;
            background-color: #3498db;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            text-decoration: none;
            font-size: 14px;
        }
        .btn-success {
            background-color: #2ecc71;
        }
        .btn-danger {
            background-color: #e74c3c;
        }
        .card {
            background-color: white;
            border-radius: 5px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
        }
        .form-group {
            margin-bottom: 15px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: bold;
        }
        input, select, textarea {
            width: 100%;
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
            box-sizing: border-box;
        }
        .form-actions {
            margin-top: 20px;
            display: flex;
            gap: 10px;
        }
        .flash-messages {
            margin-bottom: 20px;
            padding: 10px;
            border-radius: 4px;
        }
        .alert-success {
            background-color: #dff0d8;
            color: #3c763d;
            border: 1px solid #d6e9c6;
        }
        .alert-danger {
            background-color: #f2dede;
            color: #a94442;
            border: 1px solid #ebccd1;
        }
        .required-mark {
            color: #e74c3c;
        }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .card { padding: 14px; }
            input, select, textarea { font-size: 16px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-content">
                <h1>装维部门员工信息管理系统</h1>
            </div>
        </header>

        <!-- 消息提示区域 -->
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="flash-messages">
              {% for category, message in messages %}
                <div class="alert-{{ category }}">{{ message }}</div>
              {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        <div class="card">
            <h2>添加新员工</h2>
            <form method="post">
                <div class="form-group">
                    <label for="full_name">姓名 <span class="required-mark">*</span></label>
                    <input type="text" id="full_name" name="full_name" required>
                </div>
                <div class="form-group">
                    <label for="gender">性别 <span class="required-mark">*</span></label>
                    <select id="gender" name="gender" required>
                        <option value="">请选择</option>
                        <option value="男">男</option>
                        <option value="女">女</option>
                        <option value="其他">其他</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="id_card">身份证号 <span class="required-mark">*</span></label>
                    <input type="text" id="id_card" name="id_card" required placeholder="18位身份证号">
                </div>
                <div class="form-group">
                    <label for="private_phone">私人电话</label>
                    <input type="text" id="private_phone" name="private_phone" placeholder="11位数字">
                </div>
                <div class="form-group">
                    <label for="work_phone">工作电话</label>
                    <input type="text" id="work_phone" name="work_phone" placeholder="11位数字">
                </div>
                <div class="form-group">
                    <label for="emergency_contact">紧急联系人</label>
                    <input type="text" id="emergency_contact" name="emergency_contact">
                </div>
                <div class="form-group">
                    <label for="emergency_phone">紧急联系电话</label>
                    <input type="text" id="emergency_phone" name="emergency_phone" placeholder="11位数字">
                </div>
                <div class="form-group">
                    <label for="education">学历</label>
                    <select id="education" name="education">
                        <option value="">请选择</option>
                        <option value="高中及以下">高中及以下</option>
                        <option value="大专">大专</option>
                        <option value="本科">本科</option>
                        <option value="硕士">硕士</option>
                        <option value="博士">博士</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="entry_date">入职日期 <span class="required-mark">*</span></label>
                    <input type="date" id="entry_date" name="entry_date" required>
                </div>
                <div class="form-group">
                    <label for="departure_date">离职日期</label>
                    <input type="date" id="departure_date" name="departure_date">
                </div>
                <div class="form-group">
                    <label for="region_id">区域ID</label>
                    <input type="number" id="region_id" name="region_id" min="1">
                </div>
                <div class="form-group">
                    <label for="department">所属部门</label>
                    <input type="text" id="department" name="department" maxlength="50" placeholder="如：装维一部、客服部">
                </div>
                <div class="form-group">
                    <label for="team_id">班组ID</label>
                    <input type="number" id="team_id" name="team_id" min="1">
                </div>
                <div class="form-group">
                    <label for="team_name">班组名称</label>
                    <input type="text" id="team_name" name="team_name" maxlength="50" placeholder="如：一班、光纤组">
                </div>
                <div class="form-group">
                    <label for="position">岗位 <span class="required-mark">*</span></label>
                    <select id="position" name="position" required>
                        <option value="">请选择</option>
                        <option value="装维工程师">装维工程师</option>
                        <option value="班组长">班组长</option>
                        <option value="区域主管">区域主管</option>
                        <option value="HR专员">HR专员</option>
                        <option value="财务专员">财务专员</option>
                        <option value="系统管理员">系统管理员</option>
                    </select>
                </div>
                <div class="form-actions">
                    <button type="submit" class="btn btn-success">保存</button>
                    <a href="{{ url_for('staff_page') }}" class="btn btn-danger">取消</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
'''

# 编辑员工页面模板
EDIT_STAFF_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>编辑员工 - 装维部门员工信息管理系统</title>
    <style>
        /* 保持与主页面相同的样式 */
        body {
            font-family: "Microsoft YaHei", sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
        }
        header {
            background-color: #2c3e50;
            color: white;
            padding: 15px 0;
            border-radius: 5px;
            margin-bottom: 20px;
        }
        .header-content {
            padding: 0 20px;
        }
        .btn {
            display: inline-block;
            padding: 8px 16px;
            background-color: #3498db;
            color: white;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            text-decoration: none;
            font-size: 14px;
        }
        .btn-success {
            background-color: #2ecc71;
        }
        .btn-danger {
            background-color: #e74c3c;
        }
        .card {
            background-color: white;
            border-radius: 5px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 5px rgba(0,0,0,0.1);
        }
        .form-group {
            margin-bottom: 15px;
        }
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: bold;
        }
        input, select, textarea {
            width: 100%;
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
            box-sizing: border-box;
        }
        .form-actions {
            margin-top: 20px;
            display: flex;
            gap: 10px;
        }
        .flash-messages {
            margin-bottom: 20px;
            padding: 10px;
            border-radius: 4px;
        }
        .alert-success {
            background-color: #dff0d8;
            color: #3c763d;
            border: 1px solid #d6e9c6;
        }
        .alert-danger {
            background-color: #f2dede;
            color: #a94442;
            border: 1px solid #ebccd1;
        }
        .required-mark {
            color: #e74c3c;
        }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .card { padding: 14px; }
            input, select, textarea { font-size: 16px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-content">
                <h1>装维部门员工信息管理系统</h1>
            </div>
        </header>

        <!-- 消息提示区域 -->
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="flash-messages">
              {% for category, message in messages %}
                <div class="alert-{{ category }}">{{ message }}</div>
              {% endfor %}
            </div>
          {% endif %}
        {% endwith %}

        <div class="card">
            <h2>编辑员工信息</h2>
            <form method="post">
                <div class="form-group">
                    <label for="full_name">姓名 <span class="required-mark">*</span></label>
                    <input type="text" id="full_name" name="full_name" value="{{ staff.full_name }}" required>
                </div>
                <div class="form-group">
                    <label for="gender">性别 <span class="required-mark">*</span></label>
                    <select id="gender" name="gender" required>
                        <option value="男" {% if staff.gender == '男' %}selected{% endif %}>男</option>
                        <option value="女" {% if staff.gender == '女' %}selected{% endif %}>女</option>
                        <option value="其他" {% if staff.gender == '其他' %}selected{% endif %}>其他</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="id_card">身份证号 <span class="required-mark">*</span></label>
                    <input type="text" id="id_card" name="id_card" value="{{ staff.id_card }}" required placeholder="18位身份证号">
                </div>
                <div class="form-group">
                    <label for="private_phone">私人电话</label>
                    <input type="text" id="private_phone" name="private_phone" value="{{ staff.private_phone or '' }}" placeholder="11位数字">
                </div>
                <div class="form-group">
                    <label for="work_phone">工作电话</label>
                    <input type="text" id="work_phone" name="work_phone" value="{{ staff.work_phone or '' }}" placeholder="11位数字">
                </div>
                <div class="form-group">
                    <label for="emergency_contact">紧急联系人</label>
                    <input type="text" id="emergency_contact" name="emergency_contact" value="{{ staff.emergency_contact or '' }}">
                </div>
                <div class="form-group">
                    <label for="emergency_phone">紧急联系电话</label>
                    <input type="text" id="emergency_phone" name="emergency_phone" value="{{ staff.emergency_phone or '' }}" placeholder="11位数字">
                </div>
                <div class="form-group">
                    <label for="education">学历</label>
                    <select id="education" name="education">
                        <option value="">请选择</option>
                        <option value="高中及以下" {% if staff.education == '高中及以下' %}selected{% endif %}>高中及以下</option>
                        <option value="大专" {% if staff.education == '大专' %}selected{% endif %}>大专</option>
                        <option value="本科" {% if staff.education == '本科' %}selected{% endif %}>本科</option>
                        <option value="硕士" {% if staff.education == '硕士' %}selected{% endif %}>硕士</option>
                        <option value="博士" {% if staff.education == '博士' %}selected{% endif %}>博士</option>
                    </select>
                </div>
                <div class="form-group">
                    <label for="entry_date">入职日期 <span class="required-mark">*</span></label>
                    <input type="date" id="entry_date" name="entry_date" value="{{ staff.entry_date }}" required>
                </div>
                <div class="form-group">
                    <label for="departure_date">离职日期</label>
                    <input type="date" id="departure_date" name="departure_date" value="{{ staff.departure_date or '' }}">
                </div>
                <div class="form-group">
                    <label for="region_id">区域ID</label>
                    <input type="number" id="region_id" name="region_id" value="{{ staff.region_id or '' }}" min="1">
                </div>
                <div class="form-group">
                    <label for="department">所属部门</label>
                    <input type="text" id="department" name="department" value="{{ staff.department or '' }}" maxlength="50" placeholder="如：装维一部、客服部">
                </div>
                <div class="form-group">
                    <label for="team_id">班组ID</label>
                    <input type="number" id="team_id" name="team_id" value="{{ staff.team_id or '' }}" min="1">
                </div>
                <div class="form-group">
                    <label for="team_name">班组名称</label>
                    <input type="text" id="team_name" name="team_name" value="{{ staff.team_name or '' }}" maxlength="50" placeholder="如：一班、光纤组">
                </div>
                <div class="form-group">
                    <label for="position">岗位 <span class="required-mark">*</span></label>
                    <select id="position" name="position" required>
                        <option value="装维工程师" {% if staff.position == '装维工程师' %}selected{% endif %}>装维工程师</option>
                        <option value="班组长" {% if staff.position == '班组长' %}selected{% endif %}>班组长</option>
                        <option value="区域主管" {% if staff.position == '区域主管' %}selected{% endif %}>区域主管</option>
                        <option value="HR专员" {% if staff.position == 'HR专员' %}selected{% endif %}>HR专员</option>
                        <option value="财务专员" {% if staff.position == '财务专员' %}selected{% endif %}>财务专员</option>
                        <option value="系统管理员" {% if staff.position == '系统管理员' %}selected{% endif %}>系统管理员</option>
                    </select>
                </div>
                <div class="form-actions">
                    <button type="submit" class="btn btn-success">保存</button>
                    <a href="{{ url_for('staff_page') }}" class="btn btn-danger">取消</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
'''

TASK_DETAIL_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>工单详情 #{{ task.task_id }} - 装维部门</title>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .container { max-width: 960px; margin: 0 auto; }
        header { background: #2c3e50; color: white; padding: 15px 0; border-radius: 5px; margin-bottom: 20px; }
        .header-content { padding: 0 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }
        .card { background: white; border-radius: 5px; padding: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 16px; }
        .meta-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; font-size: 14px; }
        .meta-item label { display: block; color: #6b7280; font-size: 12px; margin-bottom: 4px; }
        .meta-item span { font-weight: 600; color: #111827; }
        .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
        .status-待派单 { background: #fff4d6; color: #9a6700; }
        .status-已派单 { background: #ddf4ff; color: #0550ae; }
        .status-处理中 { background: #d0ebff; color: #1971c2; }
        .status-待回执 { background: #f3d9fa; color: #862e9c; }
        .status-已完成 { background: #dafbe1; color: #1a7f37; }
        .status-已取消 { background: #f6f8fa; color: #656d76; }
        .priority-高 { color: #cf222e; background: #ffebe9; }
        .priority-中 { color: #9a6700; background: #fff4d6; }
        .priority-低 { color: #1a7f37; background: #dafbe1; }
        .btn { display: inline-block; padding: 8px 16px; border-radius: 4px; text-decoration: none; font-size: 14px; border: none; cursor: pointer; }
        .btn-primary { background: #3498db; color: white; }
        .btn-success { background: #2ecc71; color: white; }
        .btn-danger { background: #e74c3c; color: white; }
        .btn-light { background: #ecf0f1; color: #2c3e50; }
        .btn-warning { background: #f39c12; color: white; }
        .btn-sm { padding: 4px 10px; font-size: 12px; }
        .gallery { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 12px; min-height: 40px; }
        .gallery-item { border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px; width: 160px; text-align: center; background: #fafafa; }
        .gallery-item img { max-width: 140px; max-height: 100px; object-fit: cover; border-radius: 4px; display: block; margin: 0 auto 8px auto; }
        .upload-box { border: 1px dashed #bdc3c7; border-radius: 6px; padding: 16px; margin-top: 12px; }
        .upload-status { margin-top: 10px; font-size: 13px; color: #6b7280; min-height: 18px; }
        .upload-status.error { color: #b91c1c; }
        .upload-status.ok { color: #15803d; }
        .empty-hint { color: #9ca3af; font-size: 14px; }
        .desc-block { margin-top: 12px; padding: 12px; background: #f9fafb; border-radius: 6px; font-size: 14px; line-height: 1.6; white-space: pre-wrap; }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .header-content { flex-direction: column; align-items: flex-start; gap: 8px; }
            .meta-grid { grid-template-columns: 1fr 1fr; }
            .gallery-item { width: calc(50% - 7px); }
            .gallery-item img { max-width: 100%; }
            .card { padding: 14px; }
        }
        @media (max-width: 400px) {
            .meta-grid { grid-template-columns: 1fr; }
            .gallery-item { width: 100%; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-content">
                <h1 style="margin:0;">工单详情 #{{ task.task_id }}</h1>
                <div>
                    <a href="{{ url_for('tasks_page') }}" class="btn btn-light">返回列表</a>
                </div>
            </div>
        </header>

        <div class="card">
            <h2 style="margin-top:0;">{{ task.title }}</h2>
            <div class="meta-grid">
                <div class="meta-item"><label>状态</label><span class="pill status-{{ task.status }}">{{ task.status }}</span></div>
                <div class="meta-item"><label>优先级</label><span class="pill priority-{{ task.priority }}">{{ task.priority }}</span></div>
                <div class="meta-item"><label>业务类型</label><span>{{ task.fault_type or '—' }}</span></div>
                {% if task.fault_phenomenon %}<div class="meta-item"><label>故障现象</label><span>{{ task.fault_phenomenon }}</span></div>{% endif %}
                {% if task.resolution_method %}<div class="meta-item"><label>处理方式</label><span>{{ task.resolution_method }}</span></div>{% endif %}
                <div class="meta-item"><label>客户地址</label><span>{{ task.customer_address or '—' }}</span></div>
                <div class="meta-item"><label>截止日期</label><span>{{ task.due_date or '—' }}</span></div>
                <div class="meta-item"><label>执行人</label><span>{{ task.assigned_staff_name or '—' }}</span></div>
                <div class="meta-item"><label>派单时间</label><span>{{ task.assigned_at or '—' }}</span></div>
                <div class="meta-item"><label>创建/更新</label><span>{{ task.created_at or '—' }} / {{ task.updated_at or '—' }}</span></div>
            </div>
            {% if task.description %}
            <div class="desc-block">{{ task.description }}</div>
            {% endif %}
        </div>

        <div class="card">
            <h2 style="margin-top:0;">现场照片</h2>
            <p style="color:#6b7280;font-size:14px;margin:0 0 8px 0;">支持 jpg/png/gif/webp，单张 ≤ 5MB，每单最多 {{ max_photos }} 张。选择本地图片后点击上传，将以 FormData 提交至服务器。</p>
            <div id="imageGallery" class="gallery"></div>
            <p id="galleryEmpty" class="empty-hint" style="display:none;">暂无照片，请在下方上传。</p>

            <div class="upload-box">
                <input type="file" id="photoInput" accept="image/jpeg,image/png,image/gif,image/webp" multiple>
                <div style="margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;">
                    <button type="button" id="btnUpload" class="btn btn-success">上传选中图片</button>
                    <button type="button" id="btnRefresh" class="btn btn-light">刷新列表</button>
                </div>
                <div id="uploadStatus" class="upload-status"></div>
            </div>
        </div>
    </div>
    <script>
    (function() {
        const taskId = {{ task.task_id }};
        const maxPhotos = {{ max_photos }};
        const galleryEl = document.getElementById('imageGallery');
        const emptyEl = document.getElementById('galleryEmpty');
        const statusEl = document.getElementById('uploadStatus');
        const initialImages = {{ images | tojson }};

        function setStatus(text, type) {
            statusEl.textContent = text || '';
            statusEl.className = 'upload-status' + (type ? ' ' + type : '');
        }

        function renderGallery(images) {
            galleryEl.innerHTML = '';
            if (!images || !images.length) {
                emptyEl.style.display = 'block';
                return;
            }
            emptyEl.style.display = 'none';
            images.forEach(function(img) {
                const wrap = document.createElement('div');
                wrap.className = 'gallery-item';
                wrap.dataset.id = img.image_id;
                const link = document.createElement('a');
                link.href = img.url;
                link.target = '_blank';
                link.rel = 'noopener';
                const image = document.createElement('img');
                image.src = img.url;
                image.alt = img.original_filename || '现场照片';
                link.appendChild(image);
                const cap = document.createElement('div');
                cap.style.fontSize = '12px';
                cap.style.color = '#6b7280';
                cap.textContent = img.created_at || '';
                const del = document.createElement('button');
                del.type = 'button';
                del.className = 'btn btn-danger btn-sm';
                del.style.marginTop = '6px';
                del.textContent = '删除';
                del.addEventListener('click', function() { deleteImage(img.image_id); });
                wrap.appendChild(link);
                wrap.appendChild(cap);
                wrap.appendChild(del);
                galleryEl.appendChild(wrap);
            });
        }

        async function loadImages() {
            const res = await fetch('/api/task/' + taskId + '/images');
            const data = await res.json();
            if (!data.ok) {
                setStatus(data.message || '加载失败', 'error');
                return;
            }
            renderGallery(data.images);
            setStatus('共 ' + (data.images ? data.images.length : 0) + ' 张 / 最多 ' + maxPhotos + ' 张', '');
        }

        async function uploadPhotos() {
            const input = document.getElementById('photoInput');
            if (!input.files || !input.files.length) {
                setStatus('请先选择图片文件', 'error');
                return;
            }
            const fd = new FormData();
            for (let i = 0; i < input.files.length; i++) {
                fd.append('photos', input.files[i]);
            }
            setStatus('上传中…', '');
            document.getElementById('btnUpload').disabled = true;
            try {
                const res = await fetch('/api/task/' + taskId + '/images', { method: 'POST', body: fd });
                const data = await res.json();
                if (!data.ok) {
                    setStatus(data.message || '上传失败', 'error');
                    return;
                }
                renderGallery(data.images);
                input.value = '';
                setStatus(data.message || '上传成功', 'ok');
            } catch (e) {
                setStatus('网络错误：' + e, 'error');
            } finally {
                document.getElementById('btnUpload').disabled = false;
            }
        }

        async function deleteImage(imageId) {
            if (!confirm('确定删除这张照片？')) return;
            setStatus('删除中…', '');
            try {
                const res = await fetch('/api/task/' + taskId + '/images/' + imageId, { method: 'DELETE' });
                const data = await res.json();
                if (!data.ok) {
                    setStatus(data.message || '删除失败', 'error');
                    return;
                }
                renderGallery(data.images);
                setStatus('已删除', 'ok');
            } catch (e) {
                setStatus('网络错误：' + e, 'error');
            }
        }

        document.getElementById('btnUpload').addEventListener('click', uploadPhotos);
        document.getElementById('btnRefresh').addEventListener('click', loadImages);
        renderGallery(initialImages);
        if (initialImages && initialImages.length) {
            setStatus('共 ' + initialImages.length + ' 张 / 最多 ' + maxPhotos + ' 张', '');
        }
    })();
    </script>
</body>
</html>
'''

ADD_TASK_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>添加任务 - 装维部门员工信息管理系统</title>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .container { max-width: 800px; margin: 0 auto; }
        header { background: #2c3e50; color: white; padding: 15px 0; border-radius: 5px; margin-bottom: 20px; }
        .header-content { padding: 0 20px; }
        .card { background: white; border-radius: 5px; padding: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input, select, textarea { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        textarea { min-height: 120px; resize: vertical; }
        .btn { display: inline-block; padding: 8px 16px; border-radius: 4px; text-decoration: none; font-size: 14px; border: none; cursor: pointer; }
        .btn-success { background: #2ecc71; color: white; }
        .btn-danger { background: #e74c3c; color: white; }
        .form-actions { margin-top: 20px; display: flex; gap: 10px; }
        .required-mark { color: #e74c3c; }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .card { padding: 14px; }
            input, select, textarea { font-size: 16px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header><div class="header-content"><h1>添加任务</h1></div></header>
        <div class="card">
            <p style="color:#7f8c8d;margin-top:0;">新任务默认为「待派单」，保存后进入工单详情页上传现场照片，管理员可在列表中「派单」给指定员工，员工也可主动接取。</p>
            <form method="post">
                <div class="form-group">
                    <label for="title">任务标题 <span class="required-mark">*</span></label>
                    <input type="text" id="title" name="title" required maxlength="200">
                </div>
                <div class="form-group">
                    <label for="fault_type">业务类型</label>
                    <select id="fault_type" name="fault_type" onchange="onBizTypeChange(this.value)">
                        <option value="">请选择</option>
                        {% for ft in fault_business_types %}
                        <option value="{{ ft }}">{{ ft }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="form-group" id="phenomenon_row" style="display:none;">
                    <label for="fault_phenomenon">故障现象</label>
                    <select id="fault_phenomenon" name="fault_phenomenon">
                        <option value="">请选择</option>
                        {% for fp in fault_phenomena %}
                        <option value="{{ fp }}">{{ fp }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="form-group">
                    <label for="resolution_method">处理方式</label>
                    <select id="resolution_method" name="resolution_method">
                        <option value="">请选择（可完工后填写）</option>
                        {% for rm in resolution_methods %}
                        <option value="{{ rm }}">{{ rm }}</option>
                        {% endfor %}
                    </select>
                </div>
                <script>
                var _needPhenomenon = {{ fault_types_need_phenomenon | tojson }};
                function onBizTypeChange(v) {
                    document.getElementById('phenomenon_row').style.display = _needPhenomenon.indexOf(v) >= 0 ? '' : 'none';
                    if (_needPhenomenon.indexOf(v) < 0) document.getElementById('fault_phenomenon').value = '';
                }
                </script>
                <div class="form-group">
                    <label for="customer_address">客户地址</label>
                    <input type="text" id="customer_address" name="customer_address" maxlength="500" placeholder="客户详细地址">
                </div>
                <div class="form-group">
                    <label for="description">任务说明</label>
                    <textarea id="description" name="description" placeholder="可选：具体要求、客户信息等"></textarea>
                </div>
                <div class="form-group">
                    <label for="priority">优先级</label>
                    <select id="priority" name="priority">
                        {% for p in priorities %}
                        <option value="{{ p }}" {% if p == '中' %}selected{% endif %}>{{ p }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="form-group">
                    <label for="due_date">截止日期</label>
                    <input type="date" id="due_date" name="due_date">
                </div>
                <div class="form-actions">
                    <button type="submit" class="btn btn-success">保存</button>
                    <a href="{{ url_for('tasks_page') }}" class="btn btn-danger">取消</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
'''

EDIT_TASK_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>编辑任务 - 装维部门员工信息管理系统</title>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .container { max-width: 800px; margin: 0 auto; }
        header { background: #2c3e50; color: white; padding: 15px 0; border-radius: 5px; margin-bottom: 20px; }
        .header-content { padding: 0 20px; }
        .card { background: white; border-radius: 5px; padding: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input, select, textarea { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        textarea { min-height: 120px; resize: vertical; }
        .btn { display: inline-block; padding: 8px 16px; border-radius: 4px; text-decoration: none; font-size: 14px; border: none; cursor: pointer; }
        .btn-success { background: #2ecc71; color: white; }
        .btn-danger { background: #e74c3c; color: white; }
        .form-actions { margin-top: 20px; display: flex; gap: 10px; flex-wrap: wrap; }
        .required-mark { color: #e74c3c; }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .card { padding: 14px; }
            input, select, textarea { font-size: 16px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header><div class="header-content"><h1>编辑任务 #{{ task.task_id }}</h1></div></header>
        <div class="card">
            <p style="color:#7f8c8d;margin-top:0;">将状态改为「待派单」会清空执行人与派单时间；指派员工请使用任务列表中的「派单」。现场照片请在 <a href="{{ url_for('task_detail', task_id=task.task_id) }}">工单详情</a> 页上传与管理。</p>
            <form method="post">
                <div class="form-group">
                    <label for="title">任务标题 <span class="required-mark">*</span></label>
                    <input type="text" id="title" name="title" required maxlength="200" value="{{ task.title }}">
                </div>
                <div class="form-group">
                    <label for="fault_type">业务类型</label>
                    <select id="fault_type" name="fault_type" onchange="onBizTypeChange(this.value)">
                        <option value="">请选择</option>
                        {% for ft in fault_business_types %}
                        <option value="{{ ft }}" {% if task.fault_type == ft %}selected{% endif %}>{{ ft }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="form-group" id="phenomenon_row" style="display:none;">
                    <label for="fault_phenomenon">故障现象</label>
                    <select id="fault_phenomenon" name="fault_phenomenon">
                        <option value="">请选择</option>
                        {% for fp in fault_phenomena %}
                        <option value="{{ fp }}" {% if task.fault_phenomenon == fp %}selected{% endif %}>{{ fp }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="form-group">
                    <label for="resolution_method">处理方式</label>
                    <select id="resolution_method" name="resolution_method">
                        <option value="">请选择</option>
                        {% for rm in resolution_methods %}
                        <option value="{{ rm }}" {% if task.resolution_method == rm %}selected{% endif %}>{{ rm }}</option>
                        {% endfor %}
                    </select>
                </div>
                <script>
                var _needPhenomenon = {{ fault_types_need_phenomenon | tojson }};
                function onBizTypeChange(v) {
                    document.getElementById('phenomenon_row').style.display = _needPhenomenon.indexOf(v) >= 0 ? '' : 'none';
                    if (_needPhenomenon.indexOf(v) < 0) document.getElementById('fault_phenomenon').value = '';
                }
                (function(){ onBizTypeChange({{ task.fault_type | tojson }}); })();
                </script>
                <div class="form-group">
                    <label for="customer_address">客户地址</label>
                    <input type="text" id="customer_address" name="customer_address" maxlength="500" value="{{ task.customer_address or '' }}">
                </div>
                <div class="form-group">
                    <label for="description">任务说明</label>
                    <textarea id="description" name="description">{{ task.description or '' }}</textarea>
                </div>
                <div class="form-group">
                    <label for="priority">优先级</label>
                    <select id="priority" name="priority">
                        {% for p in priorities %}
                        <option value="{{ p }}" {% if task.priority == p %}selected{% endif %}>{{ p }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="form-group">
                    <label for="status">状态</label>
                    <select id="status" name="status">
                        {% for s in statuses %}
                        <option value="{{ s }}" {% if task.status == s %}selected{% endif %}>{{ s }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="form-group">
                    <label for="due_date">截止日期</label>
                    <input type="date" id="due_date" name="due_date" value="{{ task.due_date or '' }}">
                </div>
                <div class="form-actions">
                    <button type="submit" class="btn btn-success">保存</button>
                    <a href="{{ url_for('tasks_page') }}" class="btn btn-danger">取消</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
'''

ASSIGN_TASK_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>派单 - 装维部门员工信息管理系统</title>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .container { max-width: 800px; margin: 0 auto; }
        header { background: #2c3e50; color: white; padding: 15px 0; border-radius: 5px; margin-bottom: 20px; }
        .header-content { padding: 0 20px; }
        .card { background: white; border-radius: 5px; padding: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        select { width: 100%; padding: 8px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        .btn { display: inline-block; padding: 8px 16px; border-radius: 4px; text-decoration: none; font-size: 14px; border: none; cursor: pointer; }
        .btn-primary { background: #3498db; color: white; }
        .btn-danger { background: #e74c3c; color: white; }
        .form-actions { margin-top: 20px; display: flex; gap: 10px; }
        .task-summary { background: #ecf0f1; padding: 12px; border-radius: 4px; margin-bottom: 16px; }
        @media (max-width: 700px) {
            body { padding: 10px; }
            .container { max-width: 100%; }
            .card { padding: 14px; }
            select { font-size: 16px; }
        }
    </style>
</head>
<body>
    <div class="container">
        <header><div class="header-content"><h1>派单 #{{ task.task_id }}</h1></div></header>
        <div class="card">
            <div class="task-summary">
                <strong>{{ task.title }}</strong>
                {% if task.description %}<p style="margin:8px 0 0 0;color:#555;">{{ task.description }}</p>{% endif %}
                <p style="margin:8px 0 0 0;font-size:14px;color:#7f8c8d;">当前状态：{{ task.status }}
                {% if task.assigned_staff_id %} · 当前执行人ID：{{ task.assigned_staff_id }}{% endif %}</p>
            </div>
            <form method="post">
                <div class="form-group">
                    <label for="assigned_staff_id">选择执行人（在职员工） <span style="color:#e74c3c">*</span></label>
                    <select id="assigned_staff_id" name="assigned_staff_id" required>
                        <option value="">请选择</option>
                        {% for team_label, members in staff_options.items() %}
                        <optgroup label="{{ team_label }}">
                            {% for s in members %}
                            <option value="{{ s.staff_id }}" {% if task.assigned_staff_id == s.staff_id %}selected{% endif %}>{{ s.full_name }}（{{ s.position }}）</option>
                            {% endfor %}
                        </optgroup>
                        {% endfor %}
                    </select>
                </div>
                <p style="font-size:13px;color:#7f8c8d;">派单后任务状态将设为「已派单」，并记录派单时间。可对已派单任务再次派单以调整执行人。</p>
                <div class="form-actions">
                    <button type="submit" class="btn btn-primary">确认派单</button>
                    <a href="{{ url_for('tasks_page') }}" class="btn btn-danger">取消</a>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
'''

if __name__ == '__main__':
    app.run(debug=True)