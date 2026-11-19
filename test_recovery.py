import json
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import scan

class Recovery(unittest.TestCase):
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
