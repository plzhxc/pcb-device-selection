export type Category = 'overview' | 'functions' | 'performance' | 'manufacturing' | 'architecture' | 'selection';
export type State = 'stated' | 'suggested' | 'tbd' | 'conflict' | 'not_applicable';
export interface Requirement {
  id: string; category: Category; name: string; value: string; unit: string;
  priority: 'must' | 'should' | 'could'; state: State;
  source_message_id: string; source_quote: string; source_type: string; updated_at: string;
  implementation?: 'hardware'|'software'|'service'|'system';
  spec_field_ids?:string[];
}
export interface Question { source_message_id?:string; id: string; question: string; reason: string; options: string[]; requirement_ids: string[]; branch?: Category; status?: 'pending' | 'deferred'; field_ids?:string[]; decision_key?:string; priority?:number; impact?:string; option_impacts?:string[] }
export interface QuestionAnswer { question_id: string; answer: string; deferred: boolean }
export interface QuestionSubmission extends QuestionAnswer {status:'sending'|'processing'|'queued'|'needs_input'|'failed';run_id?:string;message_id?:string;queue_id?:string}
export interface RunActivity {id:string;message_id:string;kind:string;status:string;created_at:string;events:{seq:number;kind:string;message?:string;at?:string;phase?:string}[]}
export interface Document {
  title: string; summary: string; requirements: Requirement[]; questions: Question[];
  flow: { label: string; requirement_ids: string[] }[];
  version: number; updated_at: string; changed_ids: string[]; changes: string[];
  questions_paused?: boolean;
  modules?:ModuleInstance[];
  created_at?:string;
  external_interfaces?:ExternalInterface[];
}
export interface Run { id: string; status: string; error: string; created_at: string; kind?: 'user' | 'continuation' }
export interface CoverageTopic { id:string; section:string; chapter:number; title:string; fields:string[]; state:'confirmed'|'not_applicable'|'design_stage'|'tbd'|'conflict'; conclusion?:string; source_quote?:string; requirement_ids?:string[] }
export interface Review { analysis_pending?:string; status:'draft'|'ready'|'approved'; closed:number; total:number; topics:CoverageTopic[]; gaps:{id:string;title:string;section:string;fields:string[]}[]; unsettled_requirements:string[]; open_questions:number; has_flow:boolean; recheck_count?:number; fields_closed?:number|null;fields_total?:number|null;module_gaps?:string[];module_pending?:string[];consistency_issues?:ConsistencyIssue[];decision_closed?:number|null;decision_total?:number|null;engineering_pending?:number;internal_rechecks?:number }
export type ResolutionState='confirmed'|'not_applicable'|'design_stage'|'tbd'|'conflict';
export interface ResolvedField {id:string;field_id?:string;topic_id:string;label:string;chapter:number;state:ResolutionState;conclusion?:string;requirement_ids?:string[];source_quote?:string;source_message_id?:string;optional?:boolean;recheck_evidence?:{source_message_id:string;source_quote:string;conclusion:string}}
export interface ModuleType {id:string;name:string;spec_ref:string;parent_id?:string;category_id:string;level:'parent'|'submodule'}
export interface ModuleParameter {id:string;name:string;unit:string;value_rule:string;conditions:string;required:string;match_rule:string}
export interface ModuleDetail extends ModuleType {category_name:string;parent_name?:string;description?:string;ports?:{role:string;direction:string;kind:string;required:string}[];parameters?:ModuleParameter[];inherited_parameters?:ModuleParameter[];engineering_card?:string[];children?:string[]}
export interface ModuleInstance {id:string;type_id:string;name:string;purpose:string;boundary:string;quantity:number;state:'proposed'|'confirmed'|'design_stage'|'tbd'|'not_applicable';requirement_ids:string[];source_quote:string;source_message_id:string;type_name:string;category_name:string;level:'parent'|'submodule';ports:NonNullable<ModuleDetail['ports']>}
export interface ConsistencyIssue {id:string;kind:'conflict'|'review';title:string;description:string;requirement_ids:string[]}
export interface Convergence {enabled:boolean;fields:ResolvedField[];fields_closed:number;fields_total:number;counts:Record<string,number>;modules:ModuleInstance[];module_links:{id:string;source:string;target:string;kind:'power'|'data'|'control'|'clock';source_port:string;target_port:string;description:string;state:string}[];module_gaps:string[];module_pending:string[];issues:ConsistencyIssue[];progress:{version:number;closed:number;delta:number;changed_requirements:number;module_count:number;open_questions:number}[];stalled:boolean;handoff:ResolvedField[];product_review:{decision_fields:ResolvedField[];decision_closed:number;decision_total:number;decision_pending:string[];decision_rechecks:string[];module_rechecks:string[];engineering_fields:ResolvedField[];engineering_pending:number;module_topics:string[]}}
export interface Project {
  id: string; title: string; is_example: boolean; updated_at: string; document: Document; markdown: string;
  messages: { id: string; role: string; content: string; created_at: string }[];
  revisions: { version: number; created_at: string; changes: string[] }[];
  latest_run: Run | null;
  input_queue: { id: string; text: string; created_at: string }[];
  review: Review;
  convergence: Convergence;
  specification:Specification;
  decision_path:DecisionPath;
  decision_map:DecisionMap;
  question_submissions?:QuestionSubmission[];
  reference_submissions?:(DomainAction&{run_id?:string;queue_id?:string;status:'processing'|'queued'})[];
  run_activity?:RunActivity[];
  domain?:DomainView;
  domain_revision?:number;
  analysis_pending?:{message_id:string;run_id:string;status:string}|null;
  scheduling?:{running:number;queued:number;position:number|null;capacity:number};
  delivery?:Delivery;
  canvas_sync?:CanvasSync;
}

export interface DeviceChange {id:string;reference_id:string;source_message_id:string;origin:string;reason:string;before:Record<string,unknown>;after:Record<string,unknown>;status:'pending'|'synchronized'|'superseded';created_version:number;synchronized_version?:number|null;requirement_ids:string[];module_ids:string[];link_ids:string[];field_ids:string[];delivery_keys:string[];remaining:{kind:string;id:string;label:string;reason?:string;excerpt?:string}[]}
export interface CanvasSync {revision:string;version:number;pending_count:number;history:DeviceChange[];latest:DeviceChange|null;requirement_ids:string[];module_ids:string[];label:string}
export interface CurrentBomRow {reference_id:string;model:string;quantity:number|null;intent:DeviceIntent;status:'adopted'|'proposed';purpose:string;module_ids:string[];supplier_part:string;package:string;source_label:string;resolution_status:string;reference_source_id:string;reference_observed_quantity:number|null}
export interface ExternalInterface {id:string;name:string;purpose:string;external_object:string;protocol:string;signals:string[];voltage:string;shared_link:string;state:string;requirement_ids:string[]}
export interface BomRow {key:string;model:string;original_model:string;designators:string[];supplier_part:string;package:string;observed_quantity:number;quantity:number|null;core:boolean;status:'adopted'|'proposed'|'reference';source_id:string;source_filename:string;module_ids:string[];reference_id:string;purpose:string;quantity_basis:string;resolution_status:string}
export interface Delivery {next_owner:string;next_label:string;user_decisions:number;internal_tasks:number;checks:{key:string;label:string;state:string;conclusion:string;next_action:string}[];bom:{sources:{id:string;filename:string;instance_count:number;warnings:string[]}[];rows:BomRow[];current_rows?:CurrentBomRow[];core_count:number;core_adopted:number;proposed_count:number};engineering_tasks:{id:string;owner:string;description:string;completion_condition:string}[]}

export interface SpecificationField extends ResolvedField {display:'requirements'|'names'|'summary'|'title';content:string;status_label:string;review_pending:boolean;engineering:boolean}
export interface Specification {revision:string;chapters:{number:number;title:string;topics:{id:string;section:string;title:string;parent?:{section:string;title:string}|null;engineering:boolean;fields:SpecificationField[]}[]}[];unassigned_requirement_ids:string[];field_count:number;confirmed_count:number;tail_markdown:string;compact_tail_markdown?:string}
export interface DecisionBranch {id:string;question:string;reason:string;options:string[];option_impacts:string[];state:'waiting'|'processing'|'answered'|'organized'|'deferred';adjusted:boolean;requirement_ids:string[];answer:string;answer_message_id:string;receipt?:QuestionSubmission}
export interface DecisionTurn {id:string;input:string;at:string;versions:number[];analyses:string[];results:string[];current_results:string[];branches:DecisionBranch[];legacy:boolean;answered?:string[];module_ids?:string[];internal_notes?:string[]}
export interface DecisionPath {turns:DecisionTurn[];pending_reviews:{requirement_id:string;message_id:string;reason:string}[];version:number;note:string}
export interface DecisionNode {id:string;kind:'requirement'|'device'|'module'|'interface'|'question'|'boundary';group:string;title:string;conclusion:string;status:string;status_label:string;basis:string;sources:{message_id:string;quote:string}[];facts:{label:string;value:string;basis:string;conditions:string}[];evidence_ids:string[];requirement_ids:string[];module_ids:string[];spec_fields:{id:string;label:string;section:string}[];history:{version:number;value:Record<string,unknown>;source_message_id:string}[];notes:string[];question_id?:string;options?:string[];option_impacts?:string[];impact?:string}
export interface DecisionMap {revision:string;version:number;root:{title:string;summary:string};groups:{id:string;label:string;description:string;total:number;settled:number;pending:number}[];nodes:DecisionNode[];edges:{source:string;target:string;kind:string;label:string}[];counts:{settled:number;questions:number;specified_devices:number;located_devices:number};policy:string}

export type DeviceIntent='must'|'prefer'|'reference'|'undecided';
export interface DeviceIdentity {build_id?:string;record_id?:string;device_key:string;model:string;manufacturer:string;supplier_part:string;supplier_namespace:string;package:string;leaf_code?:string;leaf_name:string;taxonomy_path:string;source_locator:string;document_locator:string;origin:string;identity_warning?:string;price?:string|number|null;price_conditions?:string}
export interface DeviceReference {reference_quantity_source?:{source_id:string;sha256:string;part_key:string;basis:string};id:string;mention:string;intent:DeviceIntent;identity_scope:'exact_part'|'family'|'assembly'|'unresolved';purpose:string;requirement_ids:string[];module_ids:string[];quantity:number|null;lifecycle:string;source_message_id:string;source_quote:string;device_key?:string|null;resolution_status:string;identity:DeviceIdentity|null;candidates:DeviceIdentity[];document_status:string;lookup_message:string;evidence_ids:string[]}
export interface DeviceAssertion {field_key:string;label:string;raw_value:string;raw_numeric:string|null;raw_unit:string|null;normalized:{minimum:string;maximum:string;dimension:string;unit:string;form:string}|null;validation_state:string;issue:string;source_disagreement?:string;source_evidence_status:string;is_missing:boolean;is_derived:boolean;is_required_in_catalog:boolean;conditions:string;rating_kind:string;locator:string;source_field:string;source_checksum?:string;supporting_evidence_ids?:string[];supporting_quote?:string}
export interface DeviceEvidence {id:string;kind:string;device_key:string;title:string;locator:string;primary_key?:string;page_number?:number;section_path?:string;content?:string;excerpt?:string;content_hash?:string;assertions:DeviceAssertion[];version_context:Record<string,unknown>;availability:string;checked_at:string;reference_id?:string}
export interface DomainCheck {id:string;reference_id:string;title:string;status:string;description:string;blocking:boolean;evidence_ids:string[];formula:string;missing:string[];requirement_ids:string[]}
export interface DomainView {revision:number;references:DeviceReference[];evidence:DeviceEvidence[];checks:DomainCheck[];blockers:DomainCheck[];summary:{references:number;must:number;resolved:number;needs_identity:number;evidence:number;blocking:number;pending_checks:number};jobs:{id:string;status:string;error:string;created_at:string;finished_at?:string}[];active_jobs:number;activity:RunActivity[]}
export interface CatalogResult {query:string;status:string;match_mode:string;items:DeviceIdentity[];has_more:boolean;next_offset:number;warning?:string;page_budget_exhausted?:boolean}
export interface KnowledgeStatus {available:boolean;read_only:boolean;published_devices:number|null;source_devices?:number;manual_records?:number;checked_at:string;freshness:string;error?:string;release?:{release_code:string;published_at:string};deltas?:{new:number;missing:number;renamed:number}}
export interface DomainAction {operation:'set_intent'|'select_identity'|'remove_reference'|'add_reference'|'revise_reference';reference_id?:string;intent?:DeviceIntent;device_key?:string;mention?:string;purpose?:string;module_ids?:string[];requirement_ids?:string[];quantity?:number|null}
export interface HandoffCheck {status:'ready'|'ready_with_open_items'|'blocked';blockers:string[];open_items:{id:string;scope:string;kind:string;description:string;owner:string;completion_condition:string}[];requirement_version:number;domain_revision:number;contract_valid:boolean;selection_items:number;active_knowledge_jobs:number}
export const intentLabels:Record<DeviceIntent,string>={must:'必须使用',prefer:'优先考虑',reference:'仅作参考',undecided:'意图待明确'};
export const identityLabels:Record<string,string>={matched:'已定位目录',family:'系列约束',ambiguous:'多个身份待区分',not_found:'本次目录未找到',unavailable:'来源暂不可用',unresolved:'待检索'};
export interface ProjectSummary { id: string; title: string; is_example: boolean; version: number; requirement_count: number; updated_at: string }
export const categories: Record<Category, string> = { overview: '项目概述', functions: '功能需求', performance: '性能指标', manufacturing: '生产与测试', architecture: '功能框架', selection: '器件与约束' };
export const states: Record<State, string> = { stated: '已提出', suggested: 'AI 建议', tbd: '待确认', conflict: '有冲突', not_applicable: '不适用' };
export const active = (run: Run | null | undefined) => !!run && ['queued', 'running'].includes(run.status);

export async function api<T>(url: string, options: RequestInit = {}): Promise<T> {
  let response: Response;
  try { response = await fetch(`/api${url}`, { ...options, headers: { 'Content-Type': 'application/json', ...options.headers } }); }
  catch { throw new Error('暂时连接不到工作台，请检查本地服务后重试。'); }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === 'string' ? body.detail : `操作未完成（${response.status}），请检查输入后重试。`);
  }
  return response.json() as Promise<T>;
}
export const time = (value: string) => new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
export function readSetting(key: string, fallback: string) { try { return localStorage.getItem(`requirements-studio:${key}`) ?? fallback; } catch { return fallback; } }
export function saveSetting(key: string, value: string) { try { localStorage.setItem(`requirements-studio:${key}`, value); } catch { /* Server remains the authoritative storage. */ } }
