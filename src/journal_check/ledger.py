from collections import defaultdict

def balance(accounts,entries):
    ledgers={}
    for eid,currency,lines in entries:
        totals=ledgers.setdefault(currency,{a:{'debit':0,'credit':0} for a in accounts})
        for a,side,amount in lines:totals[a][side]+=amount
    result=[]
    for currency,totals in sorted(ledgers.items()):
        accounts_out=[]
        for a,v in sorted(totals.items()):
            net=v['debit']-v['credit'];accounts_out.append({'account':a,**v,'net_debit':max(net,0),'net_credit':max(-net,0),'normal_balance':net if accounts[a]=='debit' else -net})
        result.append({'currency':currency,'accounts':accounts_out,'total_debits':sum(v['debit'] for v in totals.values()),'total_credits':sum(v['credit'] for v in totals.values()),'net_debits':sum(v['net_debit'] for v in accounts_out),'net_credits':sum(v['net_credit'] for v in accounts_out)})
    return {'entry_count':len(entries),'currencies':result}
