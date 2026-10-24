import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
import app

class InventoryTest(unittest.TestCase):
    def test_legacy_migration(self):
        old = app.DB
        try:
            with tempfile.TemporaryDirectory() as tmp:
                app.DB = Path(tmp) / 'legacy.db'
                with sqlite3.connect(app.DB) as c:
                    c.executescript("CREATE TABLE movements(id INTEGER PRIMARY KEY,product_id INTEGER,kind TEXT,quantity INTEGER,amount INTEGER NOT NULL,partner TEXT,note TEXT,created_at TEXT); INSERT INTO movements VALUES(1,1,'out',3,6000,'客户','','2026-10-20 08:00:00');")
                app.initialize()
                app.initialize()
                with app.connect() as c:
                    row = c.execute('SELECT * FROM movements').fetchone()
                    self.assertEqual(row['unit_price'], 2000)
                    self.assertEqual(row['amount'], 6000)
                    self.assertEqual(row['price_pending'], 0)
                    self.assertEqual(row['created_at'], '2026-10-20 08:00:00')
        finally:
            app.DB = old

    def test_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = app.DB
            app.DB = Path(tmp) / 'test.db'
            app.initialize()
            server = ThreadingHTTPServer(('127.0.0.1', 0), app.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            def request(path, data=None):
                req = urllib.request.Request(base+path, data=None if data is None else json.dumps(data).encode(), headers={'Content-Type':'application/json'})
                try:
                    with urllib.request.urlopen(req) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
            try:
                product = dict(sku='P001',name='<商品>',category='测试',unit='件',cost='12.35',price='20',minimum=5)
                self.assertEqual(request('/api/products', product)[0],201)
                self.assertEqual(request('/api/products', product)[0],400)
                state=request('/api/state')[1]
                pid=state['products'][0]['id']
                self.assertEqual(state['products'][0]['cost'],1235)
                m=dict(product_id=pid,kind='in',quantity=10,unit_price='12.35',partner='供应商')
                self.assertEqual(request('/api/movements',m)[0],201)
                m.update(kind='out',quantity=3,unit_price='20',partner='客户')
                self.assertEqual(request('/api/movements',m)[0],201)
                m['quantity']=8
                self.assertEqual(request('/api/movements',m)[0],400)
                m['quantity']=-1
                self.assertEqual(request('/api/movements',m)[0],400)
                m.update(quantity=1,unit_price='NaN')
                self.assertEqual(request('/api/movements',m)[0],400)
                app.initialize()
                state=request('/api/state')[1]
                self.assertEqual(state['products'][0]['stock'],7)
                self.assertEqual(len(state['movements']),2)
                self.assertEqual(state['movements'][0]['amount'],6000)
                # Deferred price is distinct from an explicit zero price.
                m.update(quantity=2,unit_price='',created_at='2026-10-20T09:30:00')
                self.assertEqual(request('/api/movements',m)[0],201)
                pending=request('/api/state')[1]['movements'][0]
                self.assertEqual(pending['price_pending'],1)
                self.assertIsNone(pending['unit_price'])
                self.assertEqual(pending['created_at'],'2026-10-20 09:30:00')
                edit='/api/movements/'+str(pending['id'])+'/edit'
                m.update(unit_price='15.25')
                self.assertEqual(request(edit,m)[0],201)
                state=request('/api/state')[1]
                self.assertEqual(state['products'][0]['stock'],5)
                self.assertEqual(state['movements'][0]['amount'],3050)
                self.assertEqual(state['movements'][0]['price_pending'],0)
                self.assertTrue(state['movements'][0]['updated_at'])
                # Repricing repeatedly must not deduct stock a second time.
                self.assertEqual(request(edit,m)[0],201)
                m.update(quantity=4,unit_price='0')
                self.assertEqual(request(edit,m)[0],201)
                self.assertEqual(request('/api/state')[1]['products'][0]['stock'],3)
                self.assertEqual(request('/api/state')[1]['movements'][0]['price_pending'],0)
                m['quantity']=100
                self.assertEqual(request(edit,m)[0],400)
                self.assertEqual(request('/api/state')[1]['products'][0]['stock'],3)
                m.update(quantity=1,created_at='2026-02-30T10:00')
                self.assertEqual(request(edit,m)[0],400)
                app.initialize()
                self.assertEqual(request('/api/state')[1]['products'][0]['stock'],3)
                self.assertEqual(len(request('/api/state')[1]['movements']),3)
                with urllib.request.urlopen(base+'/') as r:
                    self.assertIn('仓序',r.read().decode())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                app.DB=old

if __name__=='__main__':
    unittest.main()
