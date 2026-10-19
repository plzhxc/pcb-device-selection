"""Consistent physical quantities across the current views and exports."""
import re


def metric_key(row):
    key=row.get('field_key','feature')
    if key=='switching_frequency' and re.search(r'NFC|RFID|射频|载波',row.get('text',''),re.I):return 'rf_frequency'
    return key


def constraint_value(row):
    text=row.get('text','').strip();unit=row.get('unit','')
    low,high=row.get('minimum'),row.get('maximum')
    number=lambda value:format(value,'g') if isinstance(value,(int,float)) else str(value)
    value=(number(low) if low==high else f'{number(low)}～{number(high)}') if low is not None and high is not None else ('≥ '+number(low) if low is not None else '≤ '+number(high)) if high is not None or low is not None else ''
    numeric=(value+' '+unit).strip()
    if not text:return numeric
    if not unit or re.search(r'\d\s*'+re.escape(unit)+r'(?![A-Za-z])',text,re.I):return text
    if low is not None and low==high:
        pattern=r'(?<![\d.])'+re.escape(number(low))+r'(?![\d.])'
        if re.search(pattern,text):return re.sub(pattern,lambda m:m.group()+' '+unit,text)
    return text+('（'+numeric+'）' if numeric else '（'+unit+'）')
