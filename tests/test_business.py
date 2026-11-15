
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random,itertools
from pathlib import Path
from fractions import Fraction
from journal_check.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_fields(self):
        d=sample();d['unknown']=1;self.bad(d)

    def test_balances(self):
        r=analyze(sample());t=r['currencies'][0];a={x['account']:x for x in t['accounts']};self.assertEqual(a['cash']['normal_balance'],700);self.assertEqual(a['sales']['net_credit'],1000);self.assertEqual((t['total_debits'],t['total_credits'],t['net_debits'],t['net_credits']),(1300,1300,1000,1000))
    def test_currencies_separate(self):
        d=sample();e=copy.deepcopy(d['entries'][0]);e['id']='euro';e['currency']='EUR';d['entries'].append(e);r=analyze(d);self.assertEqual([x['currency'] for x in r['currencies']],['EUR','USD']);self.assertEqual(r['currencies'][0]['total_debits'],1000)
    def test_unbalanced(self):
        d=sample();d['entries'][0]['lines'][0]['amount']=999;self.bad(d)
    def test_duplicate_entry(self):
        d=sample();d['entries'].append(d['entries'][0]);self.bad(d)
    def test_unknown_account(self):
        d=sample();d['entries'][0]['lines'][0]['account']='missing';self.bad(d)
    def test_boolean_amount(self):
        d=sample();d['entries'][0]['lines'][0]['amount']=True;self.bad(d)
    def test_empty_entries(self):
        d=sample();d['entries']=[];self.assertEqual(analyze(d)['currencies'],[])
    def test_order(self):
        d=sample();d['accounts'].reverse();d['entries'].reverse();self.assertEqual(analyze(d),analyze(sample()))

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','journal_check.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_same_path_and_symlink(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
