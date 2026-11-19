import json
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import scan

class Recovery(unittest.TestCase):
    def test_stale_nonempty_tencent_falls_back_to_eastmoney(self):
        from datetime import date
        def frame(day):
            return pd.DataFrame({'date':[day],'open':[10.],'close':[11.],'high':[11.],'low':[9.]})
        with tempfile.TemporaryDirectory() as root,chdir(root), \
             patch.object(scan.ak,'stock_zh_a_hist_tx',return_value=frame('2026-11-17')), \
             patch.object(scan.ak,'stock_zh_a_hist_em',return_value=frame('2026-11-18')) as backup:
            bars=scan.history('000889','20260701','20261118','qfq',[date(2026,11,18)])
            backup.assert_called_once()
            self.assertIn(date(2026,11,18),bars.index)

    def test_single_history_outage_publishes_missing_without_false_empty_signal(self):
        calendar=pd.DataFrame({'trade_date':pd.to_datetime(['2026-11-18','2026-11-19'])})
        pool=pd.DataFrame({'代码':['000889'],'名称':['样本']})
        with tempfile.TemporaryDirectory() as root,chdir(root), \
             patch('sys.argv',['scan.py','--start','2026-11-18','--end','2026-11-18']), \
             patch.object(scan.ak,'calendar_trade_date_hist_sina',return_value=calendar), \
             patch.object(scan.ak,'stock_zt_pool_em',return_value=pool), \
             patch('scan.history',side_effect=ValueError('unavailable')):
            scan.main()
            report=json.loads(Path('reports/2026-11-18.json').read_text())
            self.assertEqual(report['status'],'incomplete')
            self.assertEqual(report['missing'],['000889'])
            self.assertIn('数据待补齐',Path('reports/2026-11-18.md').read_text())
            self.assertIn('未锁定排名',Path('reports/summary.md').read_text())

if __name__=='__main__':unittest.main()
