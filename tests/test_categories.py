import io
import json
from pathlib import Path
import tempfile
import unittest
import app

class CategoryTest(unittest.TestCase):
    def test_categories_migration_rename_and_deletion(self):
        old=app.DB
        try:
            with tempfile.TemporaryDirectory() as tmp:
                app.DB=Path(tmp)/'categories.db'
                app.initialize()
                def post(path,data):
                    handler=object.__new__(app.Handler)
                    body=json.dumps(data).encode()
                    handler.path=path
                    handler.headers={'Content-Length':str(len(body))}
                    handler.rfile=io.BytesIO(body)
                    result=[]
                    handler.reply=lambda data,status=200: result.append((status,data))
                    handler.do_POST()
                    return result[0]
                product=dict(sku='C1',name='商品',category='旧类别',unit='件',cost='1',price='2',minimum=5)
                self.assertEqual(post('/api/products',product)[0],201)
                app.initialize()
                app.initialize()
                with app.connect() as c:
                    category=c.execute('SELECT * FROM categories WHERE name=?',('旧类别',)).fetchone()
                    cid=category['id']
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM categories').fetchone()[0],1)
                self.assertEqual(post('/api/categories',{'name':' 饮料 '})[0],201)
                self.assertEqual(post('/api/categories',{'name':'饮料'})[0],400)
                for name in ('',' '*4,'a'*201):
                    self.assertEqual(post('/api/categories',{'name':name})[0],400)
                self.assertEqual(post(f'/api/categories/{cid}/edit',{'name':'饮料'})[0],400)
                self.assertEqual(post(f'/api/categories/{cid}/edit',{'name':'零食'})[0],201)
                with app.connect() as c:
                    row=c.execute('SELECT * FROM products').fetchone()
                    self.assertEqual(row['category'],'零食')
                    self.assertEqual(row['stock'],0)
                    pid=row['id']
                    c.execute('UPDATE products SET archived=1 WHERE id=?',(pid,))
                self.assertEqual(post(f'/api/categories/{cid}/delete',{})[0],400)
                with app.connect() as c:
                    c.execute('UPDATE products SET archived=0 WHERE id=?',(pid,))
                product['category']='饮料'
                self.assertEqual(post(f'/api/products/{pid}/edit',product)[0],201)
                self.assertEqual(post(f'/api/categories/{cid}/delete',{})[0],201)
                self.assertEqual(post(f'/api/categories/{cid}/edit',{'name':'不存在'})[0],400)
                app.initialize()
                with app.connect() as c:
                    self.assertEqual([r['name'] for r in c.execute('SELECT * FROM categories')],['饮料'])
        finally:
            app.DB=old
