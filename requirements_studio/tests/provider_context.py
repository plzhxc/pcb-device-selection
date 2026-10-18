"""Restore compact quote anchors for deterministic legacy test providers only."""
def decode_quotes(context):
    table=context.get('evidence_quote_table',{})
    def expand(value):
        if isinstance(value,list):return [expand(v) for v in value]
        if not isinstance(value,dict):return value
        result={k:expand(v) for k,v in value.items() if k!='source_quote_ref'}
        if value.get('source_quote_ref') in table:result['source_quote']=table[value['source_quote_ref']]
        return result
    context['current_document']=expand(context['current_document'])
    return context
