"""Recover an exact source span only when an output joined whole literal clauses."""
import re

CLAUSE=re.compile(r'[^，,。；;！？!?\r\n]+')


def literal_span(quote,source):
    if quote.strip() in source:return quote
    parts=[m.group().strip() for m in CLAUSE.finditer(quote) if m.group().strip()]
    if len(parts)<2:return None
    original=list(CLAUSE.finditer(source));selected=[];cursor=0
    for part in parts:
        found=next((i for i in range(cursor,len(original)) if original[i].group().strip()==part),None)
        if found is None:return None
        selected.append(original[found]);cursor=found+1
    # Include omitted intervening clauses, rather than presenting joined text as a quote.
    span=source[selected[0].start():selected[-1].end()].strip()
    return span if len(span)<=3000 else None
