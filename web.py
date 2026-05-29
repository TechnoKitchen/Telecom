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
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    _APSCHEDULER_AVAILABLE = True
except ImportError:
    _APSCHEDULER_AVAILABLE = False
import hashlib
import secrets
import urllib.request as _urlreq

cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "devpzlgvg"),
    api_key=os.environ.get("CLOUDINARY_API_KEY", "262731876599449"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "wEe1TrqGdkieRuwS24viPUDpAz8"),
    secure=True,
)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'a_secret_key_for_flask_flash_messages')

# 数据库配置（优先读取环境变量，本地开发回退到默认值）
# 兼容 Railway MySQL 插件变量名（MYSQLHOST 等）和自定义变量名（DB_HOST 等）
DB_CONFIG = {
    "host": os.environ.get("DB_HOST") or os.environ.get("MYSQLHOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT") or os.environ.get("MYSQLPORT", 3306)),
    "user": os.environ.get("DB_USER") or os.environ.get("MYSQLUSER", "root"),
    "password": os.environ.get("DB_PASSWORD") or os.environ.get("MYSQLPASSWORD", "tlxsdy8823166"),
    "database": os.environ.get("DB_NAME") or os.environ.get("MYSQLDATABASE", "telecom_maintenance"),
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
    public_endpoints = {"login", "register", "static", "user_login", "user_register", "user_index"}
    if request.endpoint is None:
        return None
    if request.endpoint in public_endpoints:
        return None
    if request.endpoint.startswith("user_"):
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
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'returned_by'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE maintenance_tasks ADD COLUMN returned_by INT NULL AFTER resolution_method"
        )
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'return_count'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE maintenance_tasks ADD COLUMN return_count INT NOT NULL DEFAULT 0 AFTER returned_by"
        )
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_return_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            task_id INT NOT NULL,
            staff_id INT NOT NULL,
            returned_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_return_staff (staff_id),
            INDEX idx_return_time (returned_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'helper_staff_id'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN helper_staff_id INT NULL AFTER return_count")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'helper_requested_at'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN helper_requested_at DATETIME NULL AFTER helper_staff_id")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'helper_accepted_at'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN helper_accepted_at DATETIME NULL AFTER helper_requested_at")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'helper_status'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN helper_status VARCHAR(20) NULL AFTER helper_accepted_at")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'helper_admin_note'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN helper_admin_note TEXT NULL AFTER helper_status")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'arrived_at'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN arrived_at DATETIME NULL AFTER helper_status")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'completed_at'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN completed_at DATETIME NULL AFTER arrived_at")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'settled_at'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN settled_at DATETIME NULL AFTER completed_at")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'settled_by'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN settled_by INT NULL AFTER settled_at")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'source_report_id'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN source_report_id INT NULL")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'reporter_user_id'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN reporter_user_id INT NULL")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'suspended_at'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN suspended_at DATETIME NULL")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'suspended_by'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN suspended_by INT NULL")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'suspend_reason'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN suspend_reason TEXT NULL")
    cur.execute("SHOW COLUMNS FROM maintenance_tasks LIKE 'overdue_at'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE maintenance_tasks ADD COLUMN overdue_at DATETIME NULL")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS task_suspend_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            task_id INT NOT NULL,
            staff_id INT NOT NULL,
            suspended_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_suspend_staff (staff_id),
            INDEX idx_suspend_time (suspended_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
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
        image_type VARCHAR(20) NOT NULL DEFAULT 'general',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_task_images_task (task_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    cur = connection.cursor()
    cur.execute(ddl)
    cur.execute("SHOW COLUMNS FROM task_images LIKE 'image_type'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE task_images ADD COLUMN image_type VARCHAR(20) NOT NULL DEFAULT 'general' AFTER original_filename")
    connection.commit()
    cur.close()


def ensure_task_settlements_table(connection):
    """工单结算记录表"""
    cur = connection.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS task_settlements (
        settlement_id INT AUTO_INCREMENT PRIMARY KEY,
        task_id INT NOT NULL,
        staff_id INT NOT NULL,
        settled_by INT NOT NULL,
        settled_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        notes TEXT NULL,
        INDEX idx_settlements_task (task_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
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
TASK_STATUSES = ("待派单", "已派单", "处理中", "待回执", "已完成", "已取消", "已归档", "已挂起")
FAULT_BUSINESS_TYPES = ("宽带新装", "宽带修障", "宽带移机", "宽带提速", "IPTV新装", "IPTV修障", "电话新装", "电话修障", "智能组网", "设备更换", "线路维护")
FAULT_PHENOMENA = ("光衰过大", "ONU离线", "网速不达标", "IPTV卡顿", "WiFi覆盖差", "电话无声", "线路中断", "其他")
RESOLUTION_METHODS = ("更换光猫", "重新熔纤", "更换尾纤", "重置OLT端口", "更换分光器", "更换网线", "路由器配置", "上门测速", "其他")
FAULT_TYPES_NEED_PHENOMENON = {"宽带修障", "IPTV修障", "电话修障", "线路维护"}
FAULT_TYPE_DEPT_MAP = {
    "宽带新装": "宽带部", "宽带修障": "宽带部", "宽带移机": "宽带部", "宽带提速": "宽带部",
    "IPTV新装": "IPTV部", "IPTV修障": "IPTV部",
    "电话新装": "电话部", "电话修障": "电话部",
    "智能组网": "宽带部", "设备更换": "设备部", "线路维护": "线路部",
}
TASK_PHOTO_ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
TASK_PHOTO_MAX_BYTES = 5 * 1024 * 1024
TASK_PHOTO_MAX_PER_TASK = 20


def _haversine_km(lat1, lng1, lat2, lng2):
    """计算两点间距离（km），使用 Haversine 公式"""
    import math
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lng / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _address_matches(address, province, city):
    """判断地址是否包含指定省市（去后缀后做包含匹配）"""
    if not address or not province:
        return False, False
    addr = address.replace("省","").replace("自治区","").replace("特别行政区","").replace("市","").replace("区","")
    prov_match = province in addr
    city_match = bool(city) and city in addr
    return prov_match, city_match


def _get_return_rate(cursor, staff_id):
    """返回 (return_cnt, assigned_cnt, rate) 最近30天"""
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM task_return_log WHERE staff_id=%s AND returned_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)",
        (staff_id,),
    )
    return_cnt = int((cursor.fetchone() or {}).get("cnt") or 0)
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM maintenance_tasks WHERE assigned_staff_id=%s AND assigned_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)",
        (staff_id,),
    )
    assigned_cnt = int((cursor.fetchone() or {}).get("cnt") or 0)
    rate = return_cnt / assigned_cnt if assigned_cnt > 0 else 0.0
    return return_cnt, assigned_cnt, rate


def _get_suspend_rate(cursor, staff_id):
    """返回 (suspend_cnt, assigned_cnt, rate) 最近30天"""
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM task_suspend_log WHERE staff_id=%s AND suspended_at >= DATE_SUB(NOW(), INTERVAL 10 SECOND)",
        (staff_id,),
    )
    suspend_cnt = int((cursor.fetchone() or {}).get("cnt") or 0)
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM maintenance_tasks WHERE assigned_staff_id=%s AND assigned_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)",
        (staff_id,),
    )
    assigned_cnt = int((cursor.fetchone() or {}).get("cnt") or 0)
    rate = suspend_cnt / assigned_cnt if assigned_cnt > 0 else 0.0
    return suspend_cnt, assigned_cnt, rate


def check_overdue_tasks():
    """定时任务：检查三类超时工单并自动转派，对原执行人记录退回惩罚"""
    connection = get_db_connection()
    if not connection:
        return
    try:
        cursor = connection.cursor()

        # 1. 截止日期超时
        cursor.execute("""
            SELECT task_id, fault_type, customer_address, assigned_staff_id
            FROM maintenance_tasks
            WHERE due_date IS NOT NULL
              AND due_date < CURDATE()
              AND status NOT IN ('已完成','已取消','已归档','已挂起')
              AND overdue_at IS NULL
        """)
        overdue_deadline = cursor.fetchall()

        # 2. SLA超时：派单后8小时未到达
        cursor.execute("""
            SELECT task_id, fault_type, customer_address, assigned_staff_id
            FROM maintenance_tasks
            WHERE assigned_at IS NOT NULL
              AND arrived_at IS NULL
              AND TIMESTAMPDIFF(HOUR, assigned_at, NOW()) >= 8
              AND status IN ('已派单','处理中')
              AND overdue_at IS NULL
        """)
        overdue_sla = cursor.fetchall()

        # 3. 挂起超时：挂起超过24小时
        cursor.execute("""
            SELECT task_id, fault_type, customer_address, assigned_staff_id
            FROM maintenance_tasks
            WHERE suspended_at IS NOT NULL
              AND TIMESTAMPDIFF(HOUR, suspended_at, NOW()) >= 24
              AND status = '已挂起'
              AND overdue_at IS NULL
        """)
        overdue_suspended = cursor.fetchall()

        all_overdue = list(overdue_deadline) + list(overdue_sla) + list(overdue_suspended)

        for task in all_overdue:
            task_id = task["task_id"]
            old_staff_id = task.get("assigned_staff_id")

            if old_staff_id:
                cursor.execute(
                    "INSERT INTO task_return_log (task_id, staff_id) VALUES (%s, %s)",
                    (task_id, old_staff_id),
                )
                cursor.execute(
                    "UPDATE maintenance_tasks SET return_count=return_count+1 WHERE task_id=%s",
                    (task_id,),
                )

            fault_type = task.get("fault_type") or ""
            address = task.get("customer_address") or ""
            new_id, new_name, _, new_tasks = _auto_assign_staff(
                cursor, fault_type, None, None,
                task_address=address,
                exclude_staff_id=old_staff_id,
            )
            if new_id:
                cursor.execute(
                    """UPDATE maintenance_tasks
                       SET assigned_staff_id=%s, assigned_at=NOW(), status='已派单',
                           arrived_at=NULL, suspended_at=NULL, suspended_by=NULL,
                           suspend_reason=NULL, overdue_at=NOW(), updated_at=NOW()
                       WHERE task_id=%s""",
                    (new_id, task_id),
                )
            else:
                cursor.execute(
                    """UPDATE maintenance_tasks
                       SET assigned_staff_id=NULL, assigned_at=NULL, status='待派单',
                           arrived_at=NULL, suspended_at=NULL, suspended_by=NULL,
                           suspend_reason=NULL, overdue_at=NOW(), updated_at=NOW()
                       WHERE task_id=%s""",
                    (task_id,),
                )

        connection.commit()
        cursor.close()
    except Exception:
        if connection:
            connection.rollback()
    finally:
        if connection and getattr(connection, "open", False):
            connection.close()


def _auto_assign_staff(cursor, fault_type, task_lat, task_lng, task_address="", exclude_staff_id=None):
    """
    智能派单：综合部门匹配(40%)、省市匹配(30%)、未完成任务数(30%)评分，返回 (staff_id, full_name, dist_km, active_tasks)。
    无合适员工时返回 (None, None, None, None)。exclude_staff_id 排除指定员工（退回场景）。
    """
    target_dept = FAULT_TYPE_DEPT_MAP.get(fault_type or "", "")
    exclude_clause = "AND s.staff_id != %s" if exclude_staff_id else ""
    params = (exclude_staff_id,) if exclude_staff_id else ()
    cursor.execute(f"""
        SELECT s.staff_id, s.full_name, s.department,
               s.location_province, s.location_city,
               COALESCE(t.active_tasks, 0) AS active_tasks
        FROM staff_basic_info s
        LEFT JOIN (
            SELECT assigned_staff_id, COUNT(*) AS active_tasks
            FROM maintenance_tasks
            WHERE status NOT IN ('已完成', '已取消')
            GROUP BY assigned_staff_id
        ) t ON s.staff_id = t.assigned_staff_id
        WHERE s.is_active = 1 {exclude_clause}
    """, params)
    rows = cursor.fetchall()
    if not rows:
        return None, None, None, None

    max_tasks = max(r["active_tasks"] for r in rows) or 1

    best, best_score = None, float("inf")
    for r in rows:
        dept_score = 0.0 if (target_dept and r["department"] == target_dept) else 1.0
        # 省市匹配：同省同市=0，同省不同市=0.5，不同省或无位置=1，员工无位置记录=0.8
        staff_prov = r.get("location_province") or ""
        staff_city = r.get("location_city") or ""
        if staff_prov:
            prov_match, city_match = _address_matches(task_address, staff_prov, staff_city)
            if prov_match and city_match:
                loc_score = 0.0
            elif prov_match:
                loc_score = 0.5
            else:
                loc_score = 1.0
        else:
            loc_score = 0.8  # 无位置记录，劣于有位置匹配的员工
        task_score = r["active_tasks"] / max_tasks
        r_cnt, _r_assigned, r_rate = _get_return_rate(cursor, r["staff_id"])
        sp_cnt, _sp_assigned, sp_rate = _get_suspend_rate(cursor, r["staff_id"])
        return_penalty = min(r_rate * 2, 1.0)
        suspend_penalty = min(sp_rate * 2, 1.0)
        penalty = (return_penalty + suspend_penalty) / 2
        score = dept_score * 0.32 + loc_score * 0.24 + task_score * 0.24 + penalty * 0.20
        if score < best_score:
            best_score = score
            best = r

    if not best:
        return None, None, None, None
    return best["staff_id"], best["full_name"], None, best["active_tasks"]


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
                "image_type": r.get("image_type") or "general",
                "created_at": _format_image_created_at(r.get("created_at")),
            }
        )
    return out


def fetch_task_image_rows(cursor, task_id):
    cursor.execute(
        """
        SELECT image_id, task_id, file_path, original_filename, image_type, created_at
        FROM task_images WHERE task_id = %s ORDER BY image_id ASC
        """,
        (task_id,),
    )
    return cursor.fetchall() or []


def count_task_images(cursor, task_id):
    cursor.execute("SELECT COUNT(*) AS c FROM task_images WHERE task_id = %s", (task_id,))
    row = cursor.fetchone() or {}
    return int(row.get("c") or 0)


def insert_task_images(cursor, task_id, files_storage_list, image_type="general"):
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
            INSERT INTO task_images (task_id, file_path, original_filename, image_type)
            VALUES (%s, %s, %s, %s)
            """,
            (task_id, rel, orig[:255] if orig else None, image_type),
        )
        inserted.append(
            {
                "image_id": cursor.lastrowid,
                "task_id": task_id,
                "file_path": rel,
                "original_filename": orig,
                "image_type": image_type,
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
        SELECT t.*, s.full_name AS assigned_staff_name,
               h.full_name AS helper_staff_name,
               settler.full_name AS settler_name
        FROM maintenance_tasks t
        LEFT JOIN staff_basic_info s ON t.assigned_staff_id = s.staff_id
        LEFT JOIN staff_basic_info h ON t.helper_staff_id = h.staff_id
        LEFT JOIN staff_basic_info settler ON t.settled_by = settler.staff_id
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
            "待回执": r.get("awaiting_receipt", 0),
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
    if request.method == "GET":
        # 清除非登录相关的残留 flash 消息
        from flask import get_flashed_messages
        get_flashed_messages()
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
                role = "admin" if role_level >= 3 else ("project_admin" if role_level == 2 else "user")
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
                role_level = 3 if role == "admin" else (2 if role == "project_admin" else 1)

            session["user_id"] = user["user_id"]
            session["username"] = user["username"]
            session["display_name"] = display_name
            session["role"] = role
            session["role_level"] = role_level
            staff_id_for_loc = user.get("staff_id")
            session["staff_id"] = staff_id_for_loc
            if staff_id_for_loc:
                try:
                    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
                    prov, ct = _ip_to_province_city(client_ip)
                    if prov or ct:
                        loc_conn = get_db_connection()
                        if loc_conn:
                            try:
                                loc_cur = loc_conn.cursor()
                                loc_cur.execute(
                                    "UPDATE staff_basic_info SET location_province=%s, location_city=%s, location_updated_at=NOW() WHERE staff_id=%s",
                                    (prov, ct, staff_id_for_loc),
                                )
                                loc_conn.commit()
                                loc_cur.close()
                            finally:
                                if loc_conn and getattr(loc_conn, "open", False):
                                    loc_conn.close()
                except Exception:
                    pass
            flash(f"欢迎回来，{display_name}", "success")
            # 检查是否有待确认的援助工单
            try:
                staff_id_val = session.get("staff_id")
                if staff_id_val:
                    _chk_conn = get_db_connection()
                    if _chk_conn:
                        try:
                            _chk_cur = _chk_conn.cursor()
                            _chk_cur.execute(
                                "SELECT COUNT(*) AS cnt FROM maintenance_tasks WHERE helper_staff_id=%s AND helper_status='pending'",
                                (staff_id_val,),
                            )
                            _cnt = int((_chk_cur.fetchone() or {}).get("cnt") or 0)
                            if _cnt > 0:
                                flash(f"您有 {_cnt} 个工单的援助申请待确认，请前往任务列表处理", "warning")
                            _chk_cur.close()
                        finally:
                            if _chk_conn and getattr(_chk_conn, "open", False):
                                _chk_conn.close()
            except Exception:
                pass
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
        ensure_task_settlements_table(connection)
        migrate_legacy_task_photos(connection)
        cursor = connection.cursor()
        task = fetch_task_for_detail(cursor, task_id)
        if not task:
            flash("未找到该工单", "danger")
            return redirect(url_for("tasks_page"))
        images = _json_task_images(cursor, task_id)
        user = get_current_user()
        current_staff_id = user.get("staff_id") if user else None
        is_assignee = current_staff_id and task.get("assigned_staff_id") == current_staff_id
        is_helper = (
            current_staff_id
            and task.get("helper_staff_id") == current_staff_id
            and task.get("helper_status") == "accepted"
        )
        can_operate = bool(is_assignee or is_helper)
        # SLA超时：派单后8小时未到达
        sla_overdue = False
        if task.get("assigned_at") and not task.get("arrived_at") and task.get("status") in ("已派单", "处理中"):
            delta = datetime.now() - task["assigned_at"]
            sla_overdue = delta.total_seconds() >= 8 * 3600
        # 供申请援助用：在职员工列表（排除自己和当前执行人）
        staff_options = []
        if is_assignee and task.get("helper_status") not in ("admin_pending", "pending", "accepted"):
            cursor.execute(
                "SELECT staff_id, full_name FROM staff_basic_info WHERE is_active=1 AND staff_id != %s ORDER BY full_name",
                (current_staff_id,),
            )
            staff_options = cursor.fetchall()
        return render_template_string(
            TASK_DETAIL_HTML,
            task=task,
            images=images,
            max_photos=TASK_PHOTO_MAX_PER_TASK,
            can_operate=can_operate,
            is_assignee=is_assignee,
            is_helper=is_helper,
            staff_options=staff_options,
            today=date.today(),
            sla_overdue=sla_overdue,
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
        task = fetch_task_for_detail(cursor, task_id)
        if not task:
            return jsonify({"ok": False, "message": "工单不存在"}), 404
        user = get_current_user()
        current_staff_id = user.get("staff_id") if user else None
        role_level = user.get("role_level", 1) if user else 1
        is_assignee = current_staff_id and task.get("assigned_staff_id") == current_staff_id
        is_helper = (
            current_staff_id
            and task.get("helper_staff_id") == current_staff_id
            and task.get("helper_status") == "accepted"
        )
        if role_level < 2 and not is_assignee and not is_helper:
            return jsonify({"ok": False, "message": "无权限上传照片"}), 403
        image_type = request.form.get("image_type", "general")
        if image_type not in ("arrival", "completion", "general"):
            image_type = "general"
        files = request.files.getlist("photos")
        if not files or not any(getattr(f, "filename", None) for f in files):
            return jsonify({"ok": False, "message": "请选择要上传的图片"}), 400
        inserted, msg = insert_task_images(cursor, task_id, files, image_type=image_type)
        if not inserted:
            return jsonify({"ok": False, "message": msg}), 400
        current_status = task.get("status", "")
        if image_type == "arrival" and current_status == "已派单":
            cursor.execute(
                "UPDATE maintenance_tasks SET status='处理中', arrived_at=NOW(), updated_at=NOW() WHERE task_id=%s",
                (task_id,),
            )
        elif image_type == "completion" and current_status == "处理中":
            cursor.execute(
                "UPDATE maintenance_tasks SET status='待回执', completed_at=NOW(), updated_at=NOW() WHERE task_id=%s",
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


def _ip_to_province_city(client_ip):
    """通过IP查询省市，返回 (province, city) 或 (None, None)"""
    import urllib.request as _urlreq
    if not client_ip:
        return None, None
    if "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    if client_ip in ("127.0.0.1", "::1") or client_ip.startswith("192.168.") or client_ip.startswith("10.") or client_ip.startswith("172."):
        return None, None

    def _strip_suffix(s, suffixes):
        for suf in suffixes:
            s = s.replace(suf, "")
        return s

    # 主：ip-api.com（免费，支持中文）
    try:
        url = f"http://ip-api.com/json/{client_ip}?lang=zh-CN&fields=status,regionName,city"
        req = _urlreq.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with _urlreq.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        if raw.lstrip().startswith("{"):
            result = json.loads(raw)
            if result.get("status") == "success":
                prov = _strip_suffix(result.get("regionName", ""), ["省", "自治区", "特别行政区", "壮族", "回族", "维吾尔"])
                city = result.get("city", "").replace("市", "")
                return prov or None, city or None
    except Exception:
        pass

    # 备用：ipapi.co
    try:
        url2 = f"https://ipapi.co/{client_ip}/json/"
        req2 = _urlreq.Request(url2, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with _urlreq.urlopen(req2, timeout=5) as resp2:
            raw2 = resp2.read().decode("utf-8", errors="replace")
        if raw2.lstrip().startswith("{"):
            result2 = json.loads(raw2)
            if not result2.get("error"):
                prov2 = _strip_suffix(result2.get("region", ""), ["省", "自治区", "特别行政区", "壮族", "回族", "维吾尔"])
                city2 = result2.get("city", "").replace("市", "")
                return prov2 or None, city2 or None
    except Exception:
        pass

    return None, None


@app.route("/api/ip_location")
def api_ip_location():
    if not is_logged_in():
        return jsonify({"ok": False, "message": "请先登录"}), 401
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
    prov, ct = _ip_to_province_city(client_ip)
    if prov or ct:
        return jsonify({"ok": True, "province": prov or "", "city": ct or ""})
    return jsonify({"ok": False, "message": "IP定位失败"})


@app.route("/api/staff/location", methods=["POST"])
def api_staff_location():
    """员工更新实时位置（GPS坐标和/或省市）"""
    if not is_logged_in():
        return jsonify({"ok": False, "message": "请先登录"}), 401
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "message": "请先登录"}), 401
    data = request.get_json(silent=True) or {}
    lat = lng = None
    if data.get("lat") is not None and data.get("lng") is not None:
        try:
            lat = float(data["lat"])
            lng = float(data["lng"])
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "坐标格式错误"}), 400
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            return jsonify({"ok": False, "message": "坐标超出范围"}), 400
    # 管理员/班长可传入任意 staff_id，否则只能更新自己绑定的
    requested_staff_id = data.get("staff_id")
    if requested_staff_id and user.get("role_level", 1) >= 2:
        target_staff_id = int(requested_staff_id)
    elif user.get("staff_id"):
        target_staff_id = int(user["staff_id"])
    else:
        return jsonify({"ok": False, "message": "当前账号未绑定员工，请联系管理员"}), 400
    connection = get_db_connection()
    if not connection:
        return jsonify({"ok": False, "message": "数据库连接失败"}), 500
    try:
        cur = connection.cursor()
        province = data.get("province", "").strip() or None
        city = data.get("city", "").strip() or None
        set_parts = ["location_updated_at=NOW()"]
        params = []
        if lat is not None:
            set_parts += ["latitude=%s", "longitude=%s"]
            params += [lat, lng]
        if province is not None:
            set_parts.append("location_province=%s")
            params.append(province)
        if city is not None:
            set_parts.append("location_city=%s")
            params.append(city)
        params.append(target_staff_id)
        cur.execute(
            f"UPDATE staff_basic_info SET {', '.join(set_parts)} WHERE staff_id=%s",
            params,
        )
        connection.commit()
        cur.close()
        return jsonify({"ok": True, "province": province, "city": city})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500
    finally:
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
            task_stats={"total": 0, "pending": 0, "assigned": 0, "in_progress": 0, "awaiting_receipt": 0, "done": 0, "archived": 0},
            my_stats=None,
            admin_help_pending=0,
            filters={"status": "", "priority": "", "keyword": ""},
            today=date.today(),
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

        # 权限1只能看待派单及派给自己的工单（含援助工单）；权限2和3可查看全部
        if get_current_role_level() == 1:
            current_staff_id = session.get("staff_id")
            if current_staff_id:
                where_parts.append("(t.assigned_staff_id = %s OR t.status = '待派单' OR (t.helper_staff_id = %s AND t.helper_status IN ('pending','accepted')))")
                params.extend([current_staff_id, current_staff_id])
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
                SUM(CASE WHEN status = '已取消' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN status = '已归档' THEN 1 ELSE 0 END) AS archived,
                SUM(CASE WHEN status = '已挂起' THEN 1 ELSE 0 END) AS suspended
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
            "archived": int(stats_raw.get("archived") or 0),
            "suspended": int(stats_raw.get("suspended") or 0),
        }

        # 权限1和2：查询个人待接取和待完成任务数
        my_stats = None
        if get_current_role_level() <= 2:
            current_staff_id = session.get("staff_id")
            cursor.execute(
                """
                SELECT
                    SUM(CASE WHEN status = '待派单' THEN 1 ELSE 0 END) AS to_accept,
                    SUM(CASE WHEN status IN ('已派单','处理中','待回执') AND assigned_staff_id = %s THEN 1 ELSE 0 END) AS to_finish,
                    SUM(CASE WHEN status = '已挂起' AND assigned_staff_id = %s THEN 1 ELSE 0 END) AS suspended
                FROM maintenance_tasks
                """,
                (current_staff_id, current_staff_id),
            )
            r = cursor.fetchone() or {}
            my_stats = {
                "to_accept": int(r.get("to_accept") or 0),
                "to_finish": int(r.get("to_finish") or 0),
                "suspended": int(r.get("suspended") or 0),
            }

        cursor.close()
        connection.close()

        admin_help_pending = 0
        if get_current_role_level() >= 2:
            _conn2 = get_db_connection()
            if _conn2:
                try:
                    _cur2 = _conn2.cursor()
                    _cur2.execute("SELECT COUNT(*) AS cnt FROM maintenance_tasks WHERE helper_status='admin_pending'")
                    admin_help_pending = int((_cur2.fetchone() or {}).get("cnt") or 0)
                    _cur2.close()
                finally:
                    if _conn2 and getattr(_conn2, "open", False):
                        _conn2.close()

        return render_template_string(
            TASKS_PAGE_HTML,
            task_list=task_list,
            task_stats=task_stats,
            my_stats=my_stats,
            admin_help_pending=admin_help_pending,
            filters={"status": status_filter, "priority": priority_filter, "keyword": keyword},
            today=date.today(),
        )
    except Exception as e:
        flash(f'获取任务数据失败: {str(e)}', 'danger')
        return render_template_string(
            TASKS_PAGE_HTML,
            task_list=[],
            task_stats={"total": 0, "pending": 0, "assigned": 0, "in_progress": 0, "awaiting_receipt": 0, "done": 0, "archived": 0},
            my_stats=None,
            admin_help_pending=0,
            filters={"status": "", "priority": "", "keyword": ""},
            today=date.today(),
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
    """幂等地为 staff_basic_info 补充扩展列"""
    cur = connection.cursor()
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'department'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN department VARCHAR(50) NULL AFTER position")
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'team_name'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN team_name VARCHAR(50) NULL AFTER team_id")
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'home_address'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN home_address VARCHAR(200) NULL AFTER department")
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'latitude'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN latitude DECIMAL(10,7) NULL AFTER home_address")
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'longitude'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN longitude DECIMAL(10,7) NULL AFTER latitude")
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'location_updated_at'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN location_updated_at DATETIME NULL AFTER longitude")
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'location_province'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN location_province VARCHAR(20) NULL AFTER location_updated_at")
    cur.execute("SHOW COLUMNS FROM staff_basic_info LIKE 'location_city'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE staff_basic_info ADD COLUMN location_city VARCHAR(20) NULL AFTER location_province")
    connection.commit()
    cur.close()


@app.route('/staff')
def staff_page():
    """员工管理（独立页面）"""
    if not is_logged_in():
        return redirect(url_for('login'))
    connection = get_db_connection()
    if not connection:
        flash('数据库连接失败', 'danger')
        return render_template_string(STAFF_PAGE_HTML, staff_list=[], return_rates={})

    try:
        ensure_staff_columns(connection)
        cursor = connection.cursor()
        user = get_current_user()
        # 普通员工只能看自己
        if user and user.get('role_level', 1) < 2:
            my_staff_id = user.get('staff_id')
            if my_staff_id:
                cursor.execute("SELECT * FROM staff_basic_info WHERE staff_id = %s", (my_staff_id,))
            else:
                cursor.execute("SELECT * FROM staff_basic_info WHERE 1=0")
        else:
            view_mode = request.args.get('view', '')
            if view_mode == 'all':
                cursor.execute("SELECT * FROM staff_basic_info ORDER BY staff_id")
            else:
                cursor.execute("SELECT * FROM staff_basic_info ORDER BY staff_id LIMIT 100")

        staff_list = cursor.fetchall()
        # 批量查询退回率和挂起率（管理员视图）
        return_rates = {}
        suspend_rates = {}
        if user and user.get('role_level', 1) >= 2:
            for s in staff_list:
                rc, ac, rt = _get_return_rate(cursor, s['staff_id'])
                return_rates[s['staff_id']] = {'cnt': rc, 'assigned': ac, 'rate': rt}
                sc, _, st = _get_suspend_rate(cursor, s['staff_id'])
                suspend_rates[s['staff_id']] = {'cnt': sc, 'rate': st}
        cursor.close()
        connection.close()
        return render_template_string(STAFF_PAGE_HTML, staff_list=staff_list, return_rates=return_rates, suspend_rates=suspend_rates)
    except Exception as e:
        flash(f'获取员工数据失败: {str(e)}', 'danger')
        return render_template_string(STAFF_PAGE_HTML, staff_list=[], return_rates={}, suspend_rates={})

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
            home_address_val = request.form.get('home_address', '').strip() or None

            # 插入数据
            cursor.execute('''
            INSERT INTO staff_basic_info
            (full_name, gender, id_card, private_phone, work_phone, emergency_contact, emergency_phone,
             education, entry_date, departure_date, is_active, region_id, team_id, team_name, position, department, home_address)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ''', (
                full_name, gender, id_card, private_phone, work_phone,
                emergency_contact, emergency_phone, education_val, entry_date,
                departure_date_val, is_active, region_id_val, team_id_val, team_name_val, position, department_val,
                home_address_val
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
def edit_staff(staff_id):
    if not is_logged_in():
        return redirect(url_for('login'))
    user = get_current_user()
    # 普通员工只能编辑自己绑定的记录
    if user and user.get('role_level', 1) < 2:
        if not user.get('staff_id') or int(user['staff_id']) != staff_id:
            flash('无权限编辑其他员工信息', 'danger')
            return redirect(url_for('staff_page'))
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
            home_address_val = request.form.get('home_address', '').strip() or None

            # 更新数据
            cursor.execute('''
            UPDATE staff_basic_info SET
                full_name = %s, gender = %s, id_card = %s, private_phone = %s,
                work_phone = %s, emergency_contact = %s, emergency_phone = %s,
                education = %s, entry_date = %s, departure_date = %s,
                is_active = %s, region_id = %s, team_id = %s, team_name = %s,
                position = %s, department = %s, home_address = %s,
                updated_at = NOW()
            WHERE staff_id = %s
            ''', (
                full_name, gender, id_card, private_phone, work_phone,
                emergency_contact, emergency_phone, education_val, entry_date,
                departure_date_val, is_active, region_id_val, team_id_val, team_name_val,
                position, department_val, home_address_val,
                staff_id
            ))
            
            connection.commit()
            flash('员工信息更新成功', 'success')
            if user and user.get('role_level', 1) >= 2:
                return redirect(url_for('staff_page'))
            return redirect(url_for('my_profile'))
            
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

@app.route('/staff/<int:staff_id>/view')
def view_staff(staff_id):
    if not is_logged_in():
        return redirect(url_for('login'))
    connection = get_db_connection()
    if not connection:
        flash('数据库连接失败', 'danger')
        return redirect(url_for('staff_page'))
    try:
        ensure_staff_columns(connection)
        ensure_maintenance_tasks_table(connection)
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM staff_basic_info WHERE staff_id = %s", (staff_id,))
        staff = cursor.fetchone()
        if not staff:
            cursor.close()
            connection.close()
            flash('未找到该员工信息', 'danger')
            return redirect(url_for('staff_page'))
        rc, ac, rt = _get_return_rate(cursor, staff_id)
        cursor.close()
        connection.close()
        return render_template_string(MY_PROFILE_HTML, staff=staff, return_rate={'cnt': rc, 'assigned': ac, 'rate': rt})
    except Exception as e:
        flash(f'获取员工信息失败: {str(e)}', 'danger')
        return redirect(url_for('staff_page'))


@app.route('/my_profile')
def my_profile():
    if not is_logged_in():
        return redirect(url_for('login'))
    user = get_current_user()
    staff_id = user.get('staff_id') if user else None
    if not staff_id:
        flash('当前账号未绑定员工信息，请联系管理员', 'warning')
        return redirect(url_for('tasks_page'))
    connection = get_db_connection()
    if not connection:
        flash('数据库连接失败', 'danger')
        return redirect(url_for('tasks_page'))
    try:
        ensure_staff_columns(connection)
        ensure_maintenance_tasks_table(connection)
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM staff_basic_info WHERE staff_id = %s", (staff_id,))
        staff = cursor.fetchone()
        if not staff:
            cursor.close()
            connection.close()
            flash('未找到员工信息，请联系管理员', 'warning')
            return redirect(url_for('tasks_page'))
        rc, ac, rt = _get_return_rate(cursor, staff_id)
        cursor.close()
        connection.close()
        return render_template_string(MY_PROFILE_HTML, staff=staff, return_rate={'cnt': rc, 'assigned': ac, 'rate': rt})
    except Exception as e:
        flash(f'获取员工信息失败: {str(e)}', 'danger')
        return redirect(url_for('tasks_page'))


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
@require_role_level(2)
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
            task_lat_raw = request.form.get("task_lat", "").strip()
            task_lng_raw = request.form.get("task_lng", "").strip()
            try:
                task_lat = float(task_lat_raw) if task_lat_raw else None
                task_lng = float(task_lng_raw) if task_lng_raw else None
            except ValueError:
                task_lat = task_lng = None

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

            source_report_id_raw = request.form.get("source_report_id", "").strip()
            reporter_user_id_raw = request.form.get("reporter_user_id", "").strip()
            try:
                source_report_id = int(source_report_id_raw) if source_report_id_raw else None
            except ValueError:
                source_report_id = None
            try:
                reporter_user_id = int(reporter_user_id_raw) if reporter_user_id_raw else None
            except ValueError:
                reporter_user_id = None

            ensure_task_images_table(connection)
            cursor.execute(
                """
                INSERT INTO maintenance_tasks
                (title, description, fault_type, customer_address, fault_phenomenon, resolution_method, priority, status, due_date, source_report_id, reporter_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, '待派单', %s, %s, %s)
                """,
                (title, description or None, fault_type or None, customer_address or None,
                 fault_phenomenon or None, resolution_method or None, priority, due_date,
                 source_report_id, reporter_user_id),
            )
            new_id = cursor.lastrowid

            # 智能自动派单
            auto_staff_id, auto_name, auto_dist, auto_tasks = _auto_assign_staff(cursor, fault_type, task_lat, task_lng, customer_address)
            if auto_staff_id:
                cursor.execute(
                    """UPDATE maintenance_tasks SET assigned_staff_id=%s, assigned_at=NOW(), status='已派单', updated_at=NOW()
                       WHERE task_id=%s""",
                    (auto_staff_id, new_id),
                )
                dist_str = f"，距离约 {auto_dist:.1f} km" if auto_dist is not None else ""
                flash(f"任务已添加，已自动派单给 {auto_name}（当前 {auto_tasks} 个任务{dist_str}）", "success")
            else:
                flash("任务已添加，暂无合适员工，请手动派单", "warning")

            # 关联上报记录：更新 issue_report 状态为处理中
            if source_report_id:
                cursor.execute(
                    "UPDATE issue_reports SET status='处理中' WHERE report_id=%s",
                    (source_report_id,)
                )

            connection.commit()
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
        resolution_methods=RESOLUTION_METHODS, fault_types_need_phenomenon=list(FAULT_TYPES_NEED_PHENOMENON),
        prefill={
            "title": request.args.get("title", ""),
            "fault_type": request.args.get("fault_type", ""),
            "customer_address": request.args.get("customer_address", ""),
            "contact_name": request.args.get("contact_name", ""),
            "contact_phone": request.args.get("contact_phone", ""),
            "source_report_id": request.args.get("source_report_id", ""),
            "reporter_user_id": request.args.get("reporter_user_id", ""),
        })


@app.route("/edit_task/<int:task_id>", methods=["GET", "POST"])
@require_role_level(2)
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
    """普通用户退回自己执行的工单，检查退回率后重新智能派单"""
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

        # 退回率检查（最近30天退回≥3次且退回率>20%则禁止）
        return_cnt, assigned_cnt, rate = _get_return_rate(cursor, current_staff_id)
        if return_cnt >= 3 and rate > 0.20:
            flash(f"您最近30天退回率为 {rate*100:.0f}%（{return_cnt}/{assigned_cnt}），已超过限制，无法退回工单", "danger")
            return redirect(url_for("tasks_page"))

        # 读取工单信息（用于重新派单）
        cursor.execute(
            "SELECT fault_type, customer_address FROM maintenance_tasks WHERE task_id=%s AND assigned_staff_id=%s AND status NOT IN ('已完成','已取消')",
            (task_id, current_staff_id),
        )
        task = cursor.fetchone()
        if not task:
            flash("退回失败：工单不存在、不属于您、或已完成/已取消", "warning")
            return redirect(url_for("tasks_page"))

        # 记录退回日志
        cursor.execute(
            "INSERT INTO task_return_log (task_id, staff_id) VALUES (%s, %s)",
            (task_id, current_staff_id),
        )

        # 更新工单：退回并记录退回人
        cursor.execute(
            """UPDATE maintenance_tasks SET
                assigned_staff_id=NULL, assigned_at=NULL, status='待派单',
                returned_by=%s, return_count=return_count+1, updated_at=NOW()
               WHERE task_id=%s""",
            (current_staff_id, task_id),
        )

        # 立即重新智能派单（排除退回人）
        fault_type = task.get("fault_type") or ""
        customer_address = task.get("customer_address") or ""
        auto_id, auto_name, _, auto_tasks = _auto_assign_staff(
            cursor, fault_type, None, None,
            task_address=customer_address,
            exclude_staff_id=current_staff_id,
        )
        if auto_id:
            cursor.execute(
                "UPDATE maintenance_tasks SET assigned_staff_id=%s, assigned_at=NOW(), status='已派单', updated_at=NOW() WHERE task_id=%s",
                (auto_id, task_id),
            )
            flash(f"工单已退回，已重新派单给 {auto_name}（当前 {auto_tasks} 个任务）", "success")
        else:
            flash("工单已退回，暂无其他合适员工，请管理员手动派单", "warning")

        connection.commit()
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


@app.route("/request_help/<int:task_id>", methods=["POST"])
def request_help(task_id):
    """执行人向其他员工发起援助申请"""
    current_staff_id = session.get("staff_id")
    if not current_staff_id:
        flash("您的账号未绑定员工信息，无法操作", "danger")
        return redirect(url_for("task_detail", task_id=task_id))

    helper_staff_id = request.form.get("helper_staff_id", "").strip()
    if not helper_staff_id:
        flash("请选择援助人员", "danger")
        return redirect(url_for("task_detail", task_id=task_id))

    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT assigned_staff_id, status, helper_status FROM maintenance_tasks WHERE task_id=%s",
            (task_id,),
        )
        task = cursor.fetchone()
        if not task:
            flash("工单不存在", "danger")
            return redirect(url_for("tasks_page"))
        if task.get("assigned_staff_id") != current_staff_id:
            flash("只有执行人才能申请援助", "danger")
            return redirect(url_for("task_detail", task_id=task_id))
        if task.get("status") in ("已完成", "已取消"):
            flash("已完成或已取消的工单无法申请援助", "danger")
            return redirect(url_for("task_detail", task_id=task_id))
        if task.get("helper_status") in ("admin_pending", "pending", "accepted"):
            flash("该工单已有援助申请，无法重复申请", "warning")
            return redirect(url_for("task_detail", task_id=task_id))
        if int(helper_staff_id) == current_staff_id:
            flash("不能邀请自己作为援助人", "danger")
            return redirect(url_for("task_detail", task_id=task_id))
        cursor.execute("SELECT full_name FROM staff_basic_info WHERE staff_id=%s AND is_active=1", (helper_staff_id,))
        helper = cursor.fetchone()
        if not helper:
            flash("所选员工不存在或已离职", "danger")
            return redirect(url_for("task_detail", task_id=task_id))
        cursor.execute(
            "UPDATE maintenance_tasks SET helper_staff_id=%s, helper_requested_at=NOW(), helper_status='admin_pending', helper_accepted_at=NULL, helper_admin_note=NULL WHERE task_id=%s",
            (helper_staff_id, task_id),
        )
        connection.commit()
        flash(f"援助申请已提交，等待管理员审核（被邀请人：{helper['full_name']}）", "success")
        return redirect(url_for("task_detail", task_id=task_id))
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f"申请援助失败: {str(e)}", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/respond_help/<int:task_id>", methods=["POST"])
def respond_help(task_id):
    """援助人接受或拒绝援助申请"""
    current_staff_id = session.get("staff_id")
    if not current_staff_id:
        flash("您的账号未绑定员工信息，无法操作", "danger")
        return redirect(url_for("tasks_page"))

    action = request.form.get("action", "").strip()
    if action not in ("accept", "reject"):
        flash("无效操作", "danger")
        return redirect(url_for("task_detail", task_id=task_id))

    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT helper_staff_id, helper_status FROM maintenance_tasks WHERE task_id=%s",
            (task_id,),
        )
        task = cursor.fetchone()
        if not task:
            flash("工单不存在", "danger")
            return redirect(url_for("tasks_page"))
        if task.get("helper_staff_id") != current_staff_id:
            flash("您不是该工单的援助邀请对象", "danger")
            return redirect(url_for("task_detail", task_id=task_id))
        if task.get("helper_status") != "pending":
            flash("该援助申请已处理", "warning")
            return redirect(url_for("task_detail", task_id=task_id))
        if action == "accept":
            cursor.execute(
                "UPDATE maintenance_tasks SET helper_status='accepted', helper_accepted_at=NOW() WHERE task_id=%s",
                (task_id,),
            )
            connection.commit()
            flash("已接受援助申请", "success")
        else:
            cursor.execute(
                "UPDATE maintenance_tasks SET helper_status='rejected', helper_staff_id=NULL, helper_requested_at=NULL WHERE task_id=%s",
                (task_id,),
            )
            connection.commit()
            flash("已拒绝援助申请", "info")
        return redirect(url_for("task_detail", task_id=task_id))
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f"操作失败: {str(e)}", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/review_help/<int:task_id>", methods=["POST"])
def review_help(task_id):
    """项目管理员/系统管理员审核援助申请（批准或拒绝）"""
    user = get_current_user()
    if not user or user.get("role_level", 1) < 2:
        flash("无权限操作，仅项目管理员及以上可审核援助申请", "danger")
        return redirect(url_for("task_detail", task_id=task_id))

    action = request.form.get("action", "").strip()
    if action not in ("approve", "reject"):
        flash("无效操作", "danger")
        return redirect(url_for("task_detail", task_id=task_id))

    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT helper_staff_id, helper_status FROM maintenance_tasks WHERE task_id=%s",
            (task_id,),
        )
        task = cursor.fetchone()
        if not task:
            flash("工单不存在", "danger")
            return redirect(url_for("tasks_page"))
        if task.get("helper_status") != "admin_pending":
            flash("该援助申请不在待审核状态", "warning")
            return redirect(url_for("task_detail", task_id=task_id))
        note = request.form.get("note", "").strip() or None
        if action == "approve":
            cursor.execute(
                "UPDATE maintenance_tasks SET helper_status='pending', helper_admin_note=%s WHERE task_id=%s",
                (note, task_id),
            )
            connection.commit()
            flash("已批准援助申请，等待援助人确认", "success")
        else:
            cursor.execute(
                "UPDATE maintenance_tasks SET helper_status='admin_rejected', helper_staff_id=NULL, helper_requested_at=NULL, helper_admin_note=%s WHERE task_id=%s",
                (note, task_id),
            )
            connection.commit()
            flash("已拒绝援助申请", "info")
        return redirect(url_for("task_detail", task_id=task_id))
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f"操作失败: {str(e)}", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/settle_task/<int:task_id>", methods=["POST"])
def settle_task(task_id):
    """项目管理员/系统管理员确认结算工单"""
    user = get_current_user()
    if not user or user.get("role_level", 1) < 2:
        flash("无权限操作", "danger")
        return redirect(url_for("task_detail", task_id=task_id))

    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    cursor = None
    try:
        ensure_maintenance_tasks_table(connection)
        ensure_task_settlements_table(connection)
        cursor = connection.cursor()
        cursor.execute(
            "SELECT assigned_staff_id, status FROM maintenance_tasks WHERE task_id=%s",
            (task_id,),
        )
        task = cursor.fetchone()
        if not task:
            flash("工单不存在", "danger")
            return redirect(url_for("tasks_page"))
        if task.get("status") != "待回执":
            flash("只有「待回执」状态的工单才能确认完成", "warning")
            return redirect(url_for("task_detail", task_id=task_id))
        notes = request.form.get("notes", "").strip()
        settled_by = user.get("staff_id") or 0
        cursor.execute(
            "UPDATE maintenance_tasks SET status='已完成', settled_at=NOW(), settled_by=%s, updated_at=NOW() WHERE task_id=%s AND status='待回执'",
            (settled_by, task_id),
        )
        if cursor.rowcount > 0:
            cursor.execute(
                "INSERT INTO task_settlements (task_id, staff_id, settled_by, notes) VALUES (%s, %s, %s, %s)",
                (task_id, task.get("assigned_staff_id") or 0, settled_by, notes or None),
            )
        connection.commit()
        flash("工单已确认完成", "success")
        return redirect(url_for("task_detail", task_id=task_id))
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f"操作失败: {str(e)}", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/suspend_task/<int:task_id>", methods=["POST"])
def suspend_task(task_id):
    user = get_current_user()
    if not user:
        flash("请先登录", "danger")
        return redirect(url_for("task_detail", task_id=task_id))

    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT assigned_staff_id, status, helper_status, helper_staff_id FROM maintenance_tasks WHERE task_id=%s",
            (task_id,),
        )
        task = cursor.fetchone()
        if not task:
            flash("工单不存在", "danger")
            return redirect(url_for("tasks_page"))

        staff_id = user.get("staff_id")
        is_assignee = task.get("assigned_staff_id") == staff_id
        is_accepted_helper = (
            task.get("helper_status") == "accepted"
            and task.get("helper_staff_id") == staff_id
        )
        can_operate = is_assignee or is_accepted_helper

        if not can_operate:
            flash("无权限操作", "danger")
            return redirect(url_for("task_detail", task_id=task_id))
        if task.get("status") != "处理中":
            flash("只有「处理中」状态的工单才能挂起", "warning")
            return redirect(url_for("task_detail", task_id=task_id))

        reason = request.form.get("reason", "").strip()
        if not reason:
            flash("挂起原因不能为空", "warning")
            return redirect(url_for("task_detail", task_id=task_id))

        # 挂起次数检查（最近30天≥3次且挂起率>20%则禁止）
        suspend_cnt, s_assigned_cnt, s_rate = _get_suspend_rate(cursor, staff_id)
        if suspend_cnt >= 3 and s_rate > 0.20:
            flash(f"您最近30天挂起率为 {s_rate*100:.0f}%（{suspend_cnt}/{s_assigned_cnt}），已超过限制，无法挂起工单", "danger")
            return redirect(url_for("task_detail", task_id=task_id))

        cursor.execute(
            "UPDATE maintenance_tasks SET status='已挂起', suspended_at=NOW(), suspended_by=%s, suspend_reason=%s, updated_at=NOW() WHERE task_id=%s AND status='处理中'",
            (staff_id, reason, task_id),
        )
        cursor.execute(
            "INSERT INTO task_suspend_log (task_id, staff_id) VALUES (%s, %s)",
            (task_id, staff_id),
        )
        connection.commit()
        flash(f"工单已挂起，原因：{reason}", "success")
        return redirect(url_for("task_detail", task_id=task_id))
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f"操作失败: {str(e)}", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/resume_task/<int:task_id>", methods=["POST"])
def resume_task(task_id):
    user = get_current_user()
    if not user:
        flash("请先登录", "danger")
        return redirect(url_for("task_detail", task_id=task_id))

    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT assigned_staff_id, status, helper_status, helper_staff_id FROM maintenance_tasks WHERE task_id=%s",
            (task_id,),
        )
        task = cursor.fetchone()
        if not task:
            flash("工单不存在", "danger")
            return redirect(url_for("tasks_page"))

        staff_id = user.get("staff_id")
        role_level = user.get("role_level", 1)
        is_assignee = task.get("assigned_staff_id") == staff_id
        is_accepted_helper = (
            task.get("helper_status") == "accepted"
            and task.get("helper_staff_id") == staff_id
        )
        can_resume = is_assignee or is_accepted_helper or role_level >= 2

        if not can_resume:
            flash("无权限操作", "danger")
            return redirect(url_for("task_detail", task_id=task_id))
        if task.get("status") != "已挂起":
            flash("只有「已挂起」状态的工单才能恢复", "warning")
            return redirect(url_for("task_detail", task_id=task_id))

        cursor.execute(
            "UPDATE maintenance_tasks SET status='处理中', updated_at=NOW() WHERE task_id=%s AND status='已挂起'",
            (task_id,),
        )
        connection.commit()
        flash("工单已恢复，继续处理中", "success")
        return redirect(url_for("task_detail", task_id=task_id))
    except Exception as e:
        if connection:
            connection.rollback()
        flash(f"操作失败: {str(e)}", "danger")
        return redirect(url_for("task_detail", task_id=task_id))
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if connection and getattr(connection, "open", False):
            connection.close()


@app.route("/issue_reports")
@require_role_level(2)
def issue_reports_admin():
    connection = get_db_connection()
    if not connection:
        flash("数据库连接失败", "danger")
        return redirect(url_for("tasks_page"))
    try:
        with connection.cursor() as cur:
            cur.execute("""
                SELECT r.*, u.real_name AS user_real_name, u.phone AS user_phone,
                       t.task_id AS linked_task_id, t.status AS linked_task_status
                FROM issue_reports r
                LEFT JOIN end_users u ON r.user_id = u.user_id
                LEFT JOIN maintenance_tasks t ON t.source_report_id = r.report_id
                ORDER BY r.created_at DESC
            """)
            raw = cur.fetchall()
            # 用关联工单的实时状态覆盖上报状态显示
            reports = []
            for row in raw:
                row = dict(row)
                if row.get("linked_task_status"):
                    row["display_status"] = row["linked_task_status"]
                else:
                    row["display_status"] = row["status"]
                reports.append(row)
    except Exception:
        reports = []
    finally:
        connection.close()
    return render_template_string(ISSUE_REPORTS_ADMIN_HTML, reports=reports)


_STAFF_SWITCH_BTN = '<a href="/user/" style="position:fixed;bottom:20px;right:20px;z-index:9999;background:#3b82f6;color:#fff;padding:8px 16px;border-radius:20px;font-size:13px;text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,0.25);">👤 用户端</a>'

ISSUE_REPORTS_ADMIN_HTML = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>用户上报列表 - 装维部门</title>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; }
        header { background: #2c3e50; color: white; padding: 15px 20px; border-radius: 5px; margin-bottom: 16px; }
        header h1 { margin: 0; font-size: 1.35rem; }
        header p { margin: 6px 0 0; font-size: 13px; opacity: 0.9; }
        .card { background: white; border-radius: 5px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .btn { display: inline-block; padding: 6px 14px; background: #3498db; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 13px; }
        .btn-success { background: #2ecc71; }
        .btn-light { background: #ecf0f1; color: #2c3e50; }
        .module-nav { display: flex; gap: 4px; margin-bottom: 20px; background: white; border-radius: 5px; padding: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.06); width: fit-content; }
        .module-nav a { padding: 10px 22px; text-decoration: none; color: #555; border-radius: 4px; font-weight: 500; font-size: 14px; }
        .module-nav a.active { background: #3498db; color: white; }
        .module-nav a:not(.active):hover { background: #ecf0f1; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #e5e7eb; font-size: 14px; vertical-align: middle; }
        th { background: #f9fafb; font-weight: 600; color: #374151; }
        .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
        .pill-pending { background: #fef3c7; color: #92400e; }
        .pill-processing { background: #dbeafe; color: #1e40af; }
        .pill-done { background: #d1fae5; color: #065f46; }
        .flash-messages { margin-bottom: 16px; padding: 10px; border-radius: 4px; }
        .alert-success { background: #dff0d8; color: #3c763d; border: 1px solid #d6e9c6; }
        .alert-danger { background: #f2dede; color: #a94442; border: 1px solid #ebccd1; }
    </style>
</head>
<body>
<div class="container">
    <header>
        <h1>装维部门管理系统</h1>
        <p>用户上报管理</p>
    </header>
    <nav class="module-nav">
        <a href="{{ url_for('tasks_page') }}">任务管理</a>
        <a href="{{ url_for('issue_reports_admin') }}" class="active">用户上报</a>
        {% if current_user and current_user.role_level >= 2 %}
        <a href="{{ url_for('staff_page') }}">员工管理</a>
        {% endif %}
        {% if current_user and current_user.role_level >= 3 %}
        <a href="{{ url_for('task_statistics') }}">数据统计</a>
        <a href="{{ url_for('account_management') }}">账号管理</a>
        {% endif %}
    </nav>
    {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}<div class="flash-messages">{% for cat, msg in messages %}<div class="alert-{{ cat }}">{{ msg }}</div>{% endfor %}</div>{% endif %}
    {% endwith %}
    <div class="card">
        <h2 style="margin:0 0 16px;font-size:18px;">用户上报列表</h2>
        {% if reports %}
        <table>
            <thead><tr>
                <th>编号</th><th>用户</th><th>联系人</th><th>业务类型</th><th>标题</th><th>上报时间</th><th>来源地</th><th>状态</th><th>操作</th>
            </tr></thead>
            <tbody>
            {% for r in reports %}
            <tr>
                <td>#{{ r.report_id }}</td>
                <td>{{ r.user_real_name or '—' }}<br><small style="color:#6b7280;">{{ r.user_phone or '' }}</small></td>
                <td>{{ r.contact_name or '—' }}<br><small style="color:#6b7280;">{{ r.contact_phone or '' }}</small></td>
                <td>{{ r.fault_type or '—' }}</td>
                <td>{{ r.title }}</td>
                <td style="white-space:nowrap;">{{ r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '—' }}</td>
                <td style="white-space:nowrap;font-size:13px;">
                    {% if r.reporter_province or r.reporter_city %}{{ r.reporter_province or '' }}{{ r.reporter_city or '' }}{% else %}—{% endif %}
                </td>
                <td>
                    {% if r.display_status == 'pending' %}<span class="pill pill-pending">待处理</span>
                    {% elif r.display_status in ('已派单', '处理中', '待回执') %}<span class="pill pill-processing">{{ r.display_status }}</span>
                    {% elif r.display_status == '已归档' %}<span class="pill" style="background:#e0e7ff;color:#3730a3;">已归档</span>
                    {% elif r.display_status in ('已完成',) %}<span class="pill pill-done">已完成</span>
                    {% else %}<span class="pill pill-pending">{{ r.display_status }}</span>{% endif %}
                </td>
                <td>
                <td>
                    {% if r.linked_task_id %}
                    <a href="{{ url_for('task_detail', task_id=r.linked_task_id) }}" style="color:#1d4ed8;text-decoration:none;font-size:13px;">工单 #{{ r.linked_task_id }}<br><small>{{ r.linked_task_status }}</small></a>
                    {% else %}
                    <a href="{{ url_for('add_task') }}?title={{ r.title|urlencode }}&fault_type={{ (r.fault_type or '')|urlencode }}&customer_address={{ (r.customer_address or '')|urlencode }}&contact_name={{ (r.contact_name or '')|urlencode }}&contact_phone={{ (r.contact_phone or '')|urlencode }}&source_report_id={{ r.report_id }}&reporter_user_id={{ r.user_id }}" class="btn btn-success">创建工单</a>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="text-align:center;color:#9ca3af;padding:24px 0;">暂无用户上报</p>
        {% endif %}
    </div>
</div>
''' + _STAFF_SWITCH_BTN + '''
</body></html>'''


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
''' + _STAFF_SWITCH_BTN + '''
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
            <a href="{{ url_for('issue_reports_admin') }}">用户上报</a>
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
                    <p class="summary-value">{{ stat.summary.awaiting_receipt or 0 }}</p>
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
        .status-已归档 { color: #3730a3; background: #e0e7ff; }
        .status-已挂起 { color: #92400e; background: #fef3c7; }
        .priority-高 { color: #cf222e; background: #ffebe9; }
        .priority-中 { color: #9a6700; background: #fff4d6; }
        .priority-低 { color: #1a7f37; background: #dafbe1; }
        .action-buttons { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
        .action-buttons form { margin: 0; }
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
            {% if current_user and current_user.role_level >= 2 %}
            <a href="{{ url_for('issue_reports_admin') }}">用户上报</a>
            <a href="{{ url_for('staff_page') }}">员工管理</a>
            {% else %}
            <a href="{{ url_for('my_profile') }}">个人信息</a>
            {% endif %}
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

        {% if my_stats %}
        <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;">
            {% if my_stats.to_accept > 0 %}
            <div style="flex:1;min-width:180px;background:#fff4d6;border:1px solid #f59e0b;border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:10px;">
                <span style="font-size:24px;">📋</span>
                <div>
                    <div style="font-size:13px;color:#92400e;">待接取任务</div>
                    <div style="font-size:22px;font-weight:700;color:#b45309;">{{ my_stats.to_accept }}</div>
                </div>
            </div>
            {% endif %}
            {% if my_stats.to_finish > 0 %}
            <div style="flex:1;min-width:180px;background:#dbeafe;border:1px solid #3b82f6;border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:10px;">
                <span style="font-size:24px;">🔧</span>
                <div>
                    <div style="font-size:13px;color:#1e40af;">待完成任务</div>
                    <div style="font-size:22px;font-weight:700;color:#1d4ed8;">{{ my_stats.to_finish }}</div>
                </div>
            </div>
            {% endif %}
            {% if my_stats.suspended > 0 %}
            <div style="flex:1;min-width:180px;background:#fef3c7;border:1px solid #f59e0b;border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:10px;">
                <span style="font-size:24px;">⏸️</span>
                <div>
                    <div style="font-size:13px;color:#92400e;">已挂起工单</div>
                    <div style="font-size:22px;font-weight:700;color:#b45309;">{{ my_stats.suspended }}</div>
                </div>
            </div>
            {% endif %}
            {% if my_stats.to_accept == 0 and my_stats.to_finish == 0 and my_stats.suspended == 0 %}
            <div style="flex:1;background:#d1fae5;border:1px solid #10b981;border-radius:8px;padding:12px 16px;display:flex;align-items:center;gap:10px;">
                <span style="font-size:24px;">✅</span>
                <div style="font-size:14px;color:#065f46;font-weight:600;">暂无待处理任务</div>
            </div>
            {% endif %}
        </div>
        {% endif %}

        {% if current_user and current_user.role_level >= 2 and admin_help_pending > 0 %}
        <div style="display:flex;align-items:center;gap:10px;background:#fef3c7;border:1px solid #fde68a;border-radius:8px;padding:12px 16px;margin-bottom:16px;">
            <span style="font-size:22px;">🆘</span>
            <div>
                <div style="font-size:13px;color:#92400e;">待审核援助申请</div>
                <div style="font-size:22px;font-weight:700;color:#b45309;">{{ admin_help_pending }}</div>
            </div>
            <div style="margin-left:auto;font-size:13px;color:#78350f;">请前往相关工单详情页审核</div>
        </div>
        {% endif %}

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
                <div class="summary-item">
                    <p class="summary-label">已归档</p>
                    <p class="summary-value">{{ task_stats.archived }}</p>
                </div>
                <div class="summary-item">
                    <p class="summary-label">已挂起</p>
                    <p class="summary-value">{{ task_stats.suspended }}</p>
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
                        <option value="已归档" {% if filters.status == '已归档' %}selected{% endif %}>已归档</option>
                        <option value="已挂起" {% if filters.status == '已挂起' %}selected{% endif %}>已挂起</option>
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
                <thead>
                <tr>
                    <th>ID</th>
                    <th>标题</th>
                    <th>优先级</th>
                    <th>截止日期</th>
                    <th>状态</th>
                    <th>执行人</th>
                    <th>派单时间</th>
                    <th>现场照片</th>
                    <th>操作</th>
                </tr>
                </thead>
                <tbody>
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
                    <td>
                        {{ t.due_date or '—' }}
                        {% if t.due_date and t.due_date < today and t.status not in ['已完成','已取消','已归档'] %}
                        <span class="pill" style="background:#ffebe9;color:#cf222e;margin-left:4px;">已超时</span>
                        {% endif %}
                    </td>
                    <td>
                        <span class="pill status-{{ t.status }}">{{ t.status }}</span>
                        {% if current_user and current_user.role_level >= 2 and t.helper_status == 'admin_pending' %}
                        <span class="pill" style="background:#fef3c7;color:#92400e;margin-left:4px;">待审核援助</span>
                        {% endif %}
                    </td>
                    <td>{{ t.assigned_staff_name or '—' }}</td>
                    <td>{{ t.assigned_at or '—' }}</td>
                    <td>
                        <a class="thumb-link" href="{{ url_for('task_detail', task_id=t.task_id) }}">
                            {% if t.photo_count %}查看（{{ t.photo_count }}）{% else %}上传/查看{% endif %}
                        </a>
                    </td>
                    <td>
                        <div class="action-buttons">
                        <a href="{{ url_for('task_detail', task_id=t.task_id) }}" class="btn btn-light">详情</a>
                        {% if current_user and current_user.role_level >= 2 %}
                        <a href="{{ url_for('edit_task', task_id=t.task_id) }}" class="btn btn-warning">编辑</a>
                        {% endif %}
                        {% if current_user and current_user.role_level >= 2 and t.status not in ['已完成', '已取消'] %}
                        <a href="{{ url_for('assign_task', task_id=t.task_id) }}" class="btn btn-primary">派单</a>
                        {% endif %}
                        {% if t.status == '待派单' and current_user and current_user.role_level < 3 %}
                        <form method="post" action="{{ url_for('claim_task', task_id=t.task_id) }}">
                            <button type="submit" class="btn btn-success" onclick="return confirm('确认接取该工单？')">接取</button>
                        </form>
                        {% endif %}
                        {% if current_user and current_user.role_level < 3 and t.assigned_staff_id == current_user.staff_id and t.status not in ['已完成', '已取消', '待派单'] %}
                        <form method="post" action="{{ url_for('return_task', task_id=t.task_id) }}">
                            <button type="submit" class="btn btn-danger" onclick="return confirm('确认退回该工单？退回后将回到待派单状态。')">退回</button>
                        </form>
                        {% endif %}
                        {% if current_user and current_user.staff_id == t.helper_staff_id and t.helper_status == 'pending' %}
                        <form method="post" action="{{ url_for('respond_help', task_id=t.task_id) }}">
                            <input type="hidden" name="action" value="accept">
                            <button type="submit" class="btn btn-success btn-sm">接受援助</button>
                        </form>
                        <form method="post" action="{{ url_for('respond_help', task_id=t.task_id) }}">
                            <input type="hidden" name="action" value="reject">
                            <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('确认拒绝？')">拒绝</button>
                        </form>
                        {% endif %}
                        </div>
                    </td>
                </tr>
                {% else %}
                <tr>
                    <td colspan="9" style="text-align: center; padding: 20px;">暂无任务，点击「添加任务」创建</td>
                </tr>
                {% endfor %}
                </tbody>
            </table>

            <!-- 手机端卡片列表 -->
            <div class="task-cards">
                {% for t in task_list %}
                <div class="task-card">
                    <div class="task-card-header">
                        <div class="task-card-title">{{ t.title }}</div>
                        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
                            <span class="pill status-{{ t.status }}">{{ t.status }}</span>
                            {% if current_user and current_user.role_level >= 3 and t.helper_status == 'admin_pending' %}
                            <span class="pill" style="background:#fef3c7;color:#92400e;">待审核援助</span>
                            {% endif %}
                        </div>
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
                        {% if current_user and current_user.staff_id == t.helper_staff_id and t.helper_status == 'pending' %}
                        <form method="post" action="{{ url_for('respond_help', task_id=t.task_id) }}" style="display:inline;">
                            <input type="hidden" name="action" value="accept">
                            <button type="submit" class="btn btn-success btn-sm">接受援助</button>
                        </form>
                        <form method="post" action="{{ url_for('respond_help', task_id=t.task_id) }}" style="display:inline;">
                            <input type="hidden" name="action" value="reject">
                            <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('确认拒绝？')">拒绝</button>
                        </form>
                        {% endif %}
                    </div>
                </div>
                {% else %}
                <p style="text-align:center;color:#9ca3af;padding:24px 0;">暂无任务</p>
                {% endfor %}
            </div>
            <div id="task-pagination" style="display:flex;justify-content:center;align-items:center;gap:6px;margin-top:16px;flex-wrap:wrap;"></div>
        </div>
    </div>
<script>
(function(){
    var PAGE_SIZE = 10;
    var currentPage = 1;
    function getRows() { return document.querySelectorAll('table.task-table tbody tr[data-row]'); }
    function getCards() { return document.querySelectorAll('.task-cards .task-card'); }
    function totalItems() { return Math.max(getRows().length, getCards().length); }
    function showPage(page) {
        currentPage = page;
        var rows = getRows(), cards = getCards();
        var total = Math.max(rows.length, cards.length);
        var start = (page - 1) * PAGE_SIZE, end = start + PAGE_SIZE;
        rows.forEach(function(r, i){ r.style.display = (i >= start && i < end) ? '' : 'none'; });
        cards.forEach(function(c, i){ c.style.display = (i >= start && i < end) ? '' : 'none'; });
        renderPager(total, page);
    }
    function renderPager(total, page) {
        var pages = Math.ceil(total / PAGE_SIZE);
        var el = document.getElementById('task-pagination');
        if (!el || pages <= 1) { if(el) el.innerHTML=''; return; }
        var html = '';
        html += '<button onclick="taskPage(' + (page-1) + ')" ' + (page<=1?'disabled':'') + ' style="padding:6px 12px;border:1px solid #d1d5db;border-radius:4px;background:white;cursor:pointer;">上一页</button>';
        for (var i = 1; i <= pages; i++) {
            html += '<button onclick="taskPage(' + i + ')" style="padding:6px 12px;border:1px solid ' + (i===page?'#3498db':'#d1d5db') + ';border-radius:4px;background:' + (i===page?'#3498db':'white') + ';color:' + (i===page?'white':'#374151') + ';cursor:pointer;">' + i + '</button>';
        }
        html += '<button onclick="taskPage(' + (page+1) + ')" ' + (page>=pages?'disabled':'') + ' style="padding:6px 12px;border:1px solid #d1d5db;border-radius:4px;background:white;cursor:pointer;">下一页</button>';
        html += '<span style="font-size:13px;color:#6b7280;">共 ' + total + ' 条</span>';
        el.innerHTML = html;
    }
    window.taskPage = function(p) {
        var pages = Math.ceil(totalItems() / PAGE_SIZE);
        if (p < 1 || p > pages) return;
        showPage(p);
    };
    // 给表格行加 data-row 标记
    document.querySelectorAll('table.task-table tbody tr').forEach(function(r, i){ r.setAttribute('data-row', i); });
    showPage(1);
})();
</script>
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
        th, td { padding: 12px 15px; text-align: left; border-bottom: 1px solid #ddd; vertical-align: middle; }
        th { background-color: #ecf0f1; }
        .action-buttons { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }
        .action-buttons form { margin: 0; }
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
            {% if current_user and current_user.role_level >= 2 %}
            <a href="{{ url_for('issue_reports_admin') }}">用户上报</a>
            <a href="{{ url_for('staff_page') }}" class="active">员工管理</a>
            {% else %}
            <a href="{{ url_for('my_profile') }}" class="active">个人信息</a>
            {% endif %}
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
                <thead>
                <tr>
                    <th>ID</th>
                    <th>姓名</th>
                    <th>岗位</th>
                    <th>所属部门</th>
                    <th>所属班组</th>
                    <th>常驻地址</th>
                    <th>位置</th>
                    <th>入职日期</th>
                    <th>在职状态</th>
                    {% if current_user and current_user.role_level >= 2 %}<th>退回率(30天)</th><th>挂起率(30天)</th>{% endif %}
                    <th>操作</th>
                </tr>
                </thead>
                <tbody>
                {% for staff in staff_list %}
                <tr>
                    <td>{{ staff.staff_id }}</td>
                    <td>{{ staff.full_name }}</td>
                    <td>{{ staff.position }}</td>
                    <td>{{ staff.department or '—' }}</td>
                    <td>{{ staff.team_name or (('班组 ' ~ staff.team_id) if staff.team_id else '—') }}</td>
                    <td>{{ staff.home_address or '—' }}</td>
                    <td>{% if staff.location_province %}📍 {{ staff.location_province }}{{ staff.location_city or '' }}{% if staff.location_updated_at %}<br><small style="color:#9ca3af;">{{ staff.location_updated_at }}</small>{% endif %}{% else %}—{% endif %}</td>
                    <td>{{ staff.entry_date }}</td>
                    <td>{{ '在职' if staff.is_active else '离职' }}</td>
                    {% if current_user and current_user.role_level >= 2 %}
                    <td>{% set rr = return_rates.get(staff.staff_id, {}) %}{% if rr.cnt %}
                        <span style="color:{% if rr.rate > 0.2 %}#dc2626{% elif rr.rate > 0.1 %}#d97706{% else %}#15803d{% endif %};">{{ (rr.rate*100)|int }}%</span>
                        <small style="color:#9ca3af;">（{{ rr.cnt }}/{{ rr.assigned }}）</small>
                    {% else %}—{% endif %}</td>
                    <td>{% set sr = suspend_rates.get(staff.staff_id, {}) %}{% if sr.cnt %}
                        <span style="color:{% if sr.rate > 0.2 %}#dc2626{% elif sr.rate > 0.1 %}#d97706{% else %}#15803d{% endif %};">{{ (sr.rate*100)|int }}%</span>
                        <small style="color:#9ca3af;">（{{ sr.cnt }}次）</small>
                    {% else %}—{% endif %}</td>
                    {% endif %}
                    <td class="action-buttons">
                        <a href="{{ url_for('view_staff', staff_id=staff.staff_id) }}" class="btn btn-primary">查看</a>
                        {% if current_user and (current_user.role_level >= 3 or current_user.staff_id == staff.staff_id) %}
                        <a href="{{ url_for('edit_staff', staff_id=staff.staff_id) }}" class="btn btn-warning">编辑</a>
                        {% endif %}
                        {% if current_user and current_user.role_level >= 3 %}
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
                </tbody>
            </table>

            <!-- 手机端员工卡片列表 -->
            <div class="staff-cards">
                {% for staff in staff_list %}
                <div class="staff-card">
                    <div class="staff-card-name">{{ staff.full_name }} · {{ staff.position }}</div>
                    <div class="staff-card-meta">
                        {% if staff.department %}部门：{{ staff.department }}<br>{% endif %}
                        班组：{{ staff.team_name or (('班组 ' ~ staff.team_id) if staff.team_id else '未分配') }}<br>
                        {% if staff.home_address %}地址：{{ staff.home_address }}<br>{% endif %}
                        {% if staff.location_province %}📍 {{ staff.location_province }}{{ staff.location_city or '' }}{% if staff.location_updated_at %}（{{ staff.location_updated_at }}）{% endif %}<br>{% endif %}
                        入职：{{ staff.entry_date }} · {{ '在职' if staff.is_active else '离职' }}
                        {% if current_user and current_user.role_level >= 2 %}{% set rr = return_rates.get(staff.staff_id, {}) %}{% if rr.cnt %}<br>退回率：<span style="color:{% if rr.rate > 0.2 %}#dc2626{% elif rr.rate > 0.1 %}#d97706{% else %}#15803d{% endif %};">{{ (rr.rate*100)|int }}%</span>（{{ rr.cnt }}/{{ rr.assigned }}）{% endif %}{% endif %}
                    </div>
                    <div class="staff-card-actions">
                        {% if current_user and (current_user.role_level >= 3 or current_user.staff_id == staff.staff_id) %}
                        <a href="{{ url_for('edit_staff', staff_id=staff.staff_id) }}" class="btn btn-warning">编辑</a>
                        {% endif %}
                        {% if current_user and current_user.role_level >= 3 %}
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
            <div id="staff-pagination" style="display:flex;justify-content:center;align-items:center;gap:6px;margin-top:16px;flex-wrap:wrap;"></div>
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
<script>
(function(){
    var PAGE_SIZE = 10;
    var currentPage = 1;
    function getRows() { return document.querySelectorAll('table.staff-table tbody tr[data-row]'); }
    function getCards() { return document.querySelectorAll('.staff-cards .staff-card'); }
    function totalItems() { return Math.max(getRows().length, getCards().length); }
    function showPage(page) {
        currentPage = page;
        var rows = getRows(), cards = getCards();
        var total = Math.max(rows.length, cards.length);
        var start = (page - 1) * PAGE_SIZE, end = start + PAGE_SIZE;
        rows.forEach(function(r, i){ r.style.display = (i >= start && i < end) ? '' : 'none'; });
        cards.forEach(function(c, i){ c.style.display = (i >= start && i < end) ? '' : 'none'; });
        renderPager(total, page);
    }
    function renderPager(total, page) {
        var pages = Math.ceil(total / PAGE_SIZE);
        var el = document.getElementById('staff-pagination');
        if (!el || pages <= 1) { if(el) el.innerHTML=''; return; }
        var html = '';
        html += '<button onclick="staffPage(' + (page-1) + ')" ' + (page<=1?'disabled':'') + ' style="padding:6px 12px;border:1px solid #d1d5db;border-radius:4px;background:white;cursor:pointer;">上一页</button>';
        for (var i = 1; i <= pages; i++) {
            html += '<button onclick="staffPage(' + i + ')" style="padding:6px 12px;border:1px solid ' + (i===page?'#3498db':'#d1d5db') + ';border-radius:4px;background:' + (i===page?'#3498db':'white') + ';color:' + (i===page?'white':'#374151') + ';cursor:pointer;">' + i + '</button>';
        }
        html += '<button onclick="staffPage(' + (page+1) + ')" ' + (page>=pages?'disabled':'') + ' style="padding:6px 12px;border:1px solid #d1d5db;border-radius:4px;background:white;cursor:pointer;">下一页</button>';
        html += '<span style="font-size:13px;color:#6b7280;">共 ' + total + ' 条</span>';
        el.innerHTML = html;
    }
    window.staffPage = function(p) {
        var pages = Math.ceil(totalItems() / PAGE_SIZE);
        if (p < 1 || p > pages) return;
        showPage(p);
    };
    document.querySelectorAll('table.staff-table tbody tr').forEach(function(r, i){ r.setAttribute('data-row', i); });
    showPage(1);
})();
</script>
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
            <a href="{{ url_for('issue_reports_admin') }}">用户上报</a>
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
                                <option value="2" {% if u.role_level == 2 %}selected{% endif %}>2-项目管理员</option>
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
                                <option value="2" {% if u.role_level == 2 %}selected{% endif %}>2-项目管理员</option>
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
                    <select id="department" name="department">
                        <option value="">请选择</option>
                        <option value="宽带部">宽带部</option>
                        <option value="IPTV部">IPTV部</option>
                        <option value="电话部">电话部</option>
                        <option value="设备部">设备部</option>
                        <option value="线路部">线路部</option>
                    </select>
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
                    <label for="home_address">常驻地址</label>
                    <input type="text" id="home_address" name="home_address" maxlength="200" placeholder="如：XX市XX区XX路XX号">
                </div>
                <div class="form-group">
                    <label for="position">岗位 <span class="required-mark">*</span></label>
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

MY_PROFILE_HTML = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>个人信息 - 装维部门</title>
    <style>
        body { font-family: "Microsoft YaHei", sans-serif; margin: 0; padding: 20px; background-color: #f5f5f5; }
        .container { max-width: 700px; margin: 0 auto; }
        header { background-color: #2c3e50; color: white; padding: 15px 20px; border-radius: 5px; margin-bottom: 16px; }
        header h1 { margin: 0; font-size: 1.35rem; }
        .module-nav { display: flex; gap: 4px; margin-bottom: 20px; background: white; border-radius: 5px; padding: 5px; box-shadow: 0 2px 5px rgba(0,0,0,0.06); width: fit-content; }
        .module-nav a { padding: 10px 22px; text-decoration: none; color: #555; border-radius: 4px; font-weight: 500; font-size: 14px; }
        .module-nav a.active { background: #3498db; color: white; }
        .module-nav a:not(.active):hover { background: #ecf0f1; color: #2c3e50; }
        .card { background: white; border-radius: 5px; padding: 24px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); }
        .card h2 { margin: 0 0 20px 0; font-size: 1.1rem; color: #2c3e50; }
        .info-row { display: flex; padding: 10px 0; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
        .info-row:last-child { border-bottom: none; }
        .info-label { width: 110px; color: #6b7280; flex-shrink: 0; }
        .info-value { color: #1f2937; flex: 1; }
        .btn { display: inline-block; padding: 9px 20px; background: #3498db; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; font-size: 14px; margin-top: 20px; }
        .btn-warning { background: #f39c12; }
        .flash-messages { margin-bottom: 16px; }
        .alert-success { background: #dff0d8; color: #3c763d; border: 1px solid #d6e9c6; padding: 10px; border-radius: 4px; }
        .alert-warning { background: #fcf8e3; color: #8a6d3b; border: 1px solid #faebcc; padding: 10px; border-radius: 4px; }
        .alert-danger { background: #f2dede; color: #a94442; border: 1px solid #ebccd1; padding: 10px; border-radius: 4px; }
        @media (max-width: 700px) { body { padding: 10px; } .module-nav { width: 100%; overflow-x: auto; } .module-nav a { padding: 8px 14px; font-size: 13px; white-space: nowrap; } }
    </style>
</head>
<body>
    <div class="container">
        <header><h1>装维部门管理系统</h1></header>
        <nav class="module-nav" aria-label="模块切换">
            <a href="{{ url_for('tasks_page') }}">任务管理</a>
            <a href="{{ url_for('my_profile') }}" class="active">个人信息</a>
        </nav>
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}<div class="flash-messages">{% for category, message in messages %}<div class="alert-{{ category }}">{{ message }}</div>{% endfor %}</div>{% endif %}
        {% endwith %}
        <div class="card">
            <h2>我的信息</h2>
            <div class="info-row"><span class="info-label">姓名</span><span class="info-value">{{ staff.full_name }}</span></div>
            <div class="info-row"><span class="info-label">性别</span><span class="info-value">{{ staff.gender or '—' }}</span></div>
            <div class="info-row"><span class="info-label">岗位</span><span class="info-value">{{ staff.position or '—' }}</span></div>
            <div class="info-row"><span class="info-label">所属部门</span><span class="info-value">{{ staff.department or '—' }}</span></div>
            <div class="info-row"><span class="info-label">所属班组</span><span class="info-value">{{ staff.team_name or '—' }}</span></div>
            <div class="info-row"><span class="info-label">工作电话</span><span class="info-value">{{ staff.work_phone or '—' }}</span></div>
            <div class="info-row"><span class="info-label">常驻地址</span><span class="info-value">{{ staff.home_address or '—' }}</span></div>
            <div class="info-row"><span class="info-label">入职日期</span><span class="info-value">{{ staff.entry_date or '—' }}</span></div>
            <div class="info-row"><span class="info-label">在职状态</span><span class="info-value">{{ '在职' if staff.is_active else '离职' }}</span></div>
            <div class="info-row"><span class="info-label">所在位置</span><span class="info-value">{% if staff.location_province %}📍 {{ staff.location_province }}{{ staff.location_city or '' }}{% if staff.location_updated_at %}（{{ staff.location_updated_at }}）{% endif %}{% else %}未记录{% endif %}</span></div>
            <div class="info-row"><span class="info-label">退回率(30天)</span><span class="info-value">
                {% if return_rate and return_rate.cnt %}
                <span style="color:{% if return_rate.rate > 0.2 %}#dc2626{% elif return_rate.rate > 0.1 %}#d97706{% else %}#15803d{% endif %};">{{ (return_rate.rate*100)|int }}%</span>
                （退回 {{ return_rate.cnt }} 次 / 派单 {{ return_rate.assigned }} 次）
                {% if return_rate.cnt >= 3 and return_rate.rate > 0.2 %}
                <span style="color:#dc2626;font-weight:600;">⚠ 已超限，无法退回工单</span>
                {% endif %}
                {% else %}—{% endif %}
            </span></div>
            {% if current_user and (current_user.role_level >= 3 or current_user.staff_id == staff.staff_id) %}
            <a href="{{ url_for('edit_staff', staff_id=staff.staff_id) }}" class="btn btn-warning">编辑信息</a>
            {% endif %}
            {% if current_user and current_user.role_level >= 2 %}
            <a href="{{ url_for('staff_page') }}" class="btn" style="background:#6b7280;">返回员工列表</a>
            {% endif %}
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

        {% if current_user and current_user.role_level >= 2 %}
        <nav class="module-nav" aria-label="模块切换">
            <a href="{{ url_for('tasks_page') }}">任务管理</a>
            <a href="{{ url_for('staff_page') }}" class="active">员工管理</a>
            {% if current_user.role_level >= 3 %}
            <a href="{{ url_for('issue_reports_admin') }}">用户上报</a>
            <a href="{{ url_for('task_statistics') }}">数据统计</a>
            <a href="{{ url_for('account_management') }}">账号管理</a>
            {% endif %}
        </nav>
        {% endif %}

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
                    <input type="text" id="full_name" name="full_name" value="{{ staff.full_name }}" required maxlength="30" placeholder="真实姓名">
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
                    <label for="department">所属部门</label>
                    <select id="department" name="department">
                        <option value="">请选择</option>
                        <option value="宽带部" {% if staff.department == '宽带部' %}selected{% endif %}>宽带部</option>
                        <option value="IPTV部" {% if staff.department == 'IPTV部' %}selected{% endif %}>IPTV部</option>
                        <option value="电话部" {% if staff.department == '电话部' %}selected{% endif %}>电话部</option>
                        <option value="设备部" {% if staff.department == '设备部' %}selected{% endif %}>设备部</option>
                        <option value="线路部" {% if staff.department == '线路部' %}selected{% endif %}>线路部</option>
                    </select>
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
                    <label for="home_address">常驻地址</label>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;">
                        <select id="province_sel" onchange="updateCities()" style="flex:1;min-width:120px;">
                            <option value="">选择省份</option>
                        </select>
                        <select id="city_sel" onchange="updateHomeAddress()" style="flex:1;min-width:120px;">
                            <option value="">选择城市</option>
                        </select>
                    </div>
                    <input type="hidden" id="home_address" name="home_address" value="{{ staff.home_address or '' }}">
                    <small id="home_address_hint" style="color:#6b7280;font-size:12px;">当前：{{ staff.home_address or '未设置' }}</small>
                </div>
                <div class="form-group">
                    <label>实时位置（省市）</label>
                    <p style="margin:4px 0;font-size:13px;color:#6b7280;">
                        {% if staff.location_province %}
                        📍 {{ staff.location_province }}{{ staff.location_city or '' }}{% if staff.location_updated_at %}（{{ staff.location_updated_at }}）{% endif %}
                        {% else %}
                        暂无位置记录（登录时自动更新）
                        {% endif %}
                    </p>
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
                    <a href="{{ url_for('staff_page') if current_user and current_user.role_level >= 2 else url_for('my_profile') }}" class="btn btn-danger">取消</a>
                </div>
            </form>
        </div>
    </div>
<script>
var PC={
"北京":["东城","西城","朝阳","丰台","石景山","海淀","门头沟","房山","通州","顺义","昌平","大兴","怀柔","平谷","密云","延庆"],
"天津":["和平","河东","河西","南开","河北","红桥","东丽","西青","津南","北辰","武清","宝坻","滨海新区","宁河","静海","蓟州"],
"河北":["石家庄","唐山","秦皇岛","邯郸","邢台","保定","张家口","承德","沧州","廊坊","衡水"],
"山西":["太原","大同","阳泉","长治","晋城","朔州","晋中","运城","忻州","临汾","吕梁"],
"内蒙古":["呼和浩特","包头","乌海","赤峰","通辽","鄂尔多斯","呼伦贝尔","巴彦淖尔","乌兰察布","兴安盟","锡林郭勒盟","阿拉善盟"],
"辽宁":["沈阳","大连","鞍山","抚顺","本溪","丹东","锦州","营口","阜新","辽阳","盘锦","铁岭","朝阳","葫芦岛"],
"吉林":["长春","吉林","四平","辽源","通化","白山","松原","白城","延边"],
"黑龙江":["哈尔滨","齐齐哈尔","鸡西","鹤岗","双鸭山","大庆","伊春","佳木斯","七台河","牡丹江","黑河","绥化","大兴安岭"],
"上海":["黄浦","徐汇","长宁","静安","普陀","虹口","杨浦","闵行","宝山","嘉定","浦东新区","金山","松江","青浦","奉贤","崇明"],
"江苏":["南京","无锡","徐州","常州","苏州","南通","连云港","淮安","盐城","扬州","镇江","泰州","宿迁"],
"浙江":["杭州","宁波","温州","嘉兴","湖州","绍兴","金华","衢州","舟山","台州","丽水"],
"安徽":["合肥","芜湖","蚌埠","淮南","马鞍山","淮北","铜陵","安庆","黄山","滁州","阜阳","宿州","六安","亳州","池州","宣城"],
"福建":["福州","厦门","莆田","三明","泉州","漳州","南平","龙岩","宁德"],
"江西":["南昌","景德镇","萍乡","九江","新余","鹰潭","赣州","吉安","宜春","抚州","上饶"],
"山东":["济南","青岛","淄博","枣庄","东营","烟台","潍坊","济宁","泰安","威海","日照","临沂","德州","聊城","滨州","菏泽"],
"河南":["郑州","开封","洛阳","平顶山","安阳","鹤壁","新乡","焦作","濮阳","许昌","漯河","三门峡","南阳","商丘","信阳","周口","驻马店"],
"湖北":["武汉","黄石","十堰","宜昌","襄阳","鄂州","荆门","孝感","荆州","黄冈","咸宁","随州","恩施","仙桃","潜江","天门","神农架"],
"湖南":["长沙","株洲","湘潭","衡阳","邵阳","岳阳","常德","张家界","益阳","郴州","永州","怀化","娄底","湘西"],
"广东":["广州","深圳","珠海","汕头","佛山","韶关","湛江","肇庆","江门","茂名","惠州","梅州","汕尾","河源","阳江","清远","东莞","中山","潮州","揭州","云浮"],
"广西":["南宁","柳州","桂林","梧州","北海","防城港","钦州","贵港","玉林","百色","贺州","河池","来宾","崇左"],
"海南":["海口","三亚","三沙","儋州","五指山","琼海","文昌","万宁","东方","定安","屯昌","澄迈","临高","白沙","昌江","乐东","陵水","保亭","琼中"],
"重庆":["万州","涪陵","渝中","大渡口","江北","沙坪坝","九龙坡","南岸","北碚","綦江","大足","渝北","巴南","黔江","长寿","江津","合川","永川","南川","璧山","铜梁","潼南","荣昌","开州","梁平","武隆","城口","丰都","垫江","忠县","云阳","奉节","巫山","巫溪","石柱","秀山","酉阳","彭水"],
"四川":["成都","自贡","攀枝花","泸州","德阳","绵阳","广元","遂宁","内江","乐山","南充","眉山","宜宾","广安","达州","雅安","巴中","资阳","阿坝","甘孜","凉山"],
"贵州":["贵阳","六盘水","遵义","安顺","毕节","铜仁","黔西南","黔东南","黔南"],
"云南":["昆明","曲靖","玉溪","保山","昭通","丽江","普洱","临沧","楚雄","红河","文山","西双版纳","大理","德宏","怒江","迪庆"],
"西藏":["拉萨","日喀则","昌都","林芝","山南","那曲","阿里"],
"陕西":["西安","铜川","宝鸡","咸阳","渭南","延安","汉中","榆林","安康","商洛"],
"甘肃":["兰州","嘉峪关","金昌","白银","天水","武威","张掖","平凉","酒泉","庆阳","定西","陇南","临夏","甘南"],
"青海":["西宁","海东","海北","黄南","海南","果洛","玉树","海西"],
"宁夏":["银川","石嘴山","吴忠","固原","中卫"],
"新疆":["乌鲁木齐","克拉玛依","吐鲁番","哈密","昌吉","博尔塔拉","巴音郭楞","阿克苏","克孜勒苏","喀什","和田","伊犁","塔城","阿勒泰","石河子","阿拉尔","图木舒克","五家渠","北屯","铁门关","双河","可克达拉","昆玉","胡杨河"]
};

function updateCities() {
    var pSel = document.getElementById('province_sel');
    var cSel = document.getElementById('city_sel');
    var prov = pSel.value;
    cSel.innerHTML = '<option value="">选择城市</option>';
    if (prov && PC[prov]) {
        PC[prov].forEach(function(c) {
            var o = document.createElement('option');
            o.value = c; o.textContent = c;
            cSel.appendChild(o);
        });
    }
    updateHomeAddress();
}
function updateHomeAddress() {
    var prov = document.getElementById('province_sel').value;
    var city = document.getElementById('city_sel').value;
    var val = prov ? (city ? prov + city : prov) : '';
    document.getElementById('home_address').value = val;
    document.getElementById('home_address_hint').textContent = '当前：' + (val || '未设置');
}
function initProvCity() {
    var pSel = document.getElementById('province_sel');
    Object.keys(PC).forEach(function(p) {
        var o = document.createElement('option');
        o.value = p; o.textContent = p;
        pSel.appendChild(o);
    });
    var saved = document.getElementById('home_address').value || '';
    if (!saved) return;
    var matchedProv = '';
    Object.keys(PC).forEach(function(p) {
        if (saved.startsWith(p)) matchedProv = p;
    });
    if (!matchedProv) return;
    pSel.value = matchedProv;
    updateCities();
    var city = saved.slice(matchedProv.length);
    if (city) document.getElementById('city_sel').value = city;
}
document.addEventListener('DOMContentLoaded', initProvCity);
</script>

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
        .flash-messages { margin-bottom: 16px; }
        .alert-success { background: #dff0d8; color: #3c763d; border: 1px solid #d6e9c6; padding: 10px 14px; border-radius: 4px; margin-bottom: 8px; }
        .alert-danger  { background: #f2dede; color: #a94442; border: 1px solid #ebccd1; padding: 10px 14px; border-radius: 4px; margin-bottom: 8px; }
        .alert-warning { background: #fcf8e3; color: #8a6d3b; border: 1px solid #faebcc; padding: 10px 14px; border-radius: 4px; margin-bottom: 8px; }
        .alert-info    { background: #d9edf7; color: #31708f; border: 1px solid #bce8f1; padding: 10px 14px; border-radius: 4px; margin-bottom: 8px; }
        .helper-box { background: #f0f7ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 14px 16px; margin-top: 14px; }
        .helper-box h3 { margin: 0 0 10px 0; font-size: 14px; color: #1e40af; }
        .flow-section { border-radius: 6px; padding: 14px 16px; margin-bottom: 12px; }
        .flow-arrival { background: #eff6ff; border: 1px solid #bfdbfe; }
        .flow-arrival h3 { margin: 0 0 8px 0; font-size: 14px; color: #1d4ed8; }
        .flow-completion { background: #f0fdf4; border: 1px solid #bbf7d0; }
        .flow-completion h3 { margin: 0 0 8px 0; font-size: 14px; color: #15803d; }
        .flow-settle { background: #fefce8; border: 1px solid #fde68a; }
        .flow-settle h3 { margin: 0 0 8px 0; font-size: 14px; color: #92400e; }
        .gallery-section-label { font-size: 12px; color: #6b7280; font-weight: 600; margin: 8px 0 4px 0; }
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
        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            <div class="flash-messages">
              {% for category, message in messages %}
                <div class="alert-{{ category }}">{{ message }}</div>
              {% endfor %}
            </div>
          {% endif %}
        {% endwith %}
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
                <div class="meta-item"><label>截止日期</label><span>{{ task.due_date or '—' }}{% if task.due_date and task.due_date < today and task.status not in ['已完成','已取消','已归档'] %} <span class="pill" style="background:#ffebe9;color:#cf222e;">已超时</span>{% endif %}</span></div>
                <div class="meta-item"><label>执行人</label><span>{{ task.assigned_staff_name or '—' }}</span></div>
                <div class="meta-item"><label>派单时间</label><span>{{ task.assigned_at or '—' }}{% if task.assigned_at and not task.arrived_at and task.status in ['已派单','处理中'] and current_user and current_user.role_level >= 2 and sla_overdue %} <span class="pill" style="background:#fff4d6;color:#92400e;">未到达超时</span>{% endif %}</span></div>
                <div class="meta-item"><label>创建/更新</label><span>{{ task.created_at or '—' }} / {{ task.updated_at or '—' }}</span></div>
            </div>
            {% if task.description %}
            <div class="desc-block">{{ task.description }}</div>
            {% endif %}

            {% if task.source_report_id %}
            <div style="margin:10px 0 0;padding:8px 12px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;font-size:13px;color:#1e40af;">
                来源：用户上报 #{{ task.source_report_id }}
            </div>
            {% endif %}
            <div class="helper-box">
                <h3>工单援助</h3>
                {% set hs = task.helper_status %}
                {% set helper_name = task.helper_staff_name or '—' %}

                {% if hs == 'accepted' %}
                    {# 已接受 #}
                    <p style="margin:0 0 4px 0;font-size:14px;">援助人：<strong>{{ helper_name }}</strong> <span style="color:#15803d;">（已接受）</span></p>
                    {% if is_helper %}<p style="margin:4px 0 0 0;font-size:13px;color:#15803d;">您是本工单的援助人。</p>{% endif %}

                {% elif hs == 'pending' %}
                    {# 管理员已批准，等待援助人确认 #}
                    {% if is_assignee %}
                        <p style="margin:0;font-size:14px;color:#92400e;">管理员已批准，等待 <strong>{{ helper_name }}</strong> 确认援助申请…</p>
                    {% elif current_user and current_user.staff_id == task.helper_staff_id %}
                        <p style="margin:0 0 10px 0;font-size:14px;"><strong>{{ task.assigned_staff_name or '执行人' }}</strong> 邀请您协助处理此工单，请确认：</p>
                        <div style="display:flex;gap:8px;flex-wrap:wrap;">
                        <form method="post" action="{{ url_for('respond_help', task_id=task.task_id) }}">
                            <input type="hidden" name="action" value="accept">
                            <button type="submit" class="btn btn-success btn-sm">接受援助</button>
                        </form>
                        <form method="post" action="{{ url_for('respond_help', task_id=task.task_id) }}">
                            <input type="hidden" name="action" value="reject">
                            <button type="submit" class="btn btn-danger btn-sm" onclick="return confirm('确认拒绝援助申请？')">拒绝</button>
                        </form>
                        </div>
                    {% else %}
                        <p style="margin:0;font-size:14px;color:#6b7280;">援助申请已批准，等待援助人确认中。</p>
                    {% endif %}

                {% elif hs == 'admin_pending' %}
                    {# 等待管理员审核 #}
                    {% if is_assignee %}
                        <p style="margin:0 0 6px 0;font-size:14px;color:#92400e;">援助申请已提交，等待管理员审核（被邀请人：<strong>{{ helper_name }}</strong>）</p>
                    {% endif %}
                    {% if current_user and current_user.role_level >= 2 %}
                        <p style="margin:0 0 8px 0;font-size:13px;color:#374151;">执行人 <strong>{{ task.assigned_staff_name or '—' }}</strong> 申请邀请 <strong>{{ helper_name }}</strong> 援助此工单，请审核：</p>
                        <form method="post" action="{{ url_for('review_help', task_id=task.task_id) }}" style="display:flex;flex-direction:column;gap:8px;max-width:400px;">
                            <textarea name="note" placeholder="审核备注（可选）" style="padding:6px 8px;border:1px solid #d1d5db;border-radius:4px;font-size:13px;min-height:50px;resize:vertical;"></textarea>
                            <div style="display:flex;gap:8px;">
                                <button type="submit" name="action" value="approve" class="btn btn-success btn-sm">批准</button>
                                <button type="submit" name="action" value="reject" class="btn btn-danger btn-sm" onclick="return confirm('确认拒绝该援助申请？')">拒绝</button>
                            </div>
                        </form>
                    {% elif not is_assignee %}
                        <p style="margin:0;font-size:14px;color:#6b7280;">援助申请审核中。</p>
                    {% endif %}

                {% elif hs == 'admin_rejected' %}
                    {# 管理员拒绝 #}
                    <p style="margin:0 0 6px 0;font-size:14px;color:#b91c1c;">管理员已拒绝援助申请。{% if task.helper_admin_note %} 备注：{{ task.helper_admin_note }}{% endif %}</p>
                    {% if is_assignee and task.status not in ['已完成', '已取消'] %}
                    <p style="margin:4px 0 8px 0;font-size:13px;color:#6b7280;">可重新发起援助申请：</p>
                    <form method="post" action="{{ url_for('request_help', task_id=task.task_id) }}" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                        <select name="helper_staff_id" required style="padding:6px 10px;border:1px solid #d1d5db;border-radius:4px;font-size:14px;">
                            <option value="">— 选择援助人员 —</option>
                            {% for s in staff_options %}<option value="{{ s.staff_id }}">{{ s.full_name }}</option>{% endfor %}
                        </select>
                        <button type="submit" class="btn btn-primary btn-sm">重新申请援助</button>
                    </form>
                    {% endif %}

                {% elif hs == 'rejected' %}
                    {# 援助人拒绝 #}
                    <p style="margin:0 0 6px 0;font-size:14px;color:#b91c1c;">援助人已拒绝。</p>
                    {% if is_assignee and task.status not in ['已完成', '已取消'] %}
                    <p style="margin:4px 0 8px 0;font-size:13px;color:#6b7280;">可重新发起援助申请：</p>
                    <form method="post" action="{{ url_for('request_help', task_id=task.task_id) }}" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                        <select name="helper_staff_id" required style="padding:6px 10px;border:1px solid #d1d5db;border-radius:4px;font-size:14px;">
                            <option value="">— 选择援助人员 —</option>
                            {% for s in staff_options %}<option value="{{ s.staff_id }}">{{ s.full_name }}</option>{% endfor %}
                        </select>
                        <button type="submit" class="btn btn-primary btn-sm">重新申请援助</button>
                    </form>
                    {% endif %}

                {% elif is_assignee and task.status not in ['已完成', '已取消'] %}
                    {# 无援助，执行人可申请 #}
                    <p style="margin:0 0 8px 0;font-size:14px;color:#6b7280;">暂无援助人，如需帮助可申请援助（需管理员审核）。</p>
                    <form method="post" action="{{ url_for('request_help', task_id=task.task_id) }}" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                        <select name="helper_staff_id" required style="padding:6px 10px;border:1px solid #d1d5db;border-radius:4px;font-size:14px;">
                            <option value="">— 选择援助人员 —</option>
                            {% for s in staff_options %}<option value="{{ s.staff_id }}">{{ s.full_name }}</option>{% endfor %}
                        </select>
                        <button type="submit" class="btn btn-primary btn-sm">发送援助申请</button>
                    </form>

                {% else %}
                    <p style="margin:0;font-size:14px;color:#6b7280;">暂无援助人</p>
                {% endif %}
            </div>
        </div>

        <div class="card">
            <h2 style="margin-top:0;">工单进度</h2>

            <!-- 已派单：工程师确认到达 -->
            {% if task.status == '已派单' and can_operate %}
            <div class="flow-section flow-arrival">
                <h3>确认到达现场</h3>
                <p style="color:#6b7280;font-size:13px;margin:0 0 10px 0;">请上传到达现场的照片作为凭证，上传后工单将进入「处理中」状态。</p>
                <input type="file" id="arrivalInput" accept="image/jpeg,image/png,image/gif,image/webp" multiple>
                <div style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;">
                    <button type="button" id="btnArrival" class="btn btn-primary">确认到达并上传照片</button>
                </div>
                <div id="arrivalStatus" class="upload-status"></div>
            </div>
            {% endif %}

            <!-- 处理中：工程师确认完成 -->
            {% if task.status == '处理中' and can_operate %}
            {% if task.arrived_at %}
            <p style="font-size:13px;color:#6b7280;margin:0 0 12px 0;">到达时间：{{ task.arrived_at }}</p>
            {% endif %}
            <div class="flow-section flow-completion">
                <h3>确认完成任务</h3>
                <p style="color:#6b7280;font-size:13px;margin:0 0 10px 0;">请上传完成任务的凭证照片，上传后工单将进入「待回执」状态等待管理员确认完成。</p>
                <input type="file" id="completionInput" accept="image/jpeg,image/png,image/gif,image/webp" multiple>
                <div style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;">
                    <button type="button" id="btnCompletion" class="btn btn-success">确认完成并上传凭证</button>
                </div>
                <div id="completionStatus" class="upload-status"></div>
            </div>
            <div class="flow-section" style="border-color:#f59e0b;margin-top:12px;">
                <h3 style="color:#92400e;">挂起工单</h3>
                <p style="color:#6b7280;font-size:13px;margin:0 0 10px 0;">如遇不可抗力或需等待条件，可挂起工单并填写原因。</p>
                <form method="post" action="{{ url_for('suspend_task', task_id=task.task_id) }}">
                    <textarea name="reason" placeholder="挂起原因（必填）" required style="width:100%;box-sizing:border-box;padding:8px;border:1px solid #d1d5db;border-radius:4px;font-size:13px;min-height:60px;resize:vertical;margin-bottom:10px;"></textarea>
                    <button type="submit" class="btn" style="background:#f59e0b;color:#fff;" onclick="return confirm('确认挂起该工单？')">挂起工单</button>
                </form>
            </div>
            {% endif %}

            <!-- 已挂起：显示挂起信息 + 恢复按钮 -->
            {% if task.status == '已挂起' %}
            <div class="flow-section" style="border-color:#f59e0b;">
                <h3 style="color:#92400e;">工单已挂起</h3>
                {% if task.suspended_at %}<p style="font-size:13px;color:#6b7280;margin:0 0 4px 0;">挂起时间：{{ task.suspended_at }}</p>{% endif %}
                {% if task.suspend_reason %}<p style="font-size:13px;color:#6b7280;margin:0 0 10px 0;">挂起原因：{{ task.suspend_reason }}</p>{% endif %}
                {% if can_operate or (current_user and current_user.role_level >= 2) %}
                <form method="post" action="{{ url_for('resume_task', task_id=task.task_id) }}">
                    <button type="submit" class="btn btn-primary" onclick="return confirm('确认恢复该工单？')">恢复处理</button>
                </form>
                {% endif %}
            </div>
            {% endif %}

            <!-- 待回执：管理员确认完成 -->
            {% if task.status == '待回执' %}
            {% if task.arrived_at %}<p style="font-size:13px;color:#6b7280;margin:0 0 4px 0;">到达时间：{{ task.arrived_at }}</p>{% endif %}
            {% if task.completed_at %}<p style="font-size:13px;color:#6b7280;margin:0 0 12px 0;">完成时间：{{ task.completed_at }}</p>{% endif %}
            {% if current_user and current_user.role_level >= 2 %}
            <div class="flow-section flow-settle">
                <h3>确认完成</h3>
                <p style="color:#6b7280;font-size:13px;margin:0 0 10px 0;">工程师已完成任务并上传凭证，请审核后确认完成。</p>
                <form method="post" action="{{ url_for('settle_task', task_id=task.task_id) }}">
                    <textarea name="notes" placeholder="备注（可选）" style="width:100%;box-sizing:border-box;padding:8px;border:1px solid #d1d5db;border-radius:4px;font-size:13px;min-height:60px;resize:vertical;margin-bottom:10px;"></textarea>
                    <button type="submit" class="btn btn-success" onclick="return confirm('确认完成该工单？')">确认完成</button>
                </form>
            </div>
            {% else %}
            <p style="color:#92400e;font-size:14px;">工单已完成，等待管理员确认完成。</p>
            {% endif %}
            {% endif %}

            <!-- 已完成：显示完成信息 -->
            {% if task.status == '已完成' %}
            <div style="font-size:13px;color:#6b7280;line-height:1.8;">
                {% if task.arrived_at %}<div>到达时间：{{ task.arrived_at }}</div>{% endif %}
                {% if task.completed_at %}<div>完成时间：{{ task.completed_at }}</div>{% endif %}
                {% if task.settled_at %}<div>完成时间：{{ task.settled_at }}（{{ task.settler_name or '—' }}）</div>{% endif %}
            </div>
            {% endif %}

            <!-- 照片展示区 -->
            <div style="margin-top:16px;">
                <div id="arrivalGallery" class="gallery" data-type="arrival"></div>
                <div id="completionGallery" class="gallery" data-type="completion" style="margin-top:12px;"></div>
                <div id="generalGallery" class="gallery" data-type="general" style="margin-top:12px;"></div>
            </div>
            <p id="galleryEmpty" class="empty-hint" style="display:none;">暂无照片。</p>

            <!-- 通用照片上传（仅 can_operate） -->
            {% if can_operate and task.status not in ['待派单', '已派单', '已完成', '已取消'] %}
            <div class="upload-box" style="margin-top:12px;">
                <p style="margin:0 0 8px 0;font-size:13px;color:#6b7280;">其他照片（可选）</p>
                <input type="file" id="photoInput" accept="image/jpeg,image/png,image/gif,image/webp" multiple>
                <div style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;">
                    <button type="button" id="btnUpload" class="btn btn-light">上传其他照片</button>
                </div>
                <div id="uploadStatus" class="upload-status"></div>
            </div>
            {% endif %}
            <div style="margin-top:10px;">
                <button type="button" id="btnRefresh" class="btn btn-light">刷新照片</button>
            </div>
        </div>
    </div>
    <script>
    (function() {
        const taskId = {{ task.task_id }};
        const maxPhotos = {{ max_photos }};
        const emptyEl = document.getElementById('galleryEmpty');
        const initialImages = {{ images | tojson }};

        const TYPE_LABELS = { arrival: '到达照片', completion: '完成凭证', general: '其他照片' };

        function makeGalleryItem(img) {
            const wrap = document.createElement('div');
            wrap.className = 'gallery-item';
            wrap.dataset.id = img.image_id;
            const link = document.createElement('a');
            link.href = img.url; link.target = '_blank'; link.rel = 'noopener';
            const image = document.createElement('img');
            image.src = img.url; image.alt = img.original_filename || '照片';
            link.appendChild(image);
            const cap = document.createElement('div');
            cap.style.cssText = 'font-size:12px;color:#6b7280;';
            cap.textContent = img.created_at || '';
            const del = document.createElement('button');
            del.type = 'button'; del.className = 'btn btn-danger btn-sm';
            del.style.marginTop = '6px'; del.textContent = '删除';
            del.addEventListener('click', function() { deleteImage(img.image_id); });
            wrap.appendChild(link); wrap.appendChild(cap); wrap.appendChild(del);
            return wrap;
        }

        function renderGalleries(images) {
            const byType = { arrival: [], completion: [], general: [] };
            (images || []).forEach(function(img) {
                const t = img.image_type || 'general';
                if (!byType[t]) byType[t] = [];
                byType[t].push(img);
            });
            let total = 0;
            ['arrival', 'completion', 'general'].forEach(function(t) {
                const el = document.getElementById(t + 'Gallery');
                if (!el) return;
                el.innerHTML = '';
                const items = byType[t] || [];
                total += items.length;
                if (items.length) {
                    const lbl = document.createElement('p');
                    lbl.className = 'gallery-section-label';
                    lbl.textContent = TYPE_LABELS[t] + '（' + items.length + '张）';
                    el.appendChild(lbl);
                    items.forEach(function(img) { el.appendChild(makeGalleryItem(img)); });
                }
            });
            emptyEl.style.display = total === 0 ? 'block' : 'none';
        }

        function setStatus(elId, text, type) {
            const el = document.getElementById(elId);
            if (!el) return;
            el.textContent = text || '';
            el.className = 'upload-status' + (type ? ' ' + type : '');
        }

        async function uploadWithType(inputId, btnId, statusId, imageType) {
            const input = document.getElementById(inputId);
            if (!input || !input.files || !input.files.length) {
                setStatus(statusId, '请先选择图片文件', 'error'); return;
            }
            const fd = new FormData();
            for (let i = 0; i < input.files.length; i++) fd.append('photos', input.files[i]);
            fd.append('image_type', imageType);
            setStatus(statusId, '上传中…', '');
            const btn = document.getElementById(btnId);
            if (btn) btn.disabled = true;
            try {
                const res = await fetch('/api/task/' + taskId + '/images', { method: 'POST', body: fd });
                const data = await res.json();
                if (!data.ok) { setStatus(statusId, data.message || '上传失败', 'error'); return; }
                renderGalleries(data.images);
                input.value = '';
                setStatus(statusId, data.message || '上传成功', 'ok');
                if (imageType === 'arrival' || imageType === 'completion') {
                    setTimeout(function() { location.reload(); }, 1200);
                }
            } catch (e) {
                setStatus(statusId, '网络错误：' + e, 'error');
            } finally {
                if (btn) btn.disabled = false;
            }
        }

        async function loadImages() {
            const res = await fetch('/api/task/' + taskId + '/images');
            const data = await res.json();
            if (!data.ok) return;
            renderGalleries(data.images);
        }

        async function deleteImage(imageId) {
            if (!confirm('确定删除这张照片？')) return;
            try {
                const res = await fetch('/api/task/' + taskId + '/images/' + imageId, { method: 'DELETE' });
                const data = await res.json();
                if (data.ok) renderGalleries(data.images);
            } catch (e) {}
        }

        document.getElementById('btnRefresh').addEventListener('click', loadImages);
        {% if can_operate %}
        const btnArrival = document.getElementById('btnArrival');
        if (btnArrival) btnArrival.addEventListener('click', function() { uploadWithType('arrivalInput', 'btnArrival', 'arrivalStatus', 'arrival'); });
        const btnCompletion = document.getElementById('btnCompletion');
        if (btnCompletion) btnCompletion.addEventListener('click', function() { uploadWithType('completionInput', 'btnCompletion', 'completionStatus', 'completion'); });
        const btnUpload = document.getElementById('btnUpload');
        if (btnUpload) btnUpload.addEventListener('click', function() { uploadWithType('photoInput', 'btnUpload', 'uploadStatus', 'general'); });
        {% endif %}
        renderGalleries(initialImages);
    })();
    </script>
''' + _STAFF_SWITCH_BTN + '''
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
                    <input type="text" id="title" name="title" required maxlength="200" value="{{ prefill.title if prefill else '' }}">
                </div>
                <div class="form-group">
                    <label for="fault_type">业务类型</label>
                    <select id="fault_type" name="fault_type" onchange="onBizTypeChange(this.value)">
                        <option value="">请选择</option>
                        {% for ft in fault_business_types %}
                        <option value="{{ ft }}" {% if prefill and prefill.fault_type == ft %}selected{% endif %}>{{ ft }}</option>
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
                    <label>客户地址</label>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;">
                        <select id="task_province" onchange="onTaskProvinceChange()" style="flex:1;min-width:120px;">
                            <option value="">选择省份</option>
                        </select>
                        <select id="task_city" onchange="onTaskCityChange()" style="flex:1;min-width:120px;">
                            <option value="">选择城市</option>
                        </select>
                        <button type="button" class="btn btn-primary" style="white-space:nowrap;" onclick="getTaskLocation()">📍 获取位置</button>
                    </div>
                    <input type="text" id="task_detail" name="task_detail" maxlength="300" placeholder="详细地址（街道、门牌号等）" style="width:100%;box-sizing:border-box;" value="{{ prefill.customer_address if prefill else '' }}">
                    <input type="hidden" id="customer_address" name="customer_address" value="{{ prefill.customer_address if prefill else '' }}">
                    <input type="hidden" id="task_lat" name="task_lat">
                    <input type="hidden" id="task_lng" name="task_lng">
                    <small id="loc-status" style="color:#6b7280;font-size:12px;"></small>
                </div>
                <div class="form-group">
                    <label for="description">任务说明</label>
                    <textarea id="description" name="description" placeholder="可选：具体要求、客户信息等">{% if prefill and (prefill.contact_name or prefill.contact_phone) %}联系人：{{ prefill.contact_name }}  电话：{{ prefill.contact_phone }}{% endif %}</textarea>
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
                    <input type="hidden" name="source_report_id" value="{{ prefill.source_report_id if prefill else '' }}">
                    <input type="hidden" name="reporter_user_id" value="{{ prefill.reporter_user_id if prefill else '' }}">
                    <button type="submit" class="btn btn-success">保存</button>
                    <a href="{{ url_for('tasks_page') }}" class="btn btn-danger">取消</a>
                </div>
            </form>
        </div>
    </div>
<script>
var PC_TASK = {
"北京":["东城","西城","朝阳","丰台","石景山","海淀","门头沟","房山","通州","顺义","昌平","大兴","怀柔","平谷","密云","延庆"],
"天津":["和平","河东","河西","南开","河北","红桥","东丽","西青","津南","北辰","武清","宝坻","滨海新区","宁河","静海","蓟州"],
"河北":["石家庄","唐山","秦皇岛","邯郸","邢台","保定","张家口","承德","沧州","廊坊","衡水"],
"山西":["太原","大同","阳泉","长治","晋城","朔州","晋中","运城","忻州","临汾","吕梁"],
"内蒙古":["呼和浩特","包头","乌海","赤峰","通辽","鄂尔多斯","呼伦贝尔","巴彦淖尔","乌兰察布","兴安盟","锡林郭勒盟","阿拉善盟"],
"辽宁":["沈阳","大连","鞍山","抚顺","本溪","丹东","锦州","营口","阜新","辽阳","盘锦","铁岭","朝阳","葫芦岛"],
"吉林":["长春","吉林","四平","辽源","通化","白山","松原","白城","延边"],
"黑龙江":["哈尔滨","齐齐哈尔","鸡西","鹤岗","双鸭山","大庆","伊春","佳木斯","七台河","牡丹江","黑河","绥化","大兴安岭"],
"上海":["黄浦","徐汇","长宁","静安","普陀","虹口","杨浦","闵行","宝山","嘉定","浦东新区","金山","松江","青浦","奉贤","崇明"],
"江苏":["南京","无锡","徐州","常州","苏州","南通","连云港","淮安","盐城","扬州","镇江","泰州","宿迁"],
"浙江":["杭州","宁波","温州","嘉兴","湖州","绍兴","金华","衢州","舟山","台州","丽水"],
"安徽":["合肥","芜湖","蚌埠","淮南","马鞍山","淮北","铜陵","安庆","黄山","滁州","阜阳","宿州","六安","亳州","池州","宣城"],
"福建":["福州","厦门","莆田","三明","泉州","漳州","南平","龙岩","宁德"],
"江西":["南昌","景德镇","萍乡","九江","新余","鹰潭","赣州","吉安","宜春","抚州","上饶"],
"山东":["济南","青岛","淄博","枣庄","东营","烟台","潍坊","济宁","泰安","威海","日照","临沂","德州","聊城","滨州","菏泽"],
"河南":["郑州","开封","洛阳","平顶山","安阳","鹤壁","新乡","焦作","濮阳","许昌","漯河","三门峡","南阳","商丘","信阳","周口","驻马店"],
"湖北":["武汉","黄石","十堰","宜昌","襄阳","鄂州","荆门","孝感","荆州","黄冈","咸宁","随州","恩施","仙桃","潜江","天门","神农架"],
"湖南":["长沙","株洲","湘潭","衡阳","邵阳","岳阳","常德","张家界","益阳","郴州","永州","怀化","娄底","湘西"],
"广东":["广州","深圳","珠海","汕头","佛山","韶关","湛江","肇庆","江门","茂名","惠州","梅州","汕尾","河源","阳江","清远","东莞","中山","潮州","揭州","云浮"],
"广西":["南宁","柳州","桂林","梧州","北海","防城港","钦州","贵港","玉林","百色","贺州","河池","来宾","崇左"],
"海南":["海口","三亚","三沙","儋州","五指山","琼海","文昌","万宁","东方","定安","屯昌","澄迈","临高","白沙","昌江","乐东","陵水","保亭","琼中"],
"重庆":["万州","涪陵","渝中","大渡口","江北","沙坪坝","九龙坡","南岸","北碚","綦江","大足","渝北","巴南","黔江","长寿","江津","合川","永川","南川","璧山","铜梁","潼南","荣昌","开州","梁平","武隆","城口","丰都","垫江","忠县","云阳","奉节","巫山","巫溪","石柱","秀山","酉阳","彭水"],
"四川":["成都","自贡","攀枝花","泸州","德阳","绵阳","广元","遂宁","内江","乐山","南充","眉山","宜宾","广安","达州","雅安","巴中","资阳","阿坝","甘孜","凉山"],
"贵州":["贵阳","六盘水","遵义","安顺","毕节","铜仁","黔西南","黔东南","黔南"],
"云南":["昆明","曲靖","玉溪","保山","昭通","丽江","普洱","临沧","楚雄","红河","文山","西双版纳","大理","德宏","怒江","迪庆"],
"西藏":["拉萨","日喀则","昌都","林芝","山南","那曲","阿里"],
"陕西":["西安","铜川","宝鸡","咸阳","渭南","延安","汉中","榆林","安康","商洛"],
"甘肃":["兰州","嘉峪关","金昌","白银","天水","武威","张掖","平凉","酒泉","庆阳","定西","陇南","临夏","甘南"],
"青海":["西宁","海东","海北","黄南","海南","果洛","玉树","海西"],
"宁夏":["银川","石嘴山","吴忠","固原","中卫"],
"新疆":["乌鲁木齐","克拉玛依","吐鲁番","哈密","昌吉","博尔塔拉","巴音郭楞","阿克苏","克孜勒苏","喀什","和田","伊犁","塔城","阿勒泰","石河子","阿拉尔","图木舒克","五家渠","北屯","铁门关","双河","可克达拉","昆玉","胡杨河"]
};
function onTaskProvinceChange() {
    var pSel = document.getElementById('task_province');
    var cSel = document.getElementById('task_city');
    var prov = pSel.value;
    cSel.innerHTML = '<option value="">选择城市</option>';
    if (prov && PC_TASK[prov]) {
        PC_TASK[prov].forEach(function(c) {
            var o = document.createElement('option');
            o.value = c; o.textContent = c;
            cSel.appendChild(o);
        });
    }
    updateTaskAddress();
}
function onTaskCityChange() { updateTaskAddress(); }
function updateTaskAddress() {
    var prov = document.getElementById('task_province').value;
    var city = document.getElementById('task_city').value;
    var detail = document.getElementById('task_detail').value;
    document.getElementById('customer_address').value = (prov || '') + (city || '') + (detail || '');
}
document.addEventListener('DOMContentLoaded', function() {
    var pSel = document.getElementById('task_province');
    Object.keys(PC_TASK).forEach(function(p) {
        var o = document.createElement('option');
        o.value = p; o.textContent = p;
        pSel.appendChild(o);
    });
    document.getElementById('task_detail').addEventListener('input', updateTaskAddress);
});
function getTaskLocation() {
    var status = document.getElementById('loc-status');
    status.textContent = '定位中...';
    status.style.color = '#6b7280';
    fetch('/api/ip_location').then(function(r){ return r.json(); }).then(function(d) {
        if (!d.ok) throw new Error(d.message || '定位失败');
        var prov = d.province || '';
        var city = d.city || '';
        var pSel = document.getElementById('task_province');
        if (prov) {
            pSel.value = prov;
            onTaskProvinceChange();
            if (city) document.getElementById('task_city').value = city;
            updateTaskAddress();
        }
        status.textContent = '已获取位置：' + prov + city;
        status.style.color = '#15803d';
    }).catch(function(e) {
        status.textContent = '定位失败：' + e.message;
        status.style.color = '#b91c1c';
    });
}
</script>
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
                    <label>客户地址</label>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;">
                        <select id="task_province" onchange="onTaskProvinceChange()" style="flex:1;min-width:120px;">
                            <option value="">选择省份</option>
                        </select>
                        <select id="task_city" onchange="onTaskCityChange()" style="flex:1;min-width:120px;">
                            <option value="">选择城市</option>
                        </select>
                    </div>
                    <input type="text" id="task_detail" name="task_detail" maxlength="300" placeholder="详细地址（街道、门牌号等）" style="width:100%;box-sizing:border-box;">
                    <input type="hidden" id="customer_address" name="customer_address" value="{{ task.customer_address or '' }}">
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
<script>
var PC_ET = {
"北京":["东城","西城","朝阳","丰台","石景山","海淀","门头沟","房山","通州","顺义","昌平","大兴","怀柔","平谷","密云","延庆"],
"天津":["和平","河东","河西","南开","河北","红桥","东丽","西青","津南","北辰","武清","宝坻","滨海新区","宁河","静海","蓟州"],
"河北":["石家庄","唐山","秦皇岛","邯郸","邢台","保定","张家口","承德","沧州","廊坊","衡水"],
"山西":["太原","大同","阳泉","长治","晋城","朔州","晋中","运城","忻州","临汾","吕梁"],
"内蒙古":["呼和浩特","包头","乌海","赤峰","通辽","鄂尔多斯","呼伦贝尔","巴彦淖尔","乌兰察布","兴安盟","锡林郭勒盟","阿拉善盟"],
"辽宁":["沈阳","大连","鞍山","抚顺","本溪","丹东","锦州","营口","阜新","辽阳","盘锦","铁岭","朝阳","葫芦岛"],
"吉林":["长春","吉林","四平","辽源","通化","白山","松原","白城","延边"],
"黑龙江":["哈尔滨","齐齐哈尔","鸡西","鹤岗","双鸭山","大庆","伊春","佳木斯","七台河","牡丹江","黑河","绥化","大兴安岭"],
"上海":["黄浦","徐汇","长宁","静安","普陀","虹口","杨浦","闵行","宝山","嘉定","浦东新区","金山","松江","青浦","奉贤","崇明"],
"江苏":["南京","无锡","徐州","常州","苏州","南通","连云港","淮安","盐城","扬州","镇江","泰州","宿迁"],
"浙江":["杭州","宁波","温州","嘉兴","湖州","绍兴","金华","衢州","舟山","台州","丽水"],
"安徽":["合肥","芜湖","蚌埠","淮南","马鞍山","淮北","铜陵","安庆","黄山","滁州","阜阳","宿州","六安","亳州","池州","宣城"],
"福建":["福州","厦门","莆田","三明","泉州","漳州","南平","龙岩","宁德"],
"江西":["南昌","景德镇","萍乡","九江","新余","鹰潭","赣州","吉安","宜春","抚州","上饶"],
"山东":["济南","青岛","淄博","枣庄","东营","烟台","潍坊","济宁","泰安","威海","日照","临沂","德州","聊城","滨州","菏泽"],
"河南":["郑州","开封","洛阳","平顶山","安阳","鹤壁","新乡","焦作","濮阳","许昌","漯河","三门峡","南阳","商丘","信阳","周口","驻马店"],
"湖北":["武汉","黄石","十堰","宜昌","襄阳","鄂州","荆门","孝感","荆州","黄冈","咸宁","随州","恩施","仙桃","潜江","天门","神农架"],
"湖南":["长沙","株洲","湘潭","衡阳","邵阳","岳阳","常德","张家界","益阳","郴州","永州","怀化","娄底","湘西"],
"广东":["广州","深圳","珠海","汕头","佛山","韶关","湛江","肇庆","江门","茂名","惠州","梅州","汕尾","河源","阳江","清远","东莞","中山","潮州","揭州","云浮"],
"广西":["南宁","柳州","桂林","梧州","北海","防城港","钦州","贵港","玉林","百色","贺州","河池","来宾","崇左"],
"海南":["海口","三亚","三沙","儋州","五指山","琼海","文昌","万宁","东方","定安","屯昌","澄迈","临高","白沙","昌江","乐东","陵水","保亭","琼中"],
"重庆":["万州","涪陵","渝中","大渡口","江北","沙坪坝","九龙坡","南岸","北碚","綦江","大足","渝北","巴南","黔江","长寿","江津","合川","永川","南川","璧山","铜梁","潼南","荣昌","开州","梁平","武隆","城口","丰都","垫江","忠县","云阳","奉节","巫山","巫溪","石柱","秀山","酉阳","彭水"],
"四川":["成都","自贡","攀枝花","泸州","德阳","绵阳","广元","遂宁","内江","乐山","南充","眉山","宜宾","广安","达州","雅安","巴中","资阳","阿坝","甘孜","凉山"],
"贵州":["贵阳","六盘水","遵义","安顺","毕节","铜仁","黔西南","黔东南","黔南"],
"云南":["昆明","曲靖","玉溪","保山","昭通","丽江","普洱","临沧","楚雄","红河","文山","西双版纳","大理","德宏","怒江","迪庆"],
"西藏":["拉萨","日喀则","昌都","林芝","山南","那曲","阿里"],
"陕西":["西安","铜川","宝鸡","咸阳","渭南","延安","汉中","榆林","安康","商洛"],
"甘肃":["兰州","嘉峪关","金昌","白银","天水","武威","张掖","平凉","酒泉","庆阳","定西","陇南","临夏","甘南"],
"青海":["西宁","海东","海北","黄南","海南","果洛","玉树","海西"],
"宁夏":["银川","石嘴山","吴忠","固原","中卫"],
"新疆":["乌鲁木齐","克拉玛依","吐鲁番","哈密","昌吉","博尔塔拉","巴音郭楞","阿克苏","克孜勒苏","喀什","和田","伊犁","塔城","阿勒泰","石河子","阿拉尔","图木舒克","五家渠","北屯","铁门关","双河","可克达拉","昆玉","胡杨河"]
};
function onTaskProvinceChange() {
    var pSel = document.getElementById('task_province');
    var cSel = document.getElementById('task_city');
    cSel.innerHTML = '<option value="">选择城市</option>';
    var prov = pSel.value;
    if (prov && PC_ET[prov]) {
        PC_ET[prov].forEach(function(ct) {
            var o = document.createElement('option');
            o.value = ct; o.textContent = ct; cSel.appendChild(o);
        });
    }
    updateTaskAddress();
}
function onTaskCityChange() { updateTaskAddress(); }
function updateTaskAddress() {
    var prov = document.getElementById('task_province').value;
    var city = document.getElementById('task_city').value;
    var detail = document.getElementById('task_detail').value;
    document.getElementById('customer_address').value = (prov||'') + (city||'') + (detail||'');
}
document.addEventListener('DOMContentLoaded', function() {
    var pSel = document.getElementById('task_province');
    Object.keys(PC_ET).forEach(function(p) {
        var o = document.createElement('option');
        o.value = p; o.textContent = p; pSel.appendChild(o);
    });
    var saved = document.getElementById('customer_address').value || '';
    if (!saved) { document.getElementById('task_detail').addEventListener('input', updateTaskAddress); return; }
    var matchedProv = '';
    Object.keys(PC_ET).forEach(function(p) { if (saved.startsWith(p)) matchedProv = p; });
    if (matchedProv) {
        pSel.value = matchedProv;
        onTaskProvinceChange();
        var rest = saved.slice(matchedProv.length);
        var matchedCity = '';
        (PC_ET[matchedProv]||[]).forEach(function(ct) { if (rest.startsWith(ct)) matchedCity = ct; });
        if (matchedCity) {
            document.getElementById('task_city').value = matchedCity;
            rest = rest.slice(matchedCity.length);
        }
        document.getElementById('task_detail').value = rest;
    } else {
        document.getElementById('task_detail').value = saved;
    }
    document.getElementById('task_detail').addEventListener('input', updateTaskAddress);
});
</script>
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


# ════════════════════════════════════════════════════════════
# 用户端（原 web2.py）— 合并入同一 Flask 应用
# ════════════════════════════════════════════════════════════

# ── 用户端密码工具 ────────────────────────────────────────
def _hash_eu_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 260000)
    return f"pbkdf2:sha256:260000${salt}${h.hex()}"

def _check_eu_password(password, stored):
    try:
        parts = stored.split('$')
        salt = parts[1]
        expected = parts[2]
        h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 260000)
        return h.hex() == expected
    except Exception:
        return False

# ── 用户端会话工具 ────────────────────────────────────────
def get_end_user():
    uid = session.get("eu_id")
    if not uid:
        return None
    return {"user_id": uid, "real_name": session.get("eu_name", ""), "phone": session.get("eu_phone", "")}

def require_end_user(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not get_end_user():
            return redirect(url_for("user_login", next=request.path))
        return f(*args, **kwargs)
    return decorated

# ── 用户端 DB 初始化 ──────────────────────────────────────
def ensure_end_users_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS end_users (
            user_id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(50) NOT NULL UNIQUE,
            password VARCHAR(255) NOT NULL,
            real_name VARCHAR(50) NOT NULL,
            phone VARCHAR(20) NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            status TINYINT NOT NULL DEFAULT 1
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    conn.commit()

def ensure_issue_reports_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS issue_reports (
            report_id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            title VARCHAR(200) NOT NULL,
            description TEXT,
            fault_type VARCHAR(50),
            fault_phenomenon VARCHAR(100),
            customer_address VARCHAR(300),
            contact_name VARCHAR(50),
            contact_phone VARCHAR(20),
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_user (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        # 幂等添加 IP 定位列
        cur.execute("SHOW COLUMNS FROM issue_reports LIKE 'reporter_ip'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE issue_reports ADD COLUMN reporter_ip VARCHAR(64) DEFAULT NULL AFTER created_at")
        cur.execute("SHOW COLUMNS FROM issue_reports LIKE 'reporter_province'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE issue_reports ADD COLUMN reporter_province VARCHAR(50) DEFAULT NULL AFTER reporter_ip")
        cur.execute("SHOW COLUMNS FROM issue_reports LIKE 'reporter_city'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE issue_reports ADD COLUMN reporter_city VARCHAR(50) DEFAULT NULL AFTER reporter_province")
    conn.commit()

def ensure_task_ratings_table(conn):
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS task_ratings (
            rating_id INT AUTO_INCREMENT PRIMARY KEY,
            task_id INT NOT NULL UNIQUE,
            user_id INT NOT NULL,
            stars TINYINT NOT NULL,
            comment TEXT,
            rated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_task (task_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    conn.commit()

def _init_user_db():
    conn = get_db_connection()
    if conn:
        try:
            ensure_end_users_table(conn)
            ensure_issue_reports_table(conn)
            ensure_task_ratings_table(conn)
        finally:
            conn.close()

# ── 省市数据 ──────────────────────────────────────────────
EU_PC_DATA = {
"北京":["东城","西城","朝阳","丰台","石景山","海淀","门头沟","房山","通州","顺义","昌平","大兴","怀柔","平谷","密云","延庆"],
"天津":["和平","河东","河西","南开","河北","红桥","东丽","西青","津南","北辰","武清","宝坻","滨海新区","宁河","静海","蓟州"],
"河北":["石家庄","唐山","秦皇岛","邯郸","邢台","保定","张家口","承德","沧州","廊坊","衡水"],
"山西":["太原","大同","阳泉","长治","晋城","朔州","晋中","运城","忻州","临汾","吕梁"],
"内蒙古":["呼和浩特","包头","乌海","赤峰","通辽","鄂尔多斯","呼伦贝尔","巴彦淖尔","乌兰察布","兴安盟","锡林郭勒盟","阿拉善盟"],
"辽宁":["沈阳","大连","鞍山","抚顺","本溪","丹东","锦州","营口","阜新","辽阳","盘锦","铁岭","朝阳","葫芦岛"],
"吉林":["长春","吉林","四平","辽源","通化","白山","松原","白城","延边"],
"黑龙江":["哈尔滨","齐齐哈尔","鸡西","鹤岗","双鸭山","大庆","伊春","佳木斯","七台河","牡丹江","黑河","绥化","大兴安岭"],
"上海":["黄浦","徐汇","长宁","静安","普陀","虹口","杨浦","闵行","宝山","嘉定","浦东新区","金山","松江","青浦","奉贤","崇明"],
"江苏":["南京","无锡","徐州","常州","苏州","南通","连云港","淮安","盐城","扬州","镇江","泰州","宿迁"],
"浙江":["杭州","宁波","温州","嘉兴","湖州","绍兴","金华","衢州","舟山","台州","丽水"],
"安徽":["合肥","芜湖","蚌埠","淮南","马鞍山","淮北","铜陵","安庆","黄山","滁州","阜阳","宿州","六安","亳州","池州","宣城"],
"福建":["福州","厦门","莆田","三明","泉州","漳州","南平","龙岩","宁德"],
"江西":["南昌","景德镇","萍乡","九江","新余","鹰潭","赣州","吉安","宜春","抚州","上饶"],
"山东":["济南","青岛","淄博","枣庄","东营","烟台","潍坊","济宁","泰安","威海","日照","临沂","德州","聊城","滨州","菏泽"],
"河南":["郑州","开封","洛阳","平顶山","安阳","鹤壁","新乡","焦作","濮阳","许昌","漯河","三门峡","南阳","商丘","信阳","周口","驻马店"],
"湖北":["武汉","黄石","十堰","宜昌","襄阳","鄂州","荆门","孝感","荆州","黄冈","咸宁","随州","恩施","仙桃","潜江","天门","神农架"],
"湖南":["长沙","株洲","湘潭","衡阳","邵阳","岳阳","常德","张家界","益阳","郴州","永州","怀化","娄底","湘西"],
"广东":["广州","深圳","珠海","汕头","佛山","韶关","湛江","肇庆","江门","茂名","惠州","梅州","汕尾","河源","阳江","清远","东莞","中山","潮州","揭州","云浮"],
"广西":["南宁","柳州","桂林","梧州","北海","防城港","钦州","贵港","玉林","百色","贺州","河池","来宾","崇左"],
"海南":["海口","三亚","三沙","儋州","五指山","琼海","文昌","万宁","东方","定安","屯昌","澄迈","临高","白沙","昌江","乐东","陵水","保亭","琼中"],
"重庆":["万州","涪陵","渝中","大渡口","江北","沙坪坝","九龙坡","南岸","北碚","綦江","大足","渝北","巴南","黔江","长寿","江津","合川","永川","南川","璧山","铜梁","潼南","荣昌","开州","梁平","武隆","城口","丰都","垫江","忠县","云阳","奉节","巫山","巫溪","石柱","秀山","酉阳","彭水"],
"四川":["成都","自贡","攀枝花","泸州","德阳","绵阳","广元","遂宁","内江","乐山","南充","眉山","宜宾","广安","达州","雅安","巴中","资阳","阿坝","甘孜","凉山"],
"贵州":["贵阳","六盘水","遵义","安顺","毕节","铜仁","黔西南","黔东南","黔南"],
"云南":["昆明","曲靖","玉溪","保山","昭通","丽江","普洱","临沧","楚雄","红河","文山","西双版纳","大理","德宏","怒江","迪庆"],
"西藏":["拉萨","日喀则","昌都","林芝","山南","那曲","阿里"],
"陕西":["西安","铜川","宝鸡","咸阳","渭南","延安","汉中","榆林","安康","商洛"],
"甘肃":["兰州","嘉峪关","金昌","白银","天水","武威","张掖","平凉","酒泉","庆阳","定西","陇南","临夏","甘南"],
"青海":["西宁","海东","海北","黄南","海南","果洛","玉树","海西"],
"宁夏":["银川","石嘴山","吴忠","固原","中卫"],
"新疆":["乌鲁木齐","克拉玛依","吐鲁番","哈密","昌吉","博尔塔拉","巴音郭楞","阿克苏","克孜勒苏","喀什","和田","伊犁","塔城","阿勒泰","石河子"],
}

EU_FAULT_TYPES_NEED_PHENOMENON = {"宽带修障", "IPTV修障", "电话修障", "线路维护"}

# ── 切换按钮片段（注入到所有用户端页面） ─────────────────
_EU_SWITCH_BTN = '''<a href="/tasks" style="position:fixed;bottom:20px;right:20px;z-index:9999;background:#2c3e50;color:#fff;padding:8px 16px;border-radius:20px;font-size:13px;text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,0.25);">🔧 工程师端</a>'''

# ── 用户端 CSS ────────────────────────────────────────────
_EU_BASE_CSS = """
    * { box-sizing: border-box; }
    body { font-family: "Microsoft YaHei", sans-serif; margin: 0; background: #f0f4f8; min-height: 100vh; }
    header { background: #2c3e50; color: white; padding: 16px 20px; display:flex; justify-content:space-between; align-items:center; }
    header h1 { margin: 0; font-size: 20px; }
    header p { margin: 4px 0 0; font-size: 13px; opacity: 0.8; }
    .header-right { font-size:13px; color:rgba(255,255,255,0.85); display:flex; gap:12px; align-items:center; }
    .header-right a { color:rgba(255,255,255,0.85); text-decoration:none; }
    .header-right a:hover { text-decoration:underline; }
    .container { max-width: 640px; margin: 24px auto; padding: 0 16px; }
    .card { background: white; border-radius: 8px; padding: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
    .form-group { margin-bottom: 16px; }
    label { display: block; font-size: 14px; font-weight: 600; color: #374151; margin-bottom: 6px; }
    input, select, textarea { width: 100%; padding: 9px 12px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px; font-family: inherit; }
    input:focus, select:focus, textarea:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.1); }
    textarea { resize: vertical; min-height: 80px; }
    .row { display: flex; gap: 10px; }
    .row > * { flex: 1; }
    .required { color: #ef4444; }
    .btn { display: inline-block; padding: 10px 24px; border: none; border-radius: 6px; font-size: 15px; cursor: pointer; font-family: inherit; }
    .btn-primary { background: #3b82f6; color: white; width: 100%; }
    .btn-primary:hover { background: #2563eb; }
    .btn-loc { background: #f3f4f6; color: #374151; border: 1px solid #d1d5db; padding: 9px 12px; white-space: nowrap; width: auto; }
    .flash { padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 14px; }
    .flash-success { background: #d1fae5; color: #065f46; }
    .flash-danger { background: #fee2e2; color: #991b1b; }
    .flash-warning { background: #fef3c7; color: #92400e; }
    .section-title { font-size: 13px; font-weight: 700; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; margin: 20px 0 12px; border-top: 1px solid #f3f4f6; padding-top: 16px; }
    small { color: #6b7280; font-size: 12px; }
    .link { color: #3b82f6; text-decoration: none; font-size: 14px; }
    .link:hover { text-decoration: underline; }
    .pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:600; }
    .pill-pending { background:#fef3c7; color:#92400e; }
    .pill-done { background:#d1fae5; color:#065f46; }
    .pill-archived { background:#e0e7ff; color:#3730a3; }
    .reports-table { width:100%; border-collapse:collapse; font-size:13px; }
    .reports-table th { background:#f9fafb; padding:8px 10px; text-align:left; border-bottom:2px solid #e5e7eb; }
    .reports-table td { padding:8px 10px; border-bottom:1px solid #f3f4f6; }
    .stars-display { color:#f59e0b; font-size:16px; }
    @media (max-width: 480px) { .row { flex-direction: column; } }
"""

EU_LOGIN_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>用户登录 - 装维报修</title><style>''' + _EU_BASE_CSS + '''</style></head>
<body>
<header><div><h1>装维报修平台</h1><p>用户服务门户</p></div></header>
<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat, msg in messages %}<div class="flash flash-{{ cat }}">{{ msg }}</div>{% endfor %}{% endif %}
{% endwith %}
<div class="card">
    <h2 style="margin:0 0 20px;font-size:20px;color:#111827;">登录</h2>
    <form method="post">
        <div class="form-group"><label>用户名 <span class="required">*</span></label>
            <input type="text" name="username" maxlength="50" required autofocus></div>
        <div class="form-group"><label>密码 <span class="required">*</span></label>
            <input type="password" name="password" required></div>
        <button type="submit" class="btn btn-primary">登录</button>
    </form>
    <p style="margin:16px 0 0;text-align:center;font-size:14px;color:#6b7280;">
        还没有账号？<a href="{{ url_for('user_register') }}" class="link">立即注册</a>
    </p>
</div></div>''' + _EU_SWITCH_BTN + '''</body></html>'''

EU_REGISTER_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>用户注册 - 装维报修</title><style>''' + _EU_BASE_CSS + '''</style></head>
<body>
<header><div><h1>装维报修平台</h1><p>用户服务门户</p></div></header>
<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat, msg in messages %}<div class="flash flash-{{ cat }}">{{ msg }}</div>{% endfor %}{% endif %}
{% endwith %}
<div class="card">
    <h2 style="margin:0 0 20px;font-size:20px;color:#111827;">注册账号</h2>
    <form method="post">
        <div class="row">
            <div class="form-group"><label>用户名 <span class="required">*</span></label>
                <input type="text" name="username" maxlength="50" placeholder="登录用，字母数字" required autofocus></div>
            <div class="form-group"><label>真实姓名 <span class="required">*</span></label>
                <input type="text" name="real_name" maxlength="50" placeholder="您的真实姓名" required></div>
        </div>
        <div class="form-group"><label>手机号码 <span class="required">*</span></label>
            <input type="tel" name="phone" maxlength="11" placeholder="11位手机号" required></div>
        <div class="row">
            <div class="form-group"><label>密码 <span class="required">*</span></label>
                <input type="password" name="password" minlength="6" placeholder="至少6位" required></div>
            <div class="form-group"><label>确认密码 <span class="required">*</span></label>
                <input type="password" name="password2" minlength="6" placeholder="再次输入密码" required></div>
        </div>
        <button type="submit" class="btn btn-primary">注册</button>
    </form>
    <p style="margin:16px 0 0;text-align:center;font-size:14px;color:#6b7280;">
        已有账号？<a href="{{ url_for('user_login') }}" class="link">立即登录</a>
    </p>
</div></div>''' + _EU_SWITCH_BTN + '''</body></html>'''

EU_REPORT_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>问题上报 - 装维报修</title><style>''' + _EU_BASE_CSS + '''</style></head>
<body>
<header>
    <div><h1>装维报修平台</h1><p>请填写报修信息，我们将尽快安排处理</p></div>
    <div class="header-right">
        <span>{{ current_user.real_name }}</span>
        <a href="{{ url_for('user_my_reports') }}">我的上报</a>
        <a href="{{ url_for('user_logout') }}">退出</a>
    </div>
</header>
<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat, msg in messages %}<div class="flash flash-{{ cat }}">{{ msg }}</div>{% endfor %}{% endif %}
{% endwith %}
<div class="card">
    <form method="post">
        <div class="section-title" style="border-top:none;padding-top:0;margin-top:0;">联系信息</div>
        <div class="row">
            <div class="form-group"><label>联系人姓名 <span class="required">*</span></label>
                <input type="text" name="contact_name" maxlength="30" placeholder="您的姓名" required></div>
            <div class="form-group"><label>联系电话 <span class="required">*</span></label>
                <input type="tel" name="contact_phone" maxlength="20" placeholder="手机号码" required></div>
        </div>
        <div class="section-title">故障信息</div>
        <div class="form-group"><label>业务类型 <span class="required">*</span></label>
            <select name="fault_type" id="fault_type" onchange="onBizTypeChange(this.value)" required>
                <option value="">请选择</option>
                {% for ft in fault_business_types %}<option value="{{ ft }}">{{ ft }}</option>{% endfor %}
            </select></div>
        <div class="form-group" id="phenomenon_row" style="display:none;"><label>故障现象</label>
            <select name="fault_phenomenon" id="fault_phenomenon">
                <option value="">请选择</option>
                {% for fp in fault_phenomena %}<option value="{{ fp }}">{{ fp }}</option>{% endfor %}
            </select></div>
        <div class="form-group"><label>问题标题 <span class="required">*</span></label>
            <input type="text" name="title" maxlength="200" placeholder="简要描述问题，如：宽带无法上网" required></div>
        <div class="form-group"><label>详细描述</label>
            <textarea name="description" placeholder="可选：描述故障现象、发生时间等"></textarea></div>
        <div class="section-title">上门地址</div>
        <div class="form-group">
            <div class="row" style="margin-bottom:8px;">
                <select id="task_province" onchange="onTaskProvinceChange()"><option value="">选择省份</option></select>
                <select id="task_city" onchange="onTaskCityChange()"><option value="">选择城市</option></select>
                <button type="button" class="btn btn-loc" onclick="getLocation()">📍 定位</button>
            </div>
            <input type="text" id="task_detail" name="task_detail" maxlength="300" placeholder="详细地址（街道、门牌号等）">
            <input type="hidden" id="customer_address" name="customer_address">
            <small id="loc-status"></small>
        </div>
        <button type="submit" class="btn btn-primary" style="margin-top:8px;">提交上报</button>
    </form>
</div></div>
<script>
var PC = {{ pc_data|tojson }};
var NEED_PHENO = {{ fault_types_need_phenomenon|tojson }};
function onBizTypeChange(v){document.getElementById('phenomenon_row').style.display=NEED_PHENO.indexOf(v)>=0?'':'none';}
function onTaskProvinceChange(){var p=document.getElementById('task_province'),c=document.getElementById('task_city');c.innerHTML='<option value="">选择城市</option>';if(p.value&&PC[p.value])PC[p.value].forEach(function(ct){var o=document.createElement('option');o.value=ct;o.textContent=ct;c.appendChild(o);});updateAddr();}
function onTaskCityChange(){updateAddr();}
function updateAddr(){var p=document.getElementById('task_province').value,c=document.getElementById('task_city').value,d=document.getElementById('task_detail').value;document.getElementById('customer_address').value=(p||'')+(c||'')+(d||'');}
document.getElementById('task_detail').addEventListener('input',updateAddr);
(function(){var s=document.getElementById('task_province');Object.keys(PC).forEach(function(p){var o=document.createElement('option');o.value=p;o.textContent=p;s.appendChild(o);});})();
function getLocation(){var st=document.getElementById('loc-status');st.textContent='定位中...';st.style.color='#6b7280';fetch('/api/ip_location').then(function(r){return r.json();}).then(function(d){if(!d.ok)throw new Error(d.message);var pSel=document.getElementById('task_province');pSel.value=d.province||'';onTaskProvinceChange();setTimeout(function(){document.getElementById('task_city').value=d.city||'';updateAddr();},50);st.textContent='定位成功：'+(d.province||'')+(d.city||'');st.style.color='#065f46';}).catch(function(e){st.textContent='定位失败：'+e.message;st.style.color='#991b1b';});}
</script>''' + _EU_SWITCH_BTN + '''</body></html>'''

EU_MY_REPORTS_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>我的上报 - 装维报修</title><style>''' + _EU_BASE_CSS + '''</style></head>
<body>
<header>
    <div><h1>装维报修平台</h1><p>我的上报记录</p></div>
    <div class="header-right">
        <span>{{ current_user.real_name }}</span>
        <a href="{{ url_for('user_report') }}">提交上报</a>
        <a href="{{ url_for('user_logout') }}">退出</a>
    </div>
</header>
<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat, msg in messages %}<div class="flash flash-{{ cat }}">{{ msg }}</div>{% endfor %}{% endif %}
{% endwith %}
<div class="card">
    <h2 style="margin:0 0 12px;font-size:18px;">上报记录</h2>
    {% if reports %}
    <table class="reports-table">
        <thead><tr><th>编号</th><th>标题</th><th>业务类型</th><th>联系人</th><th>上报时间</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
        {% for r in reports %}
        <tr>
            <td>#{{ r.report_id }}</td>
            <td>{{ r.title }}</td>
            <td>{{ r.fault_type or '—' }}</td>
            <td>{{ r.contact_name or '—' }}{% if r.contact_phone %} {{ r.contact_phone }}{% endif %}</td>
            <td style="white-space:nowrap;">{{ r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '—' }}</td>
            <td>
                {% if r.display_status == 'pending' %}<span class="pill pill-pending">待处理</span>
                {% elif r.display_status == '处理中' %}<span class="pill" style="background:#dbeafe;color:#1e40af;">处理中</span>
                {% elif r.display_status == '已完成' %}<span class="pill pill-done">已完成</span>
                {% elif r.display_status == '已归档' %}<span class="pill pill-archived">已归档</span>
                {% else %}<span class="pill pill-pending">{{ r.display_status }}</span>{% endif %}
            </td>
            <td>
                {% if r.ratable_task_id %}
                <a href="{{ url_for('user_rate_task', task_id=r.ratable_task_id) }}" class="link">去评价</a>
                {% endif %}
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <p style="text-align:center;color:#9ca3af;padding:24px 0;">暂无上报记录，<a href="{{ url_for('user_report') }}" class="link">立即上报</a></p>
    {% endif %}
</div>
{% if rated_tasks %}
<div class="card" style="margin-top:16px;">
    <h2 style="margin:0 0 12px;font-size:18px;">已评价工单</h2>
    <table class="reports-table">
        <thead><tr><th>工单号</th><th>标题</th><th>评分</th><th>评价内容</th><th>评价时间</th><th>状态</th></tr></thead>
        <tbody>
        {% for t in rated_tasks %}
        <tr>
            <td>#{{ t.task_id }}</td>
            <td>{{ t.title }}</td>
            <td><span class="stars-display">{{ '★' * t.rating_stars }}{{ '☆' * (5 - t.rating_stars) }}</span></td>
            <td>{{ t.rating_comment or '—' }}</td>
            <td style="white-space:nowrap;">{{ t.rated_at.strftime('%Y-%m-%d') if t.rated_at else '—' }}</td>
            <td>{% if t.status == '已归档' %}<span class="pill pill-archived">已归档</span>
                {% else %}<span class="pill pill-done">已完成</span>{% endif %}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
</div>
{% endif %}
</div>''' + _EU_SWITCH_BTN + '''</body></html>'''

EU_RATE_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>工单评价 - 装维报修</title><style>''' + _EU_BASE_CSS + '''
    .star-group { display:flex; flex-direction:row-reverse; justify-content:flex-end; gap:4px; margin:8px 0 16px; }
    .star-group input { display:none; }
    .star-group label { font-size:36px; color:#d1d5db; cursor:pointer; transition:color 0.1s; }
    .star-group input:checked ~ label, .star-group label:hover, .star-group label:hover ~ label { color:#f59e0b; }
</style></head>
<body>
<header>
    <div><h1>装维报修平台</h1><p>工单服务评价</p></div>
    <div class="header-right">
        <span>{{ current_user.real_name }}</span>
        <a href="{{ url_for('user_my_reports') }}">我的上报</a>
        <a href="{{ url_for('user_logout') }}">退出</a>
    </div>
</header>
<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat, msg in messages %}<div class="flash flash-{{ cat }}">{{ msg }}</div>{% endfor %}{% endif %}
{% endwith %}
<div class="card">
    <h2 style="margin:0 0 6px;font-size:18px;">评价工单 #{{ task.task_id }}</h2>
    <p style="color:#6b7280;font-size:14px;margin:0 0 20px;">{{ task.title }}</p>
    <form method="post">
        <div class="form-group">
            <label>服务评分 <span class="required">*</span></label>
            <div class="star-group">
                {% for i in [5,4,3,2,1] %}
                <input type="radio" name="stars" id="star{{ i }}" value="{{ i }}" {% if i==5 %}required{% endif %}>
                <label for="star{{ i }}" title="{{ i }}星">★</label>
                {% endfor %}
            </div>
            <small style="color:#92400e;">提示：五星评价后工单将自动归档</small>
        </div>
        <div class="form-group"><label>评价内容</label>
            <textarea name="comment" placeholder="请描述本次服务体验（可选）"></textarea></div>
        <button type="submit" class="btn btn-primary">提交评价</button>
    </form>
    <p style="margin:12px 0 0;text-align:center;">
        <a href="{{ url_for('user_my_reports') }}" class="link">返回我的上报</a>
    </p>
</div></div>''' + _EU_SWITCH_BTN + '''</body></html>'''


# ── 用户端路由 ────────────────────────────────────────────
@app.route("/user/")
def user_index():
    if get_end_user():
        return redirect(url_for("user_report"))
    return redirect(url_for("user_login"))


@app.route("/user/register", methods=["GET", "POST"])
def user_register():
    if get_end_user():
        return redirect(url_for("user_report"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        real_name = request.form.get("real_name", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if not all([username, real_name, phone, password]):
            flash("请填写所有必填项", "danger")
            return redirect(url_for("user_register"))
        if len(phone) != 11 or not phone.isdigit():
            flash("手机号码格式不正确", "danger")
            return redirect(url_for("user_register"))
        if len(password) < 6:
            flash("密码至少6位", "danger")
            return redirect(url_for("user_register"))
        if password != password2:
            flash("两次密码不一致", "danger")
            return redirect(url_for("user_register"))
        conn = get_db_connection()
        if not conn:
            flash("数据库连接失败", "danger")
            return redirect(url_for("user_register"))
        try:
            ensure_end_users_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM end_users WHERE username=%s", (username,))
                if cur.fetchone():
                    flash("用户名已存在", "danger")
                    return redirect(url_for("user_register"))
                cur.execute(
                    "INSERT INTO end_users (username, password, real_name, phone) VALUES (%s,%s,%s,%s)",
                    (username, _hash_eu_password(password), real_name, phone)
                )
            conn.commit()
            flash("注册成功，请登录", "success")
            return redirect(url_for("user_login"))
        finally:
            conn.close()
    return render_template_string(EU_REGISTER_HTML)


@app.route("/user/login", methods=["GET", "POST"])
def user_login():
    if get_end_user():
        return redirect(url_for("user_report"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db_connection()
        if not conn:
            flash("数据库连接失败", "danger")
            return redirect(url_for("user_login"))
        try:
            ensure_end_users_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM end_users WHERE username=%s AND status=1", (username,))
                user = cur.fetchone()
        finally:
            conn.close()
        if not user or not _check_eu_password(password, user["password"]):
            flash("用户名或密码错误", "danger")
            return redirect(url_for("user_login"))
        session["eu_id"] = user["user_id"]
        session["eu_name"] = user["real_name"]
        session["eu_phone"] = user["phone"]
        return redirect(request.args.get("next") or url_for("user_report"))
    return render_template_string(EU_LOGIN_HTML)


@app.route("/user/logout")
def user_logout():
    session.pop("eu_id", None)
    session.pop("eu_name", None)
    session.pop("eu_phone", None)
    return redirect(url_for("user_login"))


@app.route("/user/report", methods=["GET", "POST"])
@require_end_user
def user_report():
    current_user = get_end_user()
    if request.method == "POST":
        conn = get_db_connection()
        if not conn:
            flash("数据库连接失败", "danger")
            return redirect(url_for("user_report"))
        try:
            ensure_issue_reports_table(conn)
            title = request.form.get("title", "").strip()
            fault_type = request.form.get("fault_type", "").strip()
            fault_phenomenon = request.form.get("fault_phenomenon", "").strip()
            customer_address = request.form.get("customer_address", "").strip()
            contact_name = request.form.get("contact_name", "").strip()
            contact_phone = request.form.get("contact_phone", "").strip()
            description = request.form.get("description", "").strip()
            if not title:
                flash("请填写问题标题", "danger")
                return redirect(url_for("user_report"))
            client_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
            if "," in client_ip:
                client_ip = client_ip.split(",")[0].strip()
            rep_prov, rep_city = _ip_to_province_city(client_ip)
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO issue_reports
                       (user_id, title, description, fault_type, fault_phenomenon,
                        customer_address, contact_name, contact_phone,
                        reporter_ip, reporter_province, reporter_city)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (current_user["user_id"], title, description or None,
                     fault_type or None, fault_phenomenon or None,
                     customer_address or None, contact_name or None, contact_phone or None,
                     client_ip or None, rep_prov or None, rep_city or None)
                )
                report_id = cur.lastrowid
            conn.commit()
            flash(f"上报成功，编号 #{report_id}", "success")
            return redirect(url_for("user_my_reports"))
        finally:
            conn.close()
    return render_template_string(
        EU_REPORT_HTML,
        current_user=current_user,
        fault_business_types=FAULT_BUSINESS_TYPES,
        fault_phenomena=FAULT_PHENOMENA,
        fault_types_need_phenomenon=list(EU_FAULT_TYPES_NEED_PHENOMENON),
        pc_data=EU_PC_DATA,
    )


@app.route("/user/my_reports")
@require_end_user
def user_my_reports():
    current_user = get_end_user()
    uid = current_user["user_id"]
    conn = get_db_connection()
    if not conn:
        flash("数据库连接失败", "danger")
        return render_template_string(EU_MY_REPORTS_HTML, current_user=current_user, reports=[], rated_tasks=[])
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.*, t.task_id AS linked_task_id, t.status AS linked_task_status,
                       t.rating_stars
                FROM issue_reports r
                LEFT JOIN maintenance_tasks t ON t.source_report_id = r.report_id
                WHERE r.user_id=%s ORDER BY r.created_at DESC
            """, (uid,))
            rows = cur.fetchall()
            reports = []
            for r in rows:
                r = dict(r)
                linked_status = r.get("linked_task_status")
                r["display_status"] = linked_status if linked_status else r["status"]
                r["ratable_task_id"] = (
                    r["linked_task_id"]
                    if r.get("linked_task_id") and linked_status == "已完成" and not r.get("rating_stars")
                    else None
                )
                reports.append(r)
            cur.execute("""
                SELECT t.task_id, t.title, t.status, t.rating_stars, t.rating_comment, t.rated_at
                FROM maintenance_tasks t
                WHERE t.reporter_user_id=%s AND t.rating_stars IS NOT NULL
                ORDER BY t.rated_at DESC
            """, (uid,))
            rated_tasks = cur.fetchall()
    finally:
        conn.close()
    return render_template_string(EU_MY_REPORTS_HTML, current_user=current_user, reports=reports, rated_tasks=rated_tasks)


@app.route("/user/rate/<int:task_id>", methods=["GET", "POST"])
@require_end_user
def user_rate_task(task_id):
    current_user = get_end_user()
    uid = current_user["user_id"]
    conn = get_db_connection()
    if not conn:
        flash("数据库连接失败", "danger")
        return redirect(url_for("user_my_reports"))
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM maintenance_tasks WHERE task_id=%s", (task_id,))
            task = cur.fetchone()
        if not task:
            flash("工单不存在", "danger")
            return redirect(url_for("user_my_reports"))
        if task.get("reporter_user_id") != uid:
            flash("无权评价此工单", "danger")
            return redirect(url_for("user_my_reports"))
        if task.get("status") != "已完成":
            flash("工单尚未完成，无法评价", "warning")
            return redirect(url_for("user_my_reports"))
        if task.get("rating_stars") is not None:
            flash("该工单已评价", "warning")
            return redirect(url_for("user_my_reports"))
        if request.method == "POST":
            try:
                stars = int(request.form.get("stars", 0))
            except ValueError:
                stars = 0
            if stars < 1 or stars > 5:
                flash("请选择1-5星评分", "danger")
                return redirect(url_for("user_rate_task", task_id=task_id))
            comment = request.form.get("comment", "").strip()
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO task_ratings (task_id, user_id, stars, comment) VALUES (%s,%s,%s,%s)",
                    (task_id, uid, stars, comment or None)
                )
                new_status = "已归档" if stars == 5 else "已完成"
                cur.execute(
                    "UPDATE maintenance_tasks SET rating_stars=%s, rating_comment=%s, rated_at=NOW(), status=%s WHERE task_id=%s",
                    (stars, comment or None, new_status, task_id)
                )
            conn.commit()
            flash("感谢您的五星好评！工单已归档" if stars == 5 else f"评价已提交（{stars}星）", "success")
            return redirect(url_for("user_my_reports"))
    finally:
        conn.close()
    return render_template_string(EU_RATE_HTML, current_user=current_user, task=task)


if __name__ == '__main__':
    if _APSCHEDULER_AVAILABLE:
        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(check_overdue_tasks, "interval", minutes=30, id="overdue_check")
        scheduler.start()
    app.run(debug=True)