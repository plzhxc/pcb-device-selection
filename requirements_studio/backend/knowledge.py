"""Bounded read-only adapters for the existing dataS catalog and Milvus library.

No dataS import/publish module or model-authored SQL is executed here.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .device_domain import digest
from .engineering_checks import normalize_attribute

DATA_ROOT = Path('/share/home/chenxin/Worklib/database/dataS')
SEARCH = 'dsearch_v_device_search_profiled_incremental'
ATTRS = 'dsearch_v_device_attribute_priority_incremental'
TARGET_UUID = 'c6eb0e30-869e-11f1-83c2-9cc2c44b03c7'
SOURCE_UUID = 'b740457c-372b-11f1-b5d4-0242ac110003'
SUPPLIER = re.compile(r'^[CS][0-9]{1,20}$', re.I)
PART = re.compile(r'(?<![A-Za-z0-9])(?:[A-Za-z][A-Za-z0-9]*(?:[-_+/.][A-Za-z0-9]+)*)(?![A-Za-z0-9])')
NON_PARTS = re.compile(r'^(?:CORTEX(?:-[AMR][0-9][A-Z0-9]*)?|USB(?:[1-4](?:\.[0-9])?)?|WIFI|WI-FI|BLUETOOTH|DDR[1-5]?|LPDDR[1-5]?|PCI[E]?(?:GEN[1-7])?|GEN[1-7]|SATA|HDMI|I2[CS]|SPI|UART|'
                       r'RS[-]?(?:232|485)|CAN|ISO|NTAG|MIFARE|SPS|MSPS|KB|MB|GB|GHZ|MHZ|KHZ|HZ|'
                       r'BTL|DSP|FPU|PWM|ADC|DAC|TYPE|MIPI|FPC|HDR|SMA|RJ|SIM|QC|PD|CC|AA|AAA|'
                       r'RLD|GPIF|OTG|THD|TA|TJ|XH|IP[0-9]{1,2})$', re.I)
INTERNAL_ID = re.compile(r'(?:(?:REQ|REF|MOD|M)-[0-9]+|(?:EV|CHK)-[a-f0-9]{12,64}|V3-[0-9]+(?:\.[0-9]+)*)',re.I)
_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix='device-read')


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).strip().upper().split())


def mentions(text):
    result = []
    for match in PART.finditer(text):
        token = match.group().rstrip('/.+-')
        if (len(token) < 4 or not any(x.isdigit() for x in token) or NON_PARTS.fullmatch(token) or INTERNAL_ID.fullmatch(token)
                or re.fullmatch(r'(?:ISO14443[A-B]?(?:/[AB])?|ISO18092|NTAG21x|S50/S70/Ultralight/Desfire)',token,re.I)):
            continue
        if token not in result:
            result.append(token)
    return result[:80]


def safe(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str, allow_nan=False))


def _env_file(path):
    values = {}
    if path.is_file():
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, val = line.split('=', 1); values[key.strip()] = val.strip().strip('"').strip("'")
    return values


class KnowledgeUnavailable(RuntimeError):
    pass


class Catalog:
    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(4)
        self.enabled = os.environ.get('REQUIREMENTS_STUDIO_KNOWLEDGE', '1') != '0'

    def cached(self, key, factory, ttl=300):
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        value = factory()
        with self._lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    @contextlib.contextmanager
    def connection(self, source=False):
        with self._slots:
            with self._connection(source) as conn:yield conn

    @contextlib.contextmanager
    def _connection(self, source=False):
        if not self.enabled:
            raise KnowledgeUnavailable('资料检索已在此隔离环境关闭。')
        import mysql.connector
        if source:
            env = _env_file(DATA_ROOT / 'config/source.local.env')
            needed = ('DB_HOST', 'MYSQL_PORT', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE')
            if any(k not in env for k in needed):
                raise KnowledgeUnavailable('源目录连接配置缺失。')
            params = dict(host=env['DB_HOST'], port=int(env['MYSQL_PORT']), user=env['MYSQL_USER'],
                          password=env['MYSQL_PASSWORD'], database=env['MYSQL_DATABASE'])
        else:
            params = dict(host='localhost', unix_socket='/share/home/chenxin/my-database/run/mysql.sock',
                          port=3308, user='pcb_dev', password='', database='device_parameters_dev')
        conn = None
        try:
            conn = mysql.connector.connect(**params, connection_timeout=3, read_timeout=6,
                                           write_timeout=6, autocommit=False, charset='utf8mb4')
            with conn.cursor(dictionary=True) as cur:
                cur.execute('SET SESSION TRANSACTION READ ONLY')
                cur.execute('START TRANSACTION READ ONLY')
                cur.execute('SET SESSION MAX_EXECUTION_TIME=5000')
                cur.execute('SELECT @@server_uuid AS server_uuid,DATABASE() AS db,@@session.transaction_read_only AS ro')
                identity = cur.fetchone()
            if identity['server_uuid'] != (SOURCE_UUID if source else TARGET_UUID) or not identity['ro'] or identity['db'] != params['database']:
                raise KnowledgeUnavailable('资料来源身份或只读状态不符合配置，已停止查询。')
            yield conn
        except KnowledgeUnavailable:
            raise
        except ValueError:
            # A missing/withdrawn identity is a domain result from the caller of
            # this context manager, not a failure to connect to the database.
            raise
        except Exception as exc:
            raise KnowledgeUnavailable('器件源暂不可用（' + type(exc).__name__ + '），已保存需求保留。') from exc
        finally:
            if conn is not None:
                try: conn.rollback()
                except Exception: pass
                conn.close()

    @staticmethod
    def query(conn, sql, params=()):
        if not re.match(r'^\s*(SELECT|SHOW|EXPLAIN)\b', sql, re.I):
            raise ValueError('只允许固定只读查询。')
        with conn.cursor(dictionary=True) as cur:
            cur.execute(sql, params)
            return safe(cur.fetchall())

    def status(self, refresh=False):
        def read():
            start = time.monotonic()
            try:
                with self.connection() as conn:
                    rel = self.query(conn, "SELECT release_id,release_code,release_status,published_at FROM drelease_incremental_release WHERE release_status='PUBLISHED' ORDER BY release_id DESC LIMIT 1")
                    # The published UNION view includes large text columns. MySQL
                    # materializes them even for this identity-only diagnostic,
                    # which can exhaust its temporary tables while point queries
                    # still work. Read the same two published streams directly.
                    identities = self.query(conn, 'SELECT device_id,part_number_raw FROM dsearch_v_device_search_active')
                    identities += self.query(conn, "SELECT s.device_id,s.part_number_raw FROM drelease_search_document s JOIN drelease_incremental_release r ON s.release_id=r.release_id WHERE r.release_status='PUBLISHED' AND s.status='PUBLISHED'")
                    manual = self.query(conn, "SELECT COUNT(*) AS count FROM dmanual_device_ingest_record WHERE record_status='PUBLISHED'")
                result = {'available': True, 'read_only': True, 'release': rel[0] if rel else {},
                          'published_devices': len(identities), 'manual_records': manual[0]['count'],
                          'checked_at': now(), 'freshness': 'not_checked', 'errors': []}
                try:
                    with self.connection(source=True) as conn:
                        src = self.query(conn, 'SELECT device_id,device_name FROM devices')
                    a = {int(r['device_id']): r['device_name'] for r in src}
                    b = {int(r['device_id']): r['part_number_raw'] for r in identities}
                    counts = {'new': len(set(a) - set(b)), 'missing': len(set(b) - set(a)),
                              'renamed': sum(a[k].strip() != b[k].strip() for k in set(a) & set(b))}
                    result.update(source_devices=len(a), deltas=counts,
                                  freshness='stale' if sum(counts.values()) else 'identity_fresh')
                except KnowledgeUnavailable:
                    result['errors'].append('源身份新鲜度暂不可检查；发布数据仍可检索。')
                result['elapsed_ms'] = round((time.monotonic()-start)*1000)
                return result
            except KnowledgeUnavailable as exc:
                return {'available': False, 'read_only': True, 'checked_at': now(), 'error': str(exc),
                        'freshness': 'unavailable', 'published_devices': None}
        if refresh:
            with self._lock: self._cache.pop(('status',), None)
        return self.cached(('status',), read, ttl=300)

    def _public_identity(self, row, manual=False):
        number = int(row['record_id'] if manual else row['device_id'])
        raw = row.get('raw_device_json') or {}
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except ValueError: raw = {}
        return {'device_key': f'datas:{"manual-record" if manual else "source-device"}:{number}',
                'record_id': str(number), 'model': row.get('part_number', row.get('part_number_raw', '')),
                'manufacturer': row.get('manufacturer', row.get('manufacturer_raw', '')) or '',
                'supplier_part': raw.get('supplier_part') or row.get('supplier_part') or '',
                'supplier_namespace': 'catalog', 'package': '', 'leaf_code': row.get('leaf_code'),
                'leaf_name': row.get('leaf_name_zh') or '', 'taxonomy_path': row.get('taxonomy_path', ''),
                'device_form': row.get('device_form', ''), 'component_role': row.get('component_role', ''),
                'source_locator': row.get('source_locator', row.get('source_record_locator', '')),
                'document_locator': row.get('document_locator', row.get('milvus_source_locator', '')),
                'checksum': row.get('document_checksum'), 'origin': 'manual_published' if manual else 'published',
                'build_id': str(row.get('build_id') or ''),
                'overlay_release_id': str(row.get('overlay_release_id') or ''),
                'assignment_status': row.get('assignment_status', ''),
                'core_level': row.get('phase2_core_level', row.get('core_level', 'unknown')),
                'identity_warning': ''}

    def search(self, text, mode='auto', limit=8, offset=0, leaf_codes=None):
        text = str(text).strip()
        if not 1 <= limit <= 20 or not 0 <= offset <= 200 or len(text) > 160:
            raise ValueError('查询长度、页码或条数超出范围。')
        if mode not in ('auto', 'exact', 'prefix', 'category'):
            raise ValueError('检索方式无效。')
        if not text and not leaf_codes:
            raise ValueError('请输入型号、编号或类别。')
        return self.cached(('search', normalize(text), mode, limit, offset, tuple(leaf_codes or [])),
                           lambda: self._search(text, mode, limit, offset, leaf_codes), ttl=120)

    def _search(self, text, mode, limit, offset, leaf_codes):
        key = normalize(text); exact_supplier = bool(SUPPLIER.fullmatch(key))
        source_hits = []; warning = ''
        if exact_supplier:
            with self.connection(source=True) as conn:
                source_hits = self.query(conn, 'SELECT device_id,device_name,supplier_part FROM devices WHERE supplier_part=%s ORDER BY device_id LIMIT 21', (key,))
        with self.connection() as conn:
            manual_base="record_status='PUBLISHED' AND "
            if mode == 'category' or leaf_codes:
                leaves=list(leaf_codes or [])
                if len(leaves)>500 or any(not re.fullmatch(r'[A-Za-z0-9_]+',str(x)) for x in leaves):
                    raise ValueError('分类过滤范围或代码无效。')
                if leaves:
                    predicate='leaf_code IN ('+','.join('%s' for _ in leaves)+')'
                    pub_pred,manual_pred=predicate,manual_base+predicate
                    pub_params=manual_params=tuple(leaves)
                else:
                    pub_pred='leaf_name_zh LIKE %s';pub_params=('%'+text.replace('%','\\%').replace('_','\\_')+'%',)
                    manual_pred=manual_base+'1=0';manual_params=()
                match_mode='category'
            elif exact_supplier:
                ids=[r['device_id'] for r in source_hits]
                pub_pred='device_id IN ('+','.join('%s' for _ in ids)+')' if ids else '1=0';pub_params=tuple(ids)
                manual_pred=manual_base+"JSON_UNQUOTE(JSON_EXTRACT(raw_device_json,'$.supplier_part'))=%s";manual_params=(key,)
                match_mode='supplier_exact'
            else:
                pub_pred='part_number_normalized=%s';pub_params=(key,)
                manual_pred=manual_base+'part_number_normalized=%s';manual_params=(key,)
                match_mode='model_exact'
            def counts_and_page():
                count=self.query(conn,f'SELECT COUNT(*) AS total FROM {SEARCH} WHERE '+pub_pred,pub_params)[0]['total']
                rows=self.query(conn,f'SELECT * FROM {SEARCH} WHERE '+pub_pred+' ORDER BY part_number_normalized,device_id LIMIT %s OFFSET %s',(*pub_params,limit+1,offset))
                space=max(0,limit+1-len(rows))
                manual_rows=self.query(conn,'SELECT * FROM dmanual_device_ingest_record WHERE '+manual_pred+' ORDER BY record_id LIMIT %s OFFSET %s',(*manual_params,space,max(0,offset-int(count)))) if space else []
                return rows,manual_rows,int(count)
            rows,manual_rows,published_count=counts_and_page()
            if not rows and not manual_rows and published_count==0 and mode in ('auto','prefix') and not exact_supplier:
                pattern=key.replace('%','\\%').replace('_','\\_')+'%'
                pub_pred='part_number_normalized LIKE %s';pub_params=(pattern,)
                manual_pred=manual_base+'part_number_normalized LIKE %s';manual_params=(pattern,)
                rows,manual_rows,published_count=counts_and_page();match_mode='prefix_related'
        raw_items=[self._public_identity(r) for r in rows]+[self._public_identity(r,True) for r in manual_rows]
        has_more=len(raw_items)>limit
        items=raw_items[:limit]
        # Read distinguishing identity columns for the whole page, rather than
        # asking the customer to guess between identical-looking records.
        page_ids=[int(i['record_id']) for i in items if i['origin']=='published']
        if page_ids:
            marks=','.join('%s' for _ in page_ids)
            try:
                with self.connection(source=True) as conn:
                    sources=self.query(conn,f'SELECT device_id,device_name,supplier_part FROM devices WHERE device_id IN ({marks})',tuple(page_ids))
                with self.connection() as conn:
                    packages=self.query(conn,f"SELECT device_id,field_value_text FROM {ATTRS} WHERE device_id IN ({marks}) AND field_key IN ('package','encapsulation_specification') AND is_missing=0 ORDER BY priority_rank LIMIT 80",tuple(page_ids))
                by_id={str(s.get('device_id')):s for s in sources}
                for item in items:
                    source=by_id.get(item['record_id'],{})
                    if source and normalize(source.get('device_name',''))==normalize(item['model']):
                        item['supplier_part']=source.get('supplier_part') or item['supplier_part']
                    item['package']=next((p.get('field_value_text') or '' for p in packages if str(p.get('device_id'))==item['record_id']),item['package'])
            except KnowledgeUnavailable:warning='部分采购号或封装暂不可核对；保留来源记录，不自动合并身份。'
        # A verified identical supplier identity may have two source records. Preserve both locators.
        dedup = {}
        for item in items:
            identity = (item['model'], item['manufacturer'], item['supplier_part'],item.get('checksum'),item.get('build_id')) if item['supplier_part'] and item['manufacturer'] else (item['device_key'],)
            if identity in dedup:
                dedup[identity].setdefault('alternate_records', []).append(item['device_key'])
            else: dedup[identity] = item
        items = list(dedup.values())
        state = 'not_found' if not items else 'matched' if offset==0 and len(items) == 1 and not has_more and match_mode in ('model_exact', 'supplier_exact') else 'ambiguous'
        return {'query': text, 'status': state, 'match_mode': match_mode, 'items': items[:limit],
                'has_more': has_more and offset+limit<=200, 'page_budget_exhausted':has_more and offset+limit>200, 'offset': offset, 'next_offset': offset+limit,
                'checked_at': now(), 'read_only': True, 'warning': warning}

    def device(self, key):
        match = re.fullmatch(r'datas:(source-device|manual-record):([1-9][0-9]{0,18})', key)
        if not match: raise ValueError('器件身份格式无效。')
        return self.cached(('device', key), lambda: self._device(key, match[1], int(match[2])), ttl=300)

    def _device(self, key, namespace, number):
        with self.connection() as conn:
            if namespace == 'manual-record':
                rows = self.query(conn, "SELECT * FROM dmanual_device_ingest_record WHERE record_id=%s AND record_status='PUBLISHED' LIMIT 1", (number,))
                if not rows: raise ValueError('这条手工资料未发布或已撤销。')
                identity = self._public_identity(rows[0], True)
                artifacts = self.query(conn, "SELECT artifact_payload FROM dmanual_device_ingest_artifact WHERE record_id=%s AND artifact_type='ATTRIBUTE_INDEX' AND artifact_status='PUBLISHED' LIMIT 1", (number,))
                attrs = artifacts[0]['artifact_payload'] if artifacts else []
                if isinstance(attrs, str): attrs = json.loads(attrs)
                attrs = [{**a, 'field_value_text': a.get('field_value_text', a.get('value')),
                          'field_key': a.get('field_key', 'unknown')} for a in attrs]
            else:
                rows = self.query(conn, f'SELECT * FROM {SEARCH} WHERE device_id=%s LIMIT 1', (number,))
                if not rows: raise ValueError('发布目录中没有这条器件记录。')
                identity = self._public_identity(rows[0])
                attrs = self.query(conn, f'SELECT device_id,field_key,field_name_zh,field_value_text,field_value_num,field_unit,value_origin,source_column,source_locator,source_checksum,evidence_status,priority_rank,is_required,is_missing,is_derived_value,taxonomy_version_id,priority_extension_version,needs_engineer_review FROM {ATTRS} WHERE device_id=%s ORDER BY priority_rank,field_key LIMIT 160', (number,))
        try:
            if namespace == 'source-device':
                with self.connection(source=True) as conn:
                    src = self.query(conn, 'SELECT device_name,supplier_part,pin_count,price FROM devices WHERE device_id=%s LIMIT 1', (number,))
                if src:
                    if normalize(src[0]['device_name']) != normalize(identity['model']):
                        identity['identity_warning'] = '源记录名称已变化；当前资料以发布快照为准，未混入源记录。'
                    else:
                        identity.update(supplier_part=src[0].get('supplier_part') or '',
                                        supplier_namespace='lcsc' if str(src[0].get('supplier_part', '')).startswith('C') else 'catalog',
                                        price=src[0].get('price'), price_conditions='目录历史值；币种/阶梯/时效未完整记录')
        except KnowledgeUnavailable:
            identity['identity_warning'] = '当前源记录不可核对，保留发布资料及其定位。'
        assertions = [normalize_attribute(a) for a in attrs]
        for a in assertions:
            if a['field_key'] in ('package', 'encapsulation_specification') and not a['is_missing']:
                identity['package'] = a['raw_value']
        if not identity['supplier_part']:
            locator = identity.get('document_locator') or ''
            # Locator is retained as a locator, never promoted to a verified supplier identity.
            identity['document_supplier_hint'] = (re.search(r'lichuang_id=([CS]\d+)', locator) or [None, ''])[1]
        return {'identity': identity, 'assertions': assertions, 'checked_at': now(),
                'version_context': {'build_id': identity['build_id'], 'checksum': identity['checksum'],
                                    'taxonomy_versions': sorted({a['taxonomy_version_id'] for a in assertions if a['taxonomy_version_id']}),
                                    'priority_versions': sorted({str(a.get('priority_extension_version')) for a in attrs if a.get('priority_extension_version')})}}

    def taxonomy(self):
        def read():
            with self.connection() as c:
                versions = self.query(c, 'SELECT MAX(taxonomy_version_id) AS id FROM dtax_category_node')
                version = versions[0]['id']
                nodes = self.query(c, "SELECT category_code,display_name_zh,node_type,node_status,parent_id,category_id FROM dtax_category_node WHERE taxonomy_version_id=%s ORDER BY depth,sort_order", (version,))
            return {'version': str(version), 'nodes': nodes}
        return self.cached(('taxonomy',), read, ttl=1800)

    def spans(self, identity, topics=None):
        supplier = identity.get('supplier_part', '')
        if not SUPPLIER.fullmatch(supplier):
            return {'status': 'missing', 'items': [], 'reason': '尚无经源目录核对的 supplier 身份，未沿模糊型号读取手册。'}
        return self.cached(('spans', identity['device_key'], supplier, tuple(topics or [])),
                           lambda: self._spans(identity, topics), ttl=300)

    def _spans(self, identity, topics):
        with self._slots:return self._read_spans(identity,topics)

    def _read_spans(self, identity, topics):
        from pymilvus import MilvusClient
        supplier = identity['supplier_part']
        locator = identity.get('document_locator') or ''
        original_db = 'DataSheetBaseV2' if 'DataSheetBaseV2.' in locator else 'DataSheetBase'
        result = []; errors = []
        queries = list(dict.fromkeys(topics or ['输入电压 工作条件', '功能 参数 接口']))[:2]
        aliases=[(r'input|vin|supply voltage','输入电压 VIN 推荐工作条件'),
                 (r'output.*current|iout','输出电流 连续 峰值'),(r'frequency|switching|fosc','开关频率'),
                 (r'temperature|ambient|junction','工作温度 环境温度 TA TJ'),(r'interface|protocol|feature','功能 接口 协议')]
        queries=[' '.join([q,*[words for pattern,words in aliases if re.search(pattern,q,re.I)]]) for q in queries]
        for database in dict.fromkeys(['DataSheetBaseV2', original_db,'DataSheetBase']):
            if any(x['database'] == database for x in result): continue
            client = None
            try:
                options = {'uri': os.environ.get('PCBAGENT_MILVUS_URL', 'http://127.0.0.1:19530'),
                           'db_name': database, 'timeout': 4,'alias':'requirements_'+uuid.uuid4().hex}
                token = os.environ.get('PCBAGENT_MILVUS_TOKEN')
                if token: options['token'] = token
                client = MilvusClient(**options)
                fields = ['id', 'lichuang_id', 'content_type', 'title', 'content', 'section_path', 'chunk_idx', 'chunk_total']
                if database == 'DataSheetBaseV2': fields += ['page_idx', 'source_run', 'ingested_at']
                expr = 'lichuang_id == ' + json.dumps(supplier)
                # QueryResult is a lazy list with a bitmap for its original rows.
                # Appending BM25 hits to it breaks later iteration in PyMilvus.
                rows = [dict(row) for row in client.query('DataSheet', filter=expr, output_fields=fields, limit=2, timeout=4)]
                modes = {str(r['id']): 'scalar_identity' for r in rows}
                for text in queries:
                    try:
                        groups = client.search('DataSheet', data=[text], anns_field='content_sparse_vector', filter=expr,
                                               limit=2, output_fields=fields, search_params={'metric_type': 'BM25'}, timeout=4)
                        for hit in groups[0] if groups else []:
                            row = dict(hit['entity']); row['id'] = str(hit['id']); rows.append(row)
                            modes[str(row['id'])] = 'bm25'
                    except Exception as exc:
                        errors.append({'database': database, 'stage': 'bm25', 'error': type(exc).__name__})
                dedup = {}
                for row in rows:
                    if row.get('lichuang_id') != supplier: continue
                    row['id'] = str(row['id']); row['database'] = database; row['collection'] = 'DataSheet'
                    row['content_hash'] = digest(row.get('content', '')); row['retrieval_mode'] = modes.get(row['id'], 'scalar_identity')
                    row['page_number'] = int(row['page_idx']) + 1 if row.get('page_idx') is not None else None
                    dedup[row['id']] = row
                result += list(dedup.values())[:6]
                if result: break
            except Exception as exc:
                errors.append({'database': database, 'stage': 'read', 'error': type(exc).__name__})
            finally:
                if client is not None: client.close()
        return {'status': 'available' if result else 'unavailable' if errors else 'missing',
                'items': result[:8], 'errors': errors, 'original_locator': locator,
                'retrieval_queries':queries,
                'note': '使用明确身份的文本和片段定位；片段hash不代表原始PDF hash。'}

    def invalidate(self,references):
        terms={normalize(r['mention']) for r in references}
        keys={r.get('device_key') for r in references}-{None,''}
        with self._lock:
            for key in list(self._cache):
                if key[0] in ('search','device','spans') and (key[1] in terms or key[1] in keys):self._cache.pop(key,None)

    def resolve(self, reference):
        if reference.get('device_key'):
            device = self.device(reference['device_key'])
            return {'status': 'matched', 'match_mode': 'user_identity', 'items': [device['identity']],
                    'device': device, 'checked_at': now()}
        if SUPPLIER.fullmatch(reference.get('reference_supplier_part','')):
            from .reference_design import model_name
            suggested=self.search(reference['reference_supplier_part'],mode='exact')
            if suggested['status']=='matched' and normalize(model_name(suggested['items'][0]['model']))==normalize(model_name(reference['mention'])):
                return {**suggested,'device':self.device(suggested['items'][0]['device_key']),'match_mode':'reference_supplier_exact'}
        found = self.search(reference['mention'], mode='exact' if reference['identity_scope'] == 'exact_part' else 'auto')
        if found['status'] == 'matched' and reference['identity_scope'] != 'family':
            found = {**found, 'device': self.device(found['items'][0]['device_key'])}
        return found


catalog = Catalog()
