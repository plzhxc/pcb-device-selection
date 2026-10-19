/** Keep the exact saved confirmation available without a wall of command text. */
export function UserStatement({text}:{text:string}){
  if(text.startsWith('[参考资料] ')||text.startsWith('[采用参考核心器件方案]'))return <div className="attachment-statement"><strong>{text.split('\n')[0].replace('[参考资料] ','参考资料 · ').replace('[采用参考核心器件方案]','采用已展示的核心器件方案')}</strong><p>{text.startsWith('[参考资料]')?'助手将从文件提取器件和连接，结合实际用途给出方案。':'保留本版功能，按已展示的方案继续核对资料与实现。'}</p><details><summary>查看文件提取内容与本次操作范围</summary><pre>{text}</pre></details></div>;
  const adoption=text.startsWith('确认本版功能模块方案：\n')&&text.includes('\n我采用上述功能类别、用途和数量。')&&text.endsWith('这不是电路已验证。');
  if(!adoption)return <span>{text}</span>;
  const names=text.split('\n').flatMap(line=>{const match=line.match(/^- (.+?)：/);return match?[match[1]]:[];});
  return <div className="module-adoption-statement"><strong>采用这组功能模块方案</strong><div>{names.map((name,i)=><span key={i}>{name}</span>)}</div><p>保留已经明确的产品要求，具体实现交给设计阶段。</p><details><summary>查看本次确认的完整范围</summary><p>{text}</p></details></div>;
}
