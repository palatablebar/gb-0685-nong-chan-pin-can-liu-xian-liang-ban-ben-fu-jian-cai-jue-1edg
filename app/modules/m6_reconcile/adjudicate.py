from app.ledger import adjudicate

def execute(state,baseline=None,parameters=None):
    ledger=adjudicate(state,baseline,parameters)
    state['ledger']=ledger
    # 批次判级、样本计数与封存一律以重传裁决后的可发布批次为准。
    state['calculations']=ledger['accepted']
    state['business']=ledger['business']
    return state
