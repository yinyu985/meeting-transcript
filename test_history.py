import pathlib
import tempfile
import unittest
import history_store as store


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = store.DB
        store.DB = pathlib.Path(self.temp.name) / 'history.sqlite3'

    def tearDown(self):
        store.DB = self.old
        self.temp.cleanup()

    def test_persistence_and_late_result(self):
        session = store.create()
        sid = session['id']
        store.append(sid, 'b', 2000, 1000, 'remote', '后一句')
        store.finish(sid)
        store.append(sid, 'a', 1000, 1000, 'mic', '停止后才返回的结果')
        store.append(sid, 'a', 1000, 1000, 'mic', '重复请求')
        row = store.detail(sid)
        self.assertIsNotNone(row['ended'])
        self.assertEqual([s['at'] for s in row['segments']], [1000, 2000])
        self.assertEqual(store.listing()[0]['segments'], 2)
        self.assertEqual(row['segments'][0]['text'], '停止后才返回的结果')
        store.finish(sid)
        self.assertEqual(store.detail(sid)['ended'], row['ended'])

    def test_empty_transcripts(self):
        sid = store.create()['id']
        for i, text in enumerate(['', '  \n\t', None]):
            store.append(sid, str(i), 1000, 1000, 'mic', text)
        # Old versions stored empty results: exclude these from listing and detail too.
        with store.connect() as db:
            db.execute('INSERT INTO segments VALUES(?,?,?,?,?,?)', ('legacy', sid, 1000, 1000, 'mic', ''))
        store.append(sid, 'valid', 2000, 1000, 'mic', ' 有文字 ')
        self.assertEqual(store.listing()[0]['segments'], 1)
        self.assertEqual([s['text'] for s in store.detail(sid)['segments']], ['有文字'])

    def test_delete_and_late_result(self):
        sid = store.create()['id']
        other = store.create()['id']
        store.append(sid, 'before-delete', 1000, 1000, 'mic', '待删除')
        store.delete(sid)
        store.append(sid, 'after-delete', 2000, 1000, 'mic', '在途结果')
        store.finish(sid)
        store.delete(sid)  # 重复请求安全
        self.assertIsNone(store.detail(sid))
        self.assertEqual([r['id'] for r in store.listing()], [other])
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM segments WHERE session_id=?', (sid,)).fetchone()[0], 0)
        with self.assertRaises(ValueError):
            store.delete('missing')

    def test_old_history_not_hidden(self):
        with store.connect() as db:
            db.executemany('INSERT INTO sessions VALUES(?,?,?)', [(str(i), i, i+1) for i in range(205)])
        rows = store.listing()
        self.assertEqual(len(rows), 205)
        self.assertEqual(rows[-1]['id'], '0')

    def test_unknown_session(self):
        self.assertIsNone(store.detail('missing'))
        with self.assertRaises(ValueError):
            store.append('missing', 's', 1000, 1000, 'mic', 'text')
        with self.assertRaises(ValueError):
            store.finish('missing')

    def test_session_isolation(self):
        a, b = store.create(), store.create()
        store.append(a['id'], 'x', 1000, 1000, 'mic', '独立会话')
        self.assertEqual(store.detail(b['id'])['segments'], [])


if __name__ == '__main__':
    unittest.main()
