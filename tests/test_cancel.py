import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
import app

class CancellationTest(unittest.TestCase):
    def test_cancel_pending_posted_repeat_and_insufficient_stock(self):
        old=app.DB
        try:
            with tempfile.TemporaryDirectory() as tmp:
                app.DB=Path(tmp)/'cancel.db'
                app.initialize()
                def post(path,data):
                    h=object.__new__(app.Handler)
                    body=json.dumps(data).encode()
                    h.path=path;h.headers={'Content-Length':str(len(body))};h.rfile=io.BytesIO(body)
                    result=[];h.reply=lambda data,status=200:result.append((status,data))
                    h.do_POST()
                    return result[0][0]
                self.assertEqual(post('/api/products',dict(sku='A',name='商品',category='通用',unit='件')),201)
                purchase=dict(product_id=1,kind='in',quantity=5,unit_price='2',partner='供应商',confirmed=False)
                def stock():
                    with app.connect() as c:
                        return c.execute('SELECT stock FROM products WHERE id=1').fetchone()[0]
                def latest():
                    with app.connect() as c:
                        return dict(c.execute('SELECT * FROM movements ORDER BY id DESC LIMIT 1').fetchone())
                self.assertEqual(post('/api/movements',purchase),201)
                self.assertEqual(post('/api/movements/1/cancel',dict(reason='未到货')),201)
                self.assertEqual(stock(),0)
                self.assertEqual(post('/api/movements/1/confirm',{}),400)
                self.assertTrue(latest()['cancelled_at'])
                purchase['confirmed']=True
                self.assertEqual(post('/api/movements',purchase),201)
                self.assertEqual(stock(),5)
                self.assertEqual(post('/api/movements/2/cancel',dict(reason='')),400)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    self.assertEqual(list(pool.map(lambda _:post('/api/movements/2/cancel',dict(reason='误入库')),range(2))),[201,201])
                self.assertEqual(stock(),0)
                self.assertEqual(latest()['amount'],1000)
                self.assertEqual(latest()['cancel_reason'],'误入库')
                self.assertEqual(post('/api/movements',purchase),201)
                sale=dict(purchase,kind='out',quantity=2)
                self.assertEqual(post('/api/movements',sale),201)
                self.assertEqual(post('/api/movements/3/cancel',dict(reason='退回')),400)
                self.assertEqual(stock(),3)
                self.assertEqual(post('/api/movements/4/cancel',dict(reason='销售单')),400)
                self.assertEqual(post('/api/movements/999/cancel',dict(reason='不存在')),400)
                app.initialize()
                with app.connect() as c:
                    self.assertIsNone(c.execute('SELECT cancelled_at FROM movements WHERE id=3').fetchone()[0])
                    self.assertTrue(c.execute('SELECT cancelled_at FROM movements WHERE id=2').fetchone()[0])
        finally:
            app.DB=old
