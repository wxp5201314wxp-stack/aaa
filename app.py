import json, os, sqlite3
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
            return self.reply({'products': products, 'movements': movements})
        return super().do_GET()

    def do_POST(self):
        if self.path not in ('/api/products', '/api/movements'):
            return self.reply({'error': '接口不存在'}, 404)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length <= 0 or length > 65536:
                raise ValueError('请求大小不正确')
            d = json.loads(self.rfile.read(length))
            if not isinstance(d, dict):
                raise ValueError('请求格式不正确')
            with connect() as c:
                if self.path == '/api/products':
                    c.execute('INSERT INTO products(sku,name,category,unit,cost,price,minimum) VALUES(?,?,?,?,?,?,?)', (
                        required(d,'sku','商品编码'), required(d,'name','商品名称'),
                        required(d,'category','分类'), required(d,'unit','单位'), money(d.get('cost',0)),
                        money(d.get('price',0)), integer(d.get('minimum',5),'预警库存')))
                else:
                    pid = integer(d.get('product_id'), '商品', 1)
                    qty = integer(d.get('quantity'), '数量', 1)
                    kind = d.get('kind')
                    if kind not in ('in','out'):
                        raise ValueError('业务类型无效')
                    amount = money(d.get('unit_price')) * qty
                    partner = required(d,'partner','往来单位')
                    note = str(d.get('note','')).strip()
                    if len(note) > 500:
                        raise ValueError('备注不能超过500字')
                    c.execute('BEGIN IMMEDIATE')
                    p = c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
                    if not p:
                        raise ValueError('商品不存在')
                    if kind == 'out' and p['stock'] < qty:
                        raise ValueError('库存不足，当前可用库存：' + str(p['stock']))
                    c.execute('UPDATE products SET stock=stock+? WHERE id=?',(qty if kind=='in' else -qty,pid))
                    c.execute('INSERT INTO movements(product_id,kind,quantity,amount,partner,note) VALUES(?,?,?,?,?,?)',
                              (pid,kind,qty,amount,partner,note))
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
