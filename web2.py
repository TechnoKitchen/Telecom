from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify, session
import pymysql
import os
import json
import hashlib
import secrets
from datetime import datetime
from functools import wraps
import urllib.request as _urlreq

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'a_secret_key_for_flask_flash_messages')

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "127.0.0.1"),
    "port": int(os.environ.get("DB_PORT", 3306)),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", "tlxsdy8823166"),
    "database": os.environ.get("DB_NAME", "telecom_maintenance"),
    "charset": 'utf8mb4',
    "cursorclass": pymysql.cursors.DictCursor
}

FAULT_BUSINESS_TYPES = ("宽带新装", "宽带修障", "宽带移机", "宽带提速", "IPTV新装", "IPTV修障", "电话新装", "电话修障", "智能组网", "设备更换", "线路维护")
FAULT_PHENOMENA = ("光衰过大", "ONU离线", "网速不达标", "IPTV卡顿", "WiFi覆盖差", "电话无声", "线路中断", "其他")
FAULT_TYPES_NEED_PHENOMENON = {"宽带修障", "IPTV修障", "电话修障", "线路维护"}


def get_db_connection():
    try:
        return pymysql.connect(**DB_CONFIG)
    except Exception as e:
        print(f"数据库连接失败: {e}")
        return None


# ── 密码工具 ──────────────────────────────────────────────
def _hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 260000)
    return f"pbkdf2:sha256:260000${salt}${h.hex()}"

def _check_password(password, stored):
    try:
        _, params = stored.split('$', 1) if '$' in stored else ('', stored)
        parts = stored.split('$')
        salt = parts[1]
        expected = parts[2]
        h = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), 260000)
        return h.hex() == expected
    except Exception:
        return False


# ── 会话工具 ──────────────────────────────────────────────
def get_end_user():
    uid = session.get("eu_id")
    if not uid:
        return None
    return {"user_id": uid, "real_name": session.get("eu_name", ""), "phone": session.get("eu_phone", "")}

def require_end_user(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not get_end_user():
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── IP 定位 ───────────────────────────────────────────────
def _ip_to_province_city(client_ip):
    if "," in client_ip:
        client_ip = client_ip.split(",")[0].strip()
    if client_ip in ("127.0.0.1", "::1") or client_ip.startswith("192.168.") or client_ip.startswith("10."):
        client_ip = ""
    try:
        url = f"http://ip-api.com/json/{client_ip}?lang=zh-CN&fields=status,regionName,city"
        req = _urlreq.Request(url, headers={"User-Agent": "TelecomMaintenance/1.0"})
        with _urlreq.urlopen(req, timeout=4) as resp:
            result = json.loads(resp.read().decode())
        if result.get("status") == "success":
            province = result.get("regionName", "")
            for s in ("省", "自治区", "特别行政区", "壮族", "回族", "维吾尔"):
                province = province.replace(s, "")
            city = result.get("city", "").replace("市", "")
            return province or None, city or None
    except Exception:
        pass
    return None, None


# ── DB 初始化 ─────────────────────────────────────────────
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

def ensure_rating_columns(conn):
    with conn.cursor() as cur:
        for col, defn in [
            ("reporter_user_id", "INT NULL"),
            ("rating_stars", "TINYINT NULL"),
            ("rating_comment", "TEXT NULL"),
            ("rated_at", "DATETIME NULL"),
        ]:
            cur.execute(f"SHOW COLUMNS FROM maintenance_tasks LIKE '{col}'")
            if not cur.fetchone():
                cur.execute(f"ALTER TABLE maintenance_tasks ADD COLUMN {col} {defn}")
    conn.commit()

def _init_db():
    conn = get_db_connection()
    if conn:
        try:
            ensure_end_users_table(conn)
            ensure_issue_reports_table(conn)
            ensure_task_ratings_table(conn)
            ensure_rating_columns(conn)
        finally:
            conn.close()


PC_DATA = {
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

_BASE_CSS = """
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
    @media (max-width: 480px) { .row { flex-direction: column; } }
"""

LOGIN_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>用户登录 - 装维报修</title><style>''' + _BASE_CSS + '''</style></head>
<body>
<header><div><h1>装维报修平台</h1><p>用户服务门户</p></div></header>
<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat, msg in messages %}<div class="flash flash-{{ cat }}">{{ msg }}</div>{% endfor %}{% endif %}
{% endwith %}
<div class="card">
    <h2 style="margin:0 0 20px;font-size:20px;color:#111827;">登录</h2>
    <form method="post">
        <div class="form-group">
            <label>用户名 <span class="required">*</span></label>
            <input type="text" name="username" maxlength="50" required autofocus>
        </div>
        <div class="form-group">
            <label>密码 <span class="required">*</span></label>
            <input type="password" name="password" required>
        </div>
        <button type="submit" class="btn btn-primary">登录</button>
    </form>
    <p style="margin:16px 0 0;text-align:center;font-size:14px;color:#6b7280;">
        还没有账号？<a href="{{ url_for('register') }}" class="link">立即注册</a>
    </p>
</div>
</div></body></html>'''

REGISTER_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>用户注册 - 装维报修</title><style>''' + _BASE_CSS + '''</style></head>
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
            <div class="form-group">
                <label>用户名 <span class="required">*</span></label>
                <input type="text" name="username" maxlength="50" placeholder="登录用，字母数字" required autofocus>
            </div>
            <div class="form-group">
                <label>真实姓名 <span class="required">*</span></label>
                <input type="text" name="real_name" maxlength="50" placeholder="您的真实姓名" required>
            </div>
        </div>
        <div class="form-group">
            <label>手机号码 <span class="required">*</span></label>
            <input type="tel" name="phone" maxlength="11" placeholder="11位手机号" required>
        </div>
        <div class="row">
            <div class="form-group">
                <label>密码 <span class="required">*</span></label>
                <input type="password" name="password" minlength="6" placeholder="至少6位" required>
            </div>
            <div class="form-group">
                <label>确认密码 <span class="required">*</span></label>
                <input type="password" name="password2" minlength="6" placeholder="再次输入密码" required>
            </div>
        </div>
        <button type="submit" class="btn btn-primary">注册</button>
    </form>
    <p style="margin:16px 0 0;text-align:center;font-size:14px;color:#6b7280;">
        已有账号？<a href="{{ url_for('login') }}" class="link">立即登录</a>
    </p>
</div>
</div></body></html>'''

REPORT_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>问题上报 - 装维报修</title><style>''' + _BASE_CSS + '''</style></head>
<body>
<header>
    <div><h1>装维报修平台</h1><p>请填写报修信息，我们将尽快安排处理</p></div>
    <div class="header-right">
        <span>{{ current_user.real_name }}</span>
        <a href="{{ url_for('my_reports') }}">我的上报</a>
        <a href="{{ url_for('logout') }}">退出</a>
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
            <div class="form-group">
                <label>联系人姓名 <span class="required">*</span></label>
                <input type="text" name="contact_name" maxlength="30" placeholder="您的姓名" required>
            </div>
            <div class="form-group">
                <label>联系电话 <span class="required">*</span></label>
                <input type="tel" name="contact_phone" maxlength="20" placeholder="手机号码" required>
            </div>
        </div>
        <div class="section-title">故障信息</div>
        <div class="form-group">
            <label>业务类型 <span class="required">*</span></label>
            <select name="fault_type" id="fault_type" onchange="onBizTypeChange(this.value)" required>
                <option value="">请选择</option>
                {% for ft in fault_business_types %}
                <option value="{{ ft }}">{{ ft }}</option>
                {% endfor %}
            </select>
        </div>
        <div class="form-group" id="phenomenon_row" style="display:none;">
            <label>故障现象</label>
            <select name="fault_phenomenon" id="fault_phenomenon">
                <option value="">请选择</option>
                {% for fp in fault_phenomena %}
                <option value="{{ fp }}">{{ fp }}</option>
                {% endfor %}
            </select>
        </div>
        <div class="form-group">
            <label>问题标题 <span class="required">*</span></label>
            <input type="text" name="title" maxlength="200" placeholder="简要描述问题，如：宽带无法上网" required>
        </div>
        <div class="form-group">
            <label>详细描述</label>
            <textarea name="description" placeholder="可选：描述故障现象、发生时间等"></textarea>
        </div>
        <div class="section-title">上门地址</div>
        <div class="form-group">
            <div class="row" style="margin-bottom:8px;">
                <select id="task_province" onchange="onTaskProvinceChange()">
                    <option value="">选择省份</option>
                </select>
                <select id="task_city" onchange="onTaskCityChange()">
                    <option value="">选择城市</option>
                </select>
                <button type="button" class="btn btn-loc" onclick="getLocation()">📍 定位</button>
            </div>
            <input type="text" id="task_detail" name="task_detail" maxlength="300" placeholder="详细地址（街道、门牌号等）">
            <input type="hidden" id="customer_address" name="customer_address">
            <small id="loc-status"></small>
        </div>
        <button type="submit" class="btn btn-primary" style="margin-top:8px;">提交上报</button>
    </form>
</div>
</div>
<script>
var PC = {{ pc_data|tojson }};
var NEED_PHENO = {{ fault_types_need_phenomenon|tojson }};
function onBizTypeChange(v) {
    document.getElementById('phenomenon_row').style.display = NEED_PHENO.indexOf(v) >= 0 ? '' : 'none';
}
function onTaskProvinceChange() {
    var pSel = document.getElementById('task_province');
    var cSel = document.getElementById('task_city');
    cSel.innerHTML = '<option value="">选择城市</option>';
    var prov = pSel.value;
    if (prov && PC[prov]) {
        PC[prov].forEach(function(ct) {
            var o = document.createElement('option'); o.value = ct; o.textContent = ct; cSel.appendChild(o);
        });
    }
    updateAddr();
}
function onTaskCityChange() { updateAddr(); }
function updateAddr() {
    var prov = document.getElementById('task_province').value;
    var city = document.getElementById('task_city').value;
    var detail = document.getElementById('task_detail').value;
    document.getElementById('customer_address').value = (prov||'') + (city||'') + (detail||'');
}
function getLocation() {
    var st = document.getElementById('loc-status');
    st.textContent = '定位中...'; st.style.color = '#6b7280';
    fetch('/api/ip_location').then(function(r){return r.json();}).then(function(d){
        if (!d.ok) throw new Error(d.message);
        var pSel = document.getElementById('task_province');
        if (d.province) { pSel.value = d.province; onTaskProvinceChange(); }
        if (d.city) { document.getElementById('task_city').value = d.city; updateAddr(); }
        st.textContent = '已定位：' + d.province + d.city; st.style.color = '#15803d';
    }).catch(function(e){ st.textContent = '定位失败：' + e.message; st.style.color = '#b91c1c'; });
}
document.addEventListener('DOMContentLoaded', function() {
    var pSel = document.getElementById('task_province');
    Object.keys(PC).forEach(function(p) {
        var o = document.createElement('option'); o.value = p; o.textContent = p; pSel.appendChild(o);
    });
    document.getElementById('task_detail').addEventListener('input', updateAddr);
});
</script>
</body></html>'''

MY_REPORTS_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>我的上报 - 装维报修</title><style>''' + _BASE_CSS + '''
    .reports-table { width:100%; border-collapse:collapse; margin-top:12px; }
    .reports-table th, .reports-table td { padding:10px 12px; text-align:left; border-bottom:1px solid #e5e7eb; font-size:14px; vertical-align:middle; }
    .reports-table th { background:#f9fafb; font-weight:600; color:#374151; }
    .btn-sm { padding:5px 12px; font-size:13px; border-radius:5px; border:none; cursor:pointer; text-decoration:none; display:inline-block; }
    .btn-rate { background:#f59e0b; color:white; }
    .btn-rate:hover { background:#d97706; }
    .btn-new { background:#3b82f6; color:white; }
    .btn-new:hover { background:#2563eb; }
    .stars-display { color:#f59e0b; font-size:15px; }
</style></head>
<body>
<header>
    <div><h1>装维报修平台</h1><p>我的上报记录</p></div>
    <div class="header-right">
        <span>{{ current_user.real_name }}</span>
        <a href="{{ url_for('report') }}">提交上报</a>
        <a href="{{ url_for('logout') }}">退出</a>
    </div>
</header>
<div class="container" style="max-width:900px;">
{% with messages = get_flashed_messages(with_categories=true) %}
{% if messages %}{% for cat, msg in messages %}<div class="flash flash-{{ cat }}">{{ msg }}</div>{% endfor %}{% endif %}
{% endwith %}

{% if ratable_tasks %}
<div class="card" style="background:#fef3c7;border:1px solid #fde68a;margin-bottom:16px;">
    <strong style="color:#92400e;">📋 您有 {{ ratable_tasks|length }} 个工单已完成，请评价后归档</strong>
    <div style="margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;">
    {% for t in ratable_tasks %}
        <a href="{{ url_for('rate_task', task_id=t.task_id) }}" class="btn-sm btn-rate">评价工单 #{{ t.task_id }}：{{ t.title[:20] }}</a>
    {% endfor %}
    </div>
</div>
{% endif %}

<div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <h2 style="margin:0;font-size:18px;">上报记录</h2>
        <a href="{{ url_for('report') }}" class="btn-sm btn-new">+ 新建上报</a>
    </div>
    {% if reports %}
    <table class="reports-table">
        <thead><tr>
            <th>编号</th><th>标题</th><th>业务类型</th><th>联系人</th><th>上报时间</th><th>状态</th>
        </tr></thead>
        <tbody>
        {% for r in reports %}
        <tr>
            <td>#{{ r.report_id }}</td>
            <td>{{ r.title }}</td>
            <td>{{ r.fault_type or '—' }}</td>
            <td>{{ r.contact_name or '—' }}{% if r.contact_phone %} {{ r.contact_phone }}{% endif %}</td>
            <td style="white-space:nowrap;">{{ r.created_at.strftime('%Y-%m-%d %H:%M') if r.created_at else '—' }}</td>
            <td>
                {% if r.status == 'pending' %}<span class="pill pill-pending">待处理</span>
                {% elif r.status == '处理中' %}<span class="pill" style="background:#dbeafe;color:#1e40af;">处理中</span>
                {% else %}<span class="pill pill-done">{{ r.status }}</span>{% endif %}
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <p style="text-align:center;color:#9ca3af;padding:24px 0;">暂无上报记录，<a href="{{ url_for('report') }}" class="link">立即上报</a></p>
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
</div></body></html>'''

RATE_HTML = '''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>工单评价 - 装维报修</title><style>''' + _BASE_CSS + '''
    .star-group { display:flex; flex-direction:row-reverse; justify-content:flex-end; gap:4px; margin:8px 0 16px; }
    .star-group input { display:none; }
    .star-group label { font-size:36px; color:#d1d5db; cursor:pointer; transition:color 0.1s; }
    .star-group input:checked ~ label,
    .star-group label:hover,
    .star-group label:hover ~ label { color:#f59e0b; }
</style></head>
<body>
<header>
    <div><h1>装维报修平台</h1><p>工单服务评价</p></div>
    <div class="header-right">
        <span>{{ current_user.real_name }}</span>
        <a href="{{ url_for('my_reports') }}">我的上报</a>
        <a href="{{ url_for('logout') }}">退出</a>
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
                <input type="radio" name="stars" id="star{{ i }}" value="{{ i }}" {% if i == 5 %}required{% endif %}>
                <label for="star{{ i }}" title="{{ i }}星">★</label>
                {% endfor %}
            </div>
            <small style="color:#92400e;">提示：五星评价后工单将自动归档</small>
        </div>
        <div class="form-group">
            <label>评价内容</label>
            <textarea name="comment" placeholder="请描述本次服务体验（可选）"></textarea>
        </div>
        <button type="submit" class="btn btn-primary">提交评价</button>
    </form>
    <p style="margin:12px 0 0;text-align:center;">
        <a href="{{ url_for('my_reports') }}" class="link">返回我的上报</a>
    </p>
</div>
</div></body></html>'''


# ── 路由 ──────────────────────────────────────────────────
@app.route("/api/ip_location")
def api_ip_location():
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or ""
    prov, ct = _ip_to_province_city(client_ip)
    if prov or ct:
        return jsonify({"ok": True, "province": prov or "", "city": ct or ""})
    return jsonify({"ok": False, "message": "IP定位失败"})


@app.route("/")
def index():
    if get_end_user():
        return redirect(url_for("report"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_end_user():
        return redirect(url_for("report"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        real_name = request.form.get("real_name", "").strip()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if not all([username, real_name, phone, password]):
            flash("请填写所有必填项", "danger")
            return redirect(url_for("register"))
        if len(phone) != 11 or not phone.isdigit():
            flash("手机号码格式不正确", "danger")
            return redirect(url_for("register"))
        if len(password) < 6:
            flash("密码至少6位", "danger")
            return redirect(url_for("register"))
        if password != password2:
            flash("两次密码不一致", "danger")
            return redirect(url_for("register"))
        conn = get_db_connection()
        if not conn:
            flash("数据库连接失败", "danger")
            return redirect(url_for("register"))
        try:
            ensure_end_users_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT user_id FROM end_users WHERE username=%s", (username,))
                if cur.fetchone():
                    flash("用户名已存在，请换一个", "danger")
                    return redirect(url_for("register"))
                cur.execute(
                    "INSERT INTO end_users (username, password, real_name, phone) VALUES (%s,%s,%s,%s)",
                    (username, _hash_password(password), real_name, phone)
                )
            conn.commit()
            flash("注册成功，请登录", "success")
            return redirect(url_for("login"))
        except Exception as e:
            conn.rollback()
            flash(f"注册失败：{e}", "danger")
            return redirect(url_for("register"))
        finally:
            conn.close()
    return render_template_string(REGISTER_HTML)


@app.route("/login", methods=["GET", "POST"])
def login():
    if get_end_user():
        return redirect(url_for("report"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        conn = get_db_connection()
        if not conn:
            flash("数据库连接失败", "danger")
            return redirect(url_for("login"))
        try:
            ensure_end_users_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM end_users WHERE username=%s AND status=1", (username,))
                user = cur.fetchone()
        finally:
            conn.close()
        if not user or not _check_password(password, user["password"]):
            flash("用户名或密码错误", "danger")
            return redirect(url_for("login"))
        session["eu_id"] = user["user_id"]
        session["eu_name"] = user["real_name"]
        session["eu_phone"] = user["phone"]
        next_url = request.args.get("next") or url_for("report")
        return redirect(next_url)
    return render_template_string(LOGIN_HTML)


@app.route("/logout")
def logout():
    session.pop("eu_id", None)
    session.pop("eu_name", None)
    session.pop("eu_phone", None)
    return redirect(url_for("login"))


@app.route("/report", methods=["GET", "POST"])
@require_end_user
def report():
    current_user = get_end_user()
    if request.method == "POST":
        conn = get_db_connection()
        if not conn:
            flash("数据库连接失败", "danger")
            return redirect(url_for("report"))
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
                return redirect(url_for("report"))
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO issue_reports
                       (user_id, title, description, fault_type, fault_phenomenon,
                        customer_address, contact_name, contact_phone)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (current_user["user_id"], title, description or None,
                     fault_type or None, fault_phenomenon or None,
                     customer_address or None, contact_name or None, contact_phone or None)
                )
                new_id = cur.lastrowid
            conn.commit()
            flash(f"上报成功，编号 #{new_id}，我们将尽快处理", "success")
            return redirect(url_for("my_reports"))
        except Exception as e:
            conn.rollback()
            flash(f"提交失败：{e}", "danger")
            return redirect(url_for("report"))
        finally:
            conn.close()
    return render_template_string(REPORT_HTML,
        current_user=current_user,
        fault_business_types=FAULT_BUSINESS_TYPES,
        fault_phenomena=FAULT_PHENOMENA,
        fault_types_need_phenomenon=list(FAULT_TYPES_NEED_PHENOMENON),
        pc_data=PC_DATA)


@app.route("/my_reports")
@require_end_user
def my_reports():
    current_user = get_end_user()
    uid = current_user["user_id"]
    conn = get_db_connection()
    if not conn:
        flash("数据库连接失败", "danger")
        return render_template_string(MY_REPORTS_HTML, current_user=current_user,
                                      reports=[], ratable_tasks=[], rated_tasks=[])
    try:
        ensure_issue_reports_table(conn)
        ensure_task_ratings_table(conn)
        ensure_rating_columns(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM issue_reports WHERE user_id=%s ORDER BY created_at DESC", (uid,))
            reports = cur.fetchall()
            cur.execute(
                "SELECT * FROM maintenance_tasks WHERE reporter_user_id=%s AND status='已完成' AND rating_stars IS NULL",
                (uid,)
            )
            ratable_tasks = cur.fetchall()
            cur.execute(
                "SELECT * FROM maintenance_tasks WHERE reporter_user_id=%s AND rating_stars IS NOT NULL ORDER BY rated_at DESC",
                (uid,)
            )
            rated_tasks = cur.fetchall()
    finally:
        conn.close()
    return render_template_string(MY_REPORTS_HTML, current_user=current_user,
                                  reports=reports, ratable_tasks=ratable_tasks, rated_tasks=rated_tasks)


@app.route("/rate/<int:task_id>", methods=["GET", "POST"])
@require_end_user
def rate_task(task_id):
    current_user = get_end_user()
    uid = current_user["user_id"]
    conn = get_db_connection()
    if not conn:
        flash("数据库连接失败", "danger")
        return redirect(url_for("my_reports"))
    task = None
    try:
        ensure_task_ratings_table(conn)
        ensure_rating_columns(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM maintenance_tasks WHERE task_id=%s", (task_id,))
            task = cur.fetchone()
        if not task:
            flash("工单不存在", "danger")
            return redirect(url_for("my_reports"))
        if task.get("reporter_user_id") != uid:
            flash("无权评价此工单", "danger")
            return redirect(url_for("my_reports"))
        if task.get("status") != "已完成":
            flash("工单尚未完成，无法评价", "warning")
            return redirect(url_for("my_reports"))
        if task.get("rating_stars") is not None:
            flash("该工单已评价", "warning")
            return redirect(url_for("my_reports"))
        if request.method == "POST":
            try:
                stars = int(request.form.get("stars", 0))
            except ValueError:
                stars = 0
            if stars < 1 or stars > 5:
                flash("请选择1-5星评分", "danger")
                return redirect(url_for("rate_task", task_id=task_id))
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
            if stars == 5:
                flash("感谢您的五星好评！工单已归档", "success")
            else:
                flash(f"评价已提交（{stars}星），工单保持完成状态", "success")
            return redirect(url_for("my_reports"))
    finally:
        conn.close()
    return render_template_string(RATE_HTML, current_user=current_user, task=task)


if __name__ == "__main__":
    _init_db()
    app.run(debug=True, port=5001)