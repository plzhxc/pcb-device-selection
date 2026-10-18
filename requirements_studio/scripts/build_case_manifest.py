"""Re-read only the 27 registered natural-language prompt TXT files."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
ALLOWED=Path('/share/home/chenxin')

def main():
    catalog=json.loads((ROOT/'tests/phase4_case_catalog.json').read_text(encoding='utf-8'))
    records=[]
    for entry in catalog:
        path=Path(entry['source']).resolve()
        if not path.is_relative_to(ALLOWED) or path.suffix.lower()!='.txt' or '文生原理图' not in path.name or '提示词' not in path.name:
            raise ValueError('案例来源必须是授权目录中的提示词TXT。')
        content=path.read_bytes();checksum=hashlib.sha256(content).hexdigest()
        if checksum!=entry['sha256']:raise ValueError('案例原文已变化，需复核对应验收规则：'+entry['id'])
        prompt=content.decode('utf-8-sig').strip()
        if prompt!=entry['prompt']:raise ValueError('案例正文与登记版本不一致：'+entry['id'])
        records.append({**entry,'prompt':prompt})
    out=ROOT/'.runtime/phase3/cases.json';out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'已核对 {len(records)} 个原始提示词及SHA-256；未读取案例的电路文件。')

if __name__=='__main__':main()
