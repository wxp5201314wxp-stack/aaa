import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
import app

class CostingTest(unittest.TestCase):
    def setUp(self):
        self.old=app.DB
        self.tmp=tempfile.TemporaryDirectory()
        app.DB=Path(self.tmp.name)/'costs.db'
        app.initialize()
        self.assertEqual(self.post('/api/products',dict(sku='A',name='商品',category='通用',unit='件',cost='99')),201)

    def tearDown(self):
        app.DB=self.old
        self.tmp.cleanup()

    def post(self,path,data):
        h=object.__new__(app.Handler)
        body=json.dumps(data).encode()
        h.path=path;h.headers={'Content-Length':str(len(body))};h.rfile=io.BytesIO(body)
        result=[];h.reply=lambda data,status=200:result.append((status,data))
        h.do_POST()
        return result[0][0]

    def row(self,table,mid=1):
        with app.connect() as c:
            return dict(c.execute(f'SELECT * FROM {table} WHERE id=?',(mid,)).fetchone())

    def purchase(self,quantity,price,confirmed=True):
        self.assertEqual(self.post('/api/movements',dict(product_id=1,kind='in',quantity=quantity,unit_price=price,partner='供应商',confirmed=confirmed)),201)
        with app.connect() as c:
            return c.execute('SELECT MAX(id) FROM movements').fetchone()[0]

    def sale(self,quantity,price='',edit=None):
        data=dict(kind='out',items=[dict(product_id=1,quantity=quantity,unit_price=price)],partner='客户',freight='5')
        self.assertEqual(self.post(f'/api/movements/{edit}/edit' if edit else '/api/movements',data),201)
        with app.connect() as c:
            return edit or c.execute('SELECT MAX(id) FROM movements').fetchone()[0]

    def test_actual_receipts_weighting_and_frozen_cost_on_repricing(self):
        self.purchase(10,'10');self.purchase(10,'20')
        self.assertEqual(self.row('products')['inventory_value'],30000)
        sale=self.sale(4)
        self.assertEqual(self.row('movements',sale)['cost_amount'],6000)
        self.assertEqual(self.row('products')['inventory_value'],24000)
        self.assertEqual(self.row('movements',sale)['price_pending'],1)
        self.purchase(4,'30')
        self.assertEqual(self.row('products')['inventory_value'],36000)
        self.sale(4,'25',sale)
        self.assertEqual(self.row('movements',sale)['cost_amount'],6000)
        self.assertEqual(self.row('products')['inventory_value'],36000)
        self.assertEqual(self.row('movements',sale)['amount']-6000-500,3500)
        self.sale(6,'25',sale)
        self.assertEqual(self.row('movements',sale)['cost_amount'],9600)
        self.assertEqual(self.row('products')['inventory_value'],32400)
        self.sale(3,'25',sale)
        self.assertEqual(self.row('movements',sale)['cost_amount'],4800)
        self.assertEqual(self.row('products')['inventory_value'],37200)
        app.initialize()
        self.assertEqual(self.row('products')['inventory_value'],37200)
        self.assertEqual(self.row('movements',sale)['cost_amount'],4800)

    def test_pending_confirmation_cancellation_and_adjustments(self):
        mid=self.purchase(3,'0.01',False)
        self.assertEqual(self.row('products')['inventory_value'],0)
        self.assertEqual(self.post(f'/api/movements/{mid}/confirm',{}),201)
        self.assertEqual(self.row('products')['inventory_value'],3)
        self.assertEqual(self.post(f'/api/movements/{mid}/confirm',{}),201)
        self.assertEqual(self.row('products')['inventory_value'],3)
        self.assertEqual(self.post(f'/api/movements/{mid}/cancel',dict(reason='误录')),201)
        self.assertEqual(self.row('products')['inventory_value'],0)
        self.assertEqual(self.post('/api/products/1/stock',dict(expected_stock=0,stock=2,reason='盘盈')),201)
        self.assertEqual(self.row('products')['inventory_value'],19800)
        self.assertEqual(self.row('products')['cost_estimated'],1)
        self.assertEqual(self.post('/api/products/1/stock',dict(expected_stock=2,stock=0,reason='盘亏')),201)
        self.assertEqual(self.row('products')['inventory_value'],0)

    def test_fractional_average_conserves_cents_and_rejects_invalid_reversal(self):
        first=self.purchase(1,'0.01');self.purchase(2,'0.02')
        sale=self.sale(1,'0')
        self.assertEqual(self.row('movements',sale)['cost_amount'],2)
        self.assertEqual(self.row('products')['inventory_value'],3)
        second_sale=self.sale(1,'0')
        self.assertEqual(self.row('products')['inventory_value'],1)
        # Reversing the expensive receipt must not produce negative carrying value.
        self.assertEqual(self.post('/api/movements/2/cancel',dict(reason='退货')),400)
        self.assertEqual(self.row('products')['inventory_value'],1)
        self.assertEqual(self.post(f'/api/movements/{first}/cancel',dict(reason='退货')),201)
        self.assertEqual(self.row('products')['inventory_value'],0)
        self.assertEqual(self.row('products')['stock'],0)
        self.assertEqual(self.row('movements',second_sale)['cost_amount'],2)

    def test_legacy_backfill_uses_receipts_and_is_marked_estimated(self):
        with app.connect() as c:
            c.executescript('DROP TABLE movements; DROP TABLE products; CREATE TABLE products(id INTEGER PRIMARY KEY,sku TEXT,name TEXT,category TEXT,unit TEXT,cost INTEGER,price INTEGER,stock INTEGER,minimum INTEGER); CREATE TABLE movements(id INTEGER PRIMARY KEY,product_id INTEGER,kind TEXT,quantity INTEGER,amount INTEGER,partner TEXT,note TEXT,created_at TEXT);')
            c.execute("INSERT INTO products VALUES(1,'A','商品','通用','件',9900,20000,6,5)")
            c.execute("INSERT INTO movements VALUES(1,1,'in',10,10000,'供应商','','2026-10-20 10:00:00')")
            c.execute("INSERT INTO movements VALUES(2,1,'out',4,8000,'客户','','2026-10-21 10:00:00')")
        app.initialize();app.initialize()
        self.assertEqual(self.row('products')['inventory_value'],6000)
        self.assertEqual(self.row('products')['cost_estimated'],1)
        self.assertEqual(self.row('movements',2)['cost_amount'],4000)
        self.assertEqual(self.row('movements',2)['cost_estimated'],1)
