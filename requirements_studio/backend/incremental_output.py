"""Normalize additive interface summaries without merging product conflicts."""
COLLECTION_FIELDS={'V3-5.3:F01','V3-5.3:F02','V3-5.3:F03'}

def normalize_interface_summaries(result):
    by_field={}
    for row in result.field_resolutions:
        for fid in row.field_ids:
            if fid not in by_field:by_field[fid]=[]
            by_field[fid].append(row)
    combined={};notes=[]
    for fid,rows in by_field.items():
        if len(rows)<2 or fid not in COLLECTION_FIELDS:continue
        requirements=[set(r.requirement_ids) for r in rows]
        # Additive independent endpoints are a collection. Two competing
        # statements about one endpoint must still go through model repair.
        if (any(r.state!='confirmed' or not r.source_quote for r in rows)
            or any(not ids for ids in requirements)
            or any(a&b for i,a in enumerate(requirements) for b in requirements[i+1:])):continue
        row=rows[-1].model_copy(deep=True);row.field_ids=[fid]
        row.requirement_ids=list(dict.fromkeys(rid for r in rows for rid in r.requirement_ids))
        row.conclusion='；'.join(dict.fromkeys(r.conclusion.rstrip('。；') for r in rows))+'。'
        if len(row.conclusion)>1800:continue
        combined[fid]=row
        notes.append({'path':'field_resolutions:'+fid,'kind':'additive_interface_summary','sources':[{'source_message_id':r.source_message_id,'source_quote':r.source_quote,'requirement_ids':r.requirement_ids} for r in rows]})
    if combined:
        updates=[];done=set()
        for row in result.field_resolutions:
            remaining=[fid for fid in row.field_ids if fid not in combined]
            if remaining:updates.append(row.model_copy(update={'field_ids':remaining},deep=True))
            for fid in row.field_ids:
                if fid in combined and fid not in done:updates.append(combined[fid]);done.add(fid)
        result.field_resolutions=updates
    return notes
