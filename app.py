import json, os, sqlite3, re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).parent
DB = Path(os.environ.get('INVENTORY_DB', ROOT / 'data/inventory.db'))

def connect():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def initialize():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS products (
          id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          category TEXT NOT NULL, unit TEXT NOT NULL, cost INTEGER NOT NULL,
          price INTEGER NOT NULL, stock INTEGER NOT NULL DEFAULT 0 CHECK(stock>=0),
          minimum INTEGER NOT NULL DEFAULT 5);
        CREATE TABLE IF NOT EXISTS movements (
          id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
          kind TEXT NOT NULL CHECK(kind IN ('in','out')), quantity INTEGER NOT NULL CHECK(quantity>0),
          amount INTEGER NOT NULL, partner TEXT NOT NULL, note TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now','+8 hours')));
        ''')
        c.execute('''CREATE TABLE IF NOT EXISTS stock_adjustments (
            id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
            before_stock INTEGER NOT NULL, after_stock INTEGER NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL)''')
        c.execute('CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL)')
        c.execute('INSERT OR IGNORE INTO categories(name) SELECT DISTINCT category FROM products')
        product_columns = {r['name'] for r in c.execute('PRAGMA table_info(products)')}
        if 'archived' not in product_columns:
            c.execute('ALTER TABLE products ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
        columns = {r['name'] for r in c.execute('PRAGMA table_info(movements)')}
        if 'cancelled_at' not in columns:
            c.execute('ALTER TABLE movements ADD COLUMN cancelled_at TEXT')
        if 'cancel_reason' not in columns:
            c.execute("ALTER TABLE movements ADD COLUMN cancel_reason TEXT NOT NULL DEFAULT ''")
        if 'confirmed' not in columns:
            c.execute('ALTER TABLE movements ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 1')
        if 'confirmed_at' not in columns:
            c.execute('ALTER TABLE movements ADD COLUMN confirmed_at TEXT')
            c.execute("UPDATE movements SET confirmed_at=created_at WHERE kind='in'")
        if 'freight' not in columns:
            c.execute('ALTER TABLE movements ADD COLUMN freight INTEGER NOT NULL DEFAULT 0')
        if 'unit_price' not in columns:
            c.execute('ALTER TABLE movements ADD COLUMN unit_price INTEGER')
            c.execute('UPDATE movements SET unit_price=amount/quantity')
        if 'price_pending' not in columns:
            c.execute('ALTER TABLE movements ADD COLUMN price_pending INTEGER NOT NULL DEFAULT 0')
        if 'order_id' not in columns:
            c.execute('ALTER TABLE movements ADD COLUMN order_id INTEGER')
        if 'updated_at' not in columns:
            c.execute('ALTER TABLE movements ADD COLUMN updated_at TEXT')

        # Existing databases did not retain receipt-time cost snapshots.
        if 'inventory_value' not in product_columns:
            c.execute('ALTER TABLE products ADD COLUMN inventory_value INTEGER')
            c.execute('ALTER TABLE products ADD COLUMN cost_estimated INTEGER NOT NULL DEFAULT 0')
            for product in c.execute('SELECT * FROM products').fetchall():
                totals = c.execute("SELECT COALESCE(SUM(amount),0), COALESCE(SUM(quantity),0) FROM movements WHERE product_id=? AND kind='in' AND confirmed=1 AND cancelled_at IS NULL", (product['id'],)).fetchone()
                unit = Decimal(totals[0]) / totals[1] if totals[1] else Decimal(product['cost'])
                c.execute('UPDATE products SET inventory_value=?,cost_estimated=? WHERE id=?',
                          (round_cents(unit * product['stock']), int(product['stock'] > 0), product['id']))
        if 'cost_amount' not in columns:
            c.execute('ALTER TABLE movements ADD COLUMN cost_amount INTEGER')
            c.execute('ALTER TABLE movements ADD COLUMN cost_estimated INTEGER NOT NULL DEFAULT 0')
            for product in c.execute('SELECT * FROM products').fetchall():
                totals = c.execute("SELECT COALESCE(SUM(amount),0), COALESCE(SUM(quantity),0) FROM movements WHERE product_id=? AND kind='in' AND confirmed=1 AND cancelled_at IS NULL", (product['id'],)).fetchone()
                unit = Decimal(totals[0]) / totals[1] if totals[1] else Decimal(product['cost'])
                for row in c.execute("SELECT id,quantity FROM movements WHERE product_id=? AND kind='out'", (product['id'],)).fetchall():
                    c.execute('UPDATE movements SET cost_amount=?,cost_estimated=1 WHERE id=?', (round_cents(unit * row['quantity']), row['id']))

def business_time(value):
    if value is None:
        return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    value = str(value).replace('T', ' ')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?', value):
        raise ValueError('业务时间格式不正确')
    try:
        return datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M:%S')
    except ValueError:
        raise ValueError('业务时间无效')

def integer(value, label, minimum=0):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) < minimum:
        raise ValueError(label + '必须为有效整数')
    return int(value)

def money(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or n > 100000000 or n.as_tuple().exponent < -2:
            raise ValueError('金额须为非负数，最多两位小数')
        return int(n * 100)
    except InvalidOperation:
        raise ValueError('金额格式不正确')

def required(data, key, label):
    value = str(data.get(key, '')).strip()
    if not value or len(value) > 200:
        raise ValueError(label + '必填且不能超过200字')
    return value

def round_cents(value):
    return int(Decimal(value).quantize(Decimal('1'), rounding='ROUND_HALF_UP'))

def inventory_value(product):
    return product['inventory_value'] if product['inventory_value'] is not None else product['stock'] * product['cost']

def take_cost(value, quantity, stock):
    return value if quantity == stock else round_cents(Decimal(value) * quantity / stock)

def receive_stock(c, product, quantity, amount, estimated=False):
    value = inventory_value(product)
    inherited = bool(product['cost_estimated'] or product['inventory_value'] is None) if product['stock'] else False
    c.execute('UPDATE products SET stock=stock+?,inventory_value=?,cost_estimated=? WHERE id=?',
              (quantity, value + amount, int(inherited or estimated), product['id']))

def outbound_costs(c, old_rows, parsed):
    previous = {row['product_id']: row for row in old_rows}
    requested = {pid: (qty, price) for pid, qty, price in parsed}
    costs = {}
    for pid in set(previous) | set(requested):
        product = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
        if not product:
            raise ValueError('商品不存在')
        old = previous.get(pid)
        old_qty = old['quantity'] if old else 0
        old_cost = old['cost_amount'] if old else 0
        old_estimated = bool(old['cost_estimated']) if old else False
        qty = requested[pid][0] if pid in requested else 0
        if product['archived'] and qty and qty != old_qty:
            raise ValueError('商品已归档，新增业务或调整数量前请先恢复商品')
        delta = qty - old_qty
        value = inventory_value(product)
        estimated = bool(product['cost_estimated'] or product['inventory_value'] is None)
        if delta > product['stock']:
            raise ValueError(product['name'] + '库存不足，无法保存该数量')
        if delta > 0:
            extra = take_cost(value, delta, product['stock'])
            cost = old_cost + extra
            outgoing_estimated = old_estimated or estimated
            value -= extra
        elif delta < 0:
            cost = take_cost(old_cost, qty, old_qty)
            outgoing_estimated = old_estimated
            value += old_cost - cost
            estimated = estimated or old_estimated
        else:
            cost, outgoing_estimated = old_cost, old_estimated
        remaining = product['stock'] - delta
        c.execute('UPDATE products SET stock=?,inventory_value=?,cost_estimated=? WHERE id=?',
                  (remaining, value, int(estimated and remaining > 0), pid))
        costs[pid] = (cost, int(outgoing_estimated))
    return costs

def save_outbound(c, d, edit_id=None):
    items = d.get('items')
    if d.get('kind') != 'out' or not isinstance(items, list) or not 1 <= len(items) <= 200:
        raise ValueError('出库单须包含1至200项商品')
    partner = required(d, 'partner', '客户')
    created_at = business_time(d.get('created_at'))
    note = str(d.get('note', '')).strip()
    if len(note) > 500:
        raise ValueError('备注不能超过500字')
    freight = money(d.get('freight', 0))
    parsed, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('商品明细格式不正确')
        pid = integer(item.get('product_id'), '商品', 1)
        qty = integer(item.get('quantity'), '数量', 1)
        if pid in seen:
            raise ValueError('同一商品不能重复添加')
        seen.add(pid)
        raw = item.get('unit_price')
        price = None if raw is None or str(raw).strip() == '' else money(raw)
        parsed.append((pid, qty, price))
    c.execute('BEGIN IMMEDIATE')
    old_rows, order_id = [], None
    if edit_id:
        old = c.execute('SELECT * FROM movements WHERE id=?', (edit_id,)).fetchone()
        if not old or old['kind'] != 'out':
            raise ValueError('只能编辑已存在的销售出库单')
        order_id = old['order_id'] or old['id']
        old_rows = c.execute('SELECT * FROM movements WHERE order_id=? OR id=?', (order_id, order_id)).fetchall()
    costs = outbound_costs(c, old_rows, parsed)
    old_rows = sorted(old_rows, key=lambda row: row['id'])
    retained = []
    for index, (pid, qty, price) in enumerate(parsed):
        values = (qty, 0 if price is None else qty * price, price, int(price is None), partner,
                  note, created_at, business_time(None) if edit_id else None, freight if index == 0 else 0)
        old = old_rows[index] if index < len(old_rows) else None
        if old:
            mid = old['id']
            c.execute('UPDATE movements SET quantity=?,amount=?,unit_price=?,price_pending=?,partner=?,note=?,created_at=?,updated_at=?,freight=?,product_id=? WHERE id=?', values + (pid, mid,))
        else:
            cursor = c.execute("INSERT INTO movements(quantity,amount,unit_price,price_pending,partner,note,created_at,updated_at,freight,product_id,kind,confirmed) VALUES(?,?,?,?,?,?,?,?,?,?,'out',1)", values + (pid,))
            mid = cursor.lastrowid
        order_id = order_id or mid
        retained.append(mid)
        c.execute('UPDATE movements SET order_id=?,cost_amount=?,cost_estimated=? WHERE id=?', (order_id, *costs[pid], mid))
    for row in old_rows:
        if row['id'] not in retained:
            c.execute('DELETE FROM movements WHERE id=?', (row['id'],))

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'static'), **kwargs)

    def reply(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/api/state':
            with connect() as c:
                products = [dict(r) for r in c.execute('SELECT * FROM products ORDER BY id DESC')]
                movements = [dict(r) for r in c.execute('''SELECT m.*, p.name, p.sku, p.unit FROM movements m
                    JOIN products p ON p.id=m.product_id ORDER BY m.id DESC''')]
                adjustments = [dict(r) for r in c.execute('''SELECT a.*,p.name,p.sku,p.unit
                    FROM stock_adjustments a JOIN products p ON p.id=a.product_id ORDER BY a.id DESC''')]
                categories = [dict(r) for r in c.execute('SELECT * FROM categories ORDER BY name')]
            return self.reply({'products': products, 'movements': movements, 'adjustments': adjustments, 'categories': categories})
        return super().do_GET()

    def do_POST(self):
        category_match = re.fullmatch(r'/api/categories/([1-9][0-9]*)/(edit|delete)', self.path)
        product_match = re.fullmatch(r'/api/products/([1-9][0-9]*)/(edit|delete|restore)', self.path)
        cancel_match = re.fullmatch(r'/api/movements/([1-9][0-9]*)/cancel', self.path)
        confirm_match = re.fullmatch(r'/api/movements/([1-9][0-9]*)/confirm', self.path)
        stock_match = re.fullmatch(r'/api/products/([1-9][0-9]*)/stock', self.path)
        edit_match = re.fullmatch(r'/api/movements/([1-9][0-9]*)/edit', self.path)
        if self.path not in ('/api/products', '/api/movements', '/api/categories') and not edit_match and not stock_match and not confirm_match and not product_match and not category_match and not cancel_match:
            return self.reply({'error': '接口不存在'}, 404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 65536:
                raise ValueError('请求大小不正确')
            d = json.loads(self.rfile.read(length))
            if not isinstance(d, dict):
                raise ValueError('请求格式不正确')
            with connect() as c:
                if self.path == '/api/categories' or category_match:
                    c.execute('BEGIN IMMEDIATE')
                    if not category_match:
                        name = required(d, 'name', '类别名称')
                        if c.execute('SELECT 1 FROM categories WHERE name=?', (name,)).fetchone():
                            raise ValueError('类别名称已存在')
                        c.execute('INSERT INTO categories(name) VALUES(?)', (name,))
                    else:
                        category = c.execute('SELECT * FROM categories WHERE id=?', (int(category_match[1]),)).fetchone()
                        if not category:
                            raise ValueError('类别不存在，请刷新后重试')
                        if category_match[2] == 'edit':
                            name = required(d, 'name', '类别名称')
                            if c.execute('SELECT 1 FROM categories WHERE name=? AND id<>?', (name, category['id'])).fetchone():
                                raise ValueError('类别名称已存在')
                            c.execute('UPDATE categories SET name=? WHERE id=?', (name, category['id']))
                            c.execute('UPDATE products SET category=? WHERE category=?', (name, category['name']))
                        else:
                            if c.execute('SELECT 1 FROM products WHERE category=?', (category['name'],)).fetchone():
                                raise ValueError('该类别下还有商品（含已归档），请先修改商品类别再删除')
                            c.execute('DELETE FROM categories WHERE id=?', (category['id'],))
                elif product_match:
                    pid, action = int(product_match[1]), product_match[2]
                    c.execute('BEGIN IMMEDIATE')
                    product = c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
                    if not product:
                        return self.reply({'error':'商品不存在或已删除'},404)
                    if action == 'edit':
                        if product['archived']:
                            raise ValueError('请先恢复已归档商品，再编辑资料')
                        c.execute('INSERT OR IGNORE INTO categories(name) VALUES(?)', (required(d,'category','分类'),))
                        c.execute('''UPDATE products SET sku=?,name=?,category=?,unit=?,cost=?,price=?,minimum=?
                            WHERE id=?''',(required(d,'sku','商品编码'),required(d,'name','商品名称'),
                            required(d,'category','分类'),required(d,'unit','单位'),money(d.get('cost')),
                            money(d.get('price')),integer(d.get('minimum'),'预警库存'),pid))
                    elif action == 'restore':
                        c.execute('UPDATE products SET archived=0 WHERE id=?',(pid,))
                    else:
                        if product['stock'] != 0:
                            raise ValueError('该商品还有库存，请先处理库存，再删除')
                        has_history = c.execute('SELECT 1 FROM movements WHERE product_id=? LIMIT 1',(pid,)).fetchone()
                        has_adjustments = c.execute('SELECT 1 FROM stock_adjustments WHERE product_id=? LIMIT 1',(pid,)).fetchone()
                        if has_history or has_adjustments:
                            c.execute('UPDATE products SET archived=1 WHERE id=?',(pid,))
                        else:
                            c.execute('DELETE FROM products WHERE id=?',(pid,))
                elif cancel_match:
                    reason = required(d, 'reason', '撤销原因')
                    c.execute('BEGIN IMMEDIATE')
                    movement = c.execute('SELECT * FROM movements WHERE id=?', (int(cancel_match[1]),)).fetchone()
                    if not movement or movement['kind'] != 'in':
                        raise ValueError('只能撤销采购入库单')
                    if not movement['cancelled_at']:
                        if movement['confirmed']:
                            product = c.execute('SELECT * FROM products WHERE id=?', (movement['product_id'],)).fetchone()
                            if product['stock'] < movement['quantity']:
                                raise ValueError('当前库存不足以扣回入库数量，无法撤销')
                            value = inventory_value(product) - movement['amount']
                            remaining = product['stock'] - movement['quantity']
                            if value < 0 or (remaining == 0 and value != 0):
                                raise ValueError('库存成本已发生变化，无法按原采购金额撤销，请先核对库存成本')
                            c.execute('UPDATE products SET stock=?,inventory_value=?,cost_estimated=? WHERE id=?', (remaining, value, int(bool(product['cost_estimated']) and remaining > 0), product['id']))
                        now = business_time(None)
                        c.execute('UPDATE movements SET cancelled_at=?,cancel_reason=?,updated_at=? WHERE id=?',
                                  (now, reason, now, movement['id']))
                elif confirm_match:
                    confirmed_at = business_time(d.get('confirmed_at'))
                    c.execute('BEGIN IMMEDIATE')
                    movement = c.execute('SELECT * FROM movements WHERE id=?',(int(confirm_match[1]),)).fetchone()
                    if not movement or movement['kind'] != 'in':
                        raise ValueError('只能确认采购入库单')
                    if movement['cancelled_at']:
                        raise ValueError('已撤销的入库单不能确认入库')
                    if not movement['confirmed']:
                        product = c.execute('SELECT * FROM products WHERE id=?',(movement['product_id'],)).fetchone()
                        if product['archived']:
                            raise ValueError('商品已归档，请先恢复商品再确认入库')
                        receive_stock(c, product, movement['quantity'], movement['amount'])
                        c.execute('UPDATE movements SET confirmed=1,confirmed_at=?,updated_at=? WHERE id=?',
                                  (confirmed_at,business_time(None),movement['id']))
                elif stock_match:
                    target = integer(d.get('stock'), '调整后库存')
                    expected = integer(d.get('expected_stock'), '原库存')
                    reason = required(d,'reason','调整原因')
                    c.execute('BEGIN IMMEDIATE')
                    product = c.execute('SELECT * FROM products WHERE id=?',(int(stock_match[1]),)).fetchone()
                    if not product:
                        raise ValueError('商品不存在')
                    if product['archived']:
                        raise ValueError('商品已归档，请先恢复商品')
                    if product['stock'] != expected:
                        return self.reply({'error':'库存已被其他操作更新，请刷新后重试'},409)
                    if target == expected:
                        raise ValueError('调整后库存与原库存相同')
                    value = inventory_value(product)
                    if target > expected:
                        extra = take_cost(value, target - expected, expected) if expected else product['cost'] * (target - expected)
                        value += extra
                    else:
                        value -= take_cost(value, expected - target, expected)
                    c.execute('UPDATE products SET stock=?,inventory_value=?,cost_estimated=? WHERE id=?',(target,value,int(target > 0 and (target > expected or product['cost_estimated'])),product['id']))
                    c.execute('''INSERT INTO stock_adjustments(product_id,before_stock,after_stock,reason,created_at)
                        VALUES(?,?,?,?,?)''',(product['id'],expected,target,reason,business_time(None)))
                elif self.path == '/api/products':
                    c.execute('INSERT OR IGNORE INTO categories(name) VALUES(?)', (required(d,'category','分类'),))
                    c.execute('INSERT INTO products(sku,name,category,unit,cost,price,minimum) VALUES(?,?,?,?,?,?,?)', (
                        required(d,'sku','商品编码'), required(d,'name','商品名称'),
                        required(d,'category','分类'), required(d,'unit','单位'), money(d.get('cost',0)),
                        money(d.get('price',0)), integer(d.get('minimum',5),'预警库存')))
                    c.execute('UPDATE products SET inventory_value=0 WHERE id=last_insert_rowid()')
                elif 'items' in d:
                    save_outbound(c, d, int(edit_match[1]) if edit_match else None)
                else:
                    pid = integer(d.get('product_id'), '商品', 1)
                    qty = integer(d.get('quantity'), '数量', 1)
                    kind = d.get('kind')
                    if kind not in ('in','out'):
                        raise ValueError('业务类型无效')
                    confirmed = d.get('confirmed', True) if kind == 'in' else True
                    if not isinstance(confirmed, bool):
                        raise ValueError('确认入库状态必须为布尔值')
                    raw_price = d.get('unit_price')
                    pending = kind == 'out' and (raw_price is None or str(raw_price).strip() == '')
                    unit_price = None if pending else money(raw_price)
                    amount = 0 if pending else unit_price * qty
                    freight = money(d.get('freight',0)) if kind == 'out' else 0
                    created_at = business_time(d.get('created_at'))
                    partner = required(d,'partner','往来单位')
                    note = str(d.get('note','')).strip()
                    if len(note) > 500:
                        raise ValueError('备注不能超过500字')
                    c.execute('BEGIN IMMEDIATE')
                    old = None
                    if edit_match:
                        old = c.execute('SELECT * FROM movements WHERE id=?', (int(edit_match[1]),)).fetchone()
                        if not old or old['kind'] != 'out':
                            raise ValueError('只能编辑已存在的销售出库单')
                        if old['order_id'] is not None:
                            raise ValueError('请按整张出库单编辑商品明细')
                        if 'freight' not in d:
                            freight = old['freight']
                        if kind != 'out' or pid != old['product_id']:
                            raise ValueError('编辑销售单不能更换商品或业务类型')
                    p = c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
                    if not p:
                        raise ValueError('商品不存在')
                    if p['archived'] and (not old or old['quantity'] != qty):
                        raise ValueError('商品已归档，新增业务或调整数量前请先恢复商品')
                    delta = (old['quantity'] - qty) if old else (qty if kind == 'in' else -qty)
                    if kind == 'in' and not confirmed:
                        delta = 0
                    if p['stock'] + delta < 0:
                        raise ValueError('库存不足，无法保存该数量')
                    costs = None
                    if kind == 'out':
                        costs = outbound_costs(c, [old] if old else [], [(pid, qty, unit_price)])
                    elif confirmed:
                        receive_stock(c, p, qty, amount)
                    if old:
                        c.execute('''UPDATE movements SET quantity=?,amount=?,unit_price=?,price_pending=?,
                            partner=?,note=?,created_at=?,updated_at=?,freight=? WHERE id=?''',
                            (qty,amount,unit_price,int(pending),partner,note,created_at,business_time(None),freight,old['id']))
                    else:
                        c.execute('''INSERT INTO movements(product_id,kind,quantity,amount,partner,note,
                            created_at,unit_price,price_pending,freight,confirmed,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (pid,kind,qty,amount,partner,note,created_at,unit_price,int(pending),freight,int(confirmed),
                             created_at if kind == 'in' and confirmed else None))
                    if costs is not None:
                        mid = old['id'] if old else c.execute('SELECT last_insert_rowid()').fetchone()[0]
                        c.execute('UPDATE movements SET cost_amount=?,cost_estimated=? WHERE id=?', (*costs[pid], mid))
            return self.reply({'ok':True},201)
        except sqlite3.IntegrityError:
            return self.reply({'error':'商品编码已存在或数据不符合要求'},400)
        except (ValueError, TypeError, OverflowError) as e:
            return self.reply({'error':str(e)},400)
        except sqlite3.Error:
            return self.reply({'error':'数据库暂时不可用，请稍后重试'},503)

if __name__ == '__main__':
    initialize()
    host = os.environ.get('HOST','127.0.0.1')
    port = int(os.environ.get('PORT','8000'))
    print(f'进销存已启动：http://{host}:{port}', flush=True)
    ThreadingHTTPServer((host,port),Handler).serve_forever()
