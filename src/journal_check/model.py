from .common import require,ident,number,rows
from .ledger import balance

def analyze(data):
    require(data,'accounts entries');accounts={};entries=[];seen=set()
    for a in rows(data['accounts'],1000):
        require(a,'id normal');aid=ident(a['id']);normal=ident(a['normal'])
        if aid in accounts or normal not in ('debit','credit'):raise ValueError('duplicate account or invalid normal side')
        accounts[aid]=normal
    if not accounts:raise ValueError('accounts required')
    for e in rows(data['entries'],5000):
        require(e,'id currency lines');eid=ident(e['id']);currency=ident(e['currency'])
        if eid in seen:raise ValueError('duplicate entry')
        seen.add(eid);lines=[]
        for l in rows(e['lines'],100):
            require(l,'account side amount');a=ident(l['account']);side=ident(l['side']);amount=number(l['amount'],1,10**12)
            if a not in accounts or side not in ('debit','credit'):raise ValueError('unknown account or invalid side')
            lines.append((a,side,amount))
        if len(lines)<2 or sum(v if s=='debit' else -v for a,s,v in lines)!=0:raise ValueError('unbalanced journal')
        entries.append((eid,currency,lines))
    return balance(accounts,entries)
