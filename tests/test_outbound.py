import tempfile
import unittest
from pathlib import Path
import app

class OutboundTest(unittest.TestCase):
    def test_multiple_products_atomic_edit_and_freight(self):
        old = app.DB
        try:
            with tempfile.TemporaryDirectory() as tmp:
                app.DB = Path(tmp) / 'test.db'
                app.initialize()
                with app.connect() as c:
                    for i in range(1, 4):
                        c.execute("INSERT INTO products(id,sku,name,category,unit,cost,price,stock) VALUES(?,?,?,'通用','件',100,200,10)", (i,str(i),'商品'+str(i)))
                def save(items, edit=None):
                    with app.connect() as c:
                        app.save_outbound(c, dict(kind='out',partner='客户',freight='8.25',items=items), edit)
                a=dict(product_id=1,quantity=3,unit_price='2')
                b=dict(product_id=2,quantity=4,unit_price='')
                save([a,b])
                with app.connect() as c:
                    rows=c.execute('SELECT * FROM movements ORDER BY id').fetchall()
                    root=rows[0]['id']
                    self.assertEqual([r['order_id'] for r in rows],[root,root])
                    self.assertEqual(sum(r['freight'] for r in rows),825)
                    self.assertEqual(rows[0]['amount'],600)
                    self.assertEqual(rows[1]['price_pending'],1)
                with self.assertRaises(ValueError):
                    save([a,dict(b,quantity=100)],root)
                with app.connect() as c:
                    self.assertEqual([r[0] for r in c.execute('SELECT stock FROM products ORDER BY id')],[7,6,10])
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM movements').fetchone()[0],2)
                save([dict(b,quantity=2,unit_price='3'),dict(product_id=3,quantity=1,unit_price='0')],root)
                save([dict(b,quantity=2,unit_price='3'),dict(product_id=3,quantity=1,unit_price='0')],root)
                with app.connect() as c:
                    self.assertEqual([r[0] for r in c.execute('SELECT stock FROM products ORDER BY id')],[10,8,9])
                    self.assertEqual(c.execute('SELECT order_id FROM movements WHERE id=?',(root,)).fetchone()[0],root)
                for items in ([],[a,a],[a,dict(b,quantity=100)]):
                    with self.assertRaises(ValueError):
                        save(items)
                app.initialize()
                with app.connect() as c:
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM movements').fetchone()[0],2)
        finally:
            app.DB=old
