"""Source-reviewed fixed-LDO guarantees consumed by the shared static context.

This module neither solves another voltage network nor conducts through a part.
All application quantities are conditional operating/procurement assumptions,
not thermal-design certification or permission to invent manufacturer facts.
"""
from collections.abc import Mapping
from copy import deepcopy

from .certificate_facts import certificate_source_errors, digest
from .static_voltages import Axis, Bounds, Potential, Space, Symbol, boundary_number, number

SCHEMA = 'pcbagent.regulator_device_electrical.v1'
DECIMAL = {'type': 'string', 'minLength': 1, 'maxLength': 128}
INTERVAL = {'type': 'object', 'additionalProperties': False,
            'required': ['minimum', 'maximum'], 'properties': {'minimum': DECIMAL, 'maximum': DECIMAL}}
PINMAP = {'type': 'object', 'additionalProperties': False, 'required': ['input', 'output', 'ground', 'enable'],
          'properties': {k: {'type': 'string', 'minLength': 1} for k in ('input', 'output', 'ground', 'enable')}}
CAP = {'type': 'object', 'additionalProperties': False,
       'required': ['node', 'nominal_f', 'effective_f', 'esr_ohm', 'dielectric'],
       'properties': {'node': {'enum': ['input', 'output']}, 'nominal_f': DECIMAL,
                      'effective_f': INTERVAL, 'esr_ohm': INTERVAL, 'dielectric': {'const': 'ceramic'}}}
CAPACITOR_SELECTION_SCHEMA = {'type': 'object', 'additionalProperties': False,
 'description': 'Optional selected abstract capacitor procurement guarantees for all declared static voltage and ambient-temperature conditions; not a manufacturer claim.',
 'required': ['effective_f', 'esr_ohm', 'voltage_abs_max_v', 'temperature_c', 'dielectric'],
 'properties': {'effective_f': INTERVAL, 'esr_ohm': INTERVAL, 'voltage_abs_max_v': DECIMAL,
                'temperature_c': INTERVAL, 'dielectric': {'const': 'ceramic'}}}
REGULATOR_SCHEMA = {'type': 'object', 'additionalProperties': False,
 'required': ['schema', 'supplier_part', 'document_sha256', 'transfer'], 'properties': {
 'schema': {'const': SCHEMA}, 'supplier_part': {'type': 'string', 'pattern': '^[CS][0-9]+$'},
 'document_sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
 'transfer': {'type': 'object', 'additionalProperties': False,
  'required': ['id', 'scope', 'conditions', 'effect', 'evidence', 'review'], 'properties': {
  'id': {'type': 'string', 'minLength': 1},
  'scope': {'type': 'object', 'additionalProperties': False,
            'required': ['supplier_part', 'package', 'terminals'], 'properties': {
            'supplier_part': {'type': 'string'}, 'package': {'type': 'string', 'minLength': 1}, 'terminals': PINMAP}},
  'conditions': {'type': 'object', 'additionalProperties': False,
   'required': ['vin_v', 'enable_v', 'load_a', 'temperature', 'capacitors'], 'properties': {
    'vin_v': INTERVAL, 'enable_v': INTERVAL, 'load_a': INTERVAL,
    'temperature': {'type': 'object', 'additionalProperties': False,
      'required': ['kind', 'range_c'], 'properties': {'kind': {'enum': ['junction', 'ambient']}, 'range_c': INTERVAL}},
    'capacitors': {'type': 'array', 'minItems': 2, 'maxItems': 2, 'items': CAP}}},
  'effect': {'type': 'object', 'additionalProperties': False,
    'required': ['kind', 'claim_class', 'output_v'], 'properties': {
      'kind': {'const': 'fixed_ldo_output_envelope'}, 'claim_class': {'const': 'guaranteed_static_operating_interval'},
      'output_v': INTERVAL}},
  'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 32, 'items': {'type': 'object'}},
  'review': {'type': 'object'}}}}}


def _map(x):
    return x if isinstance(x, Mapping) else {}


def _bounds(x):
    if not isinstance(x, Mapping) or set(x) != {'minimum', 'maximum'}:
        raise ValueError('closed exact interval required')
    return Bounds(boundary_number(x['minimum']), boundary_number(x['maximum']))


def _contains(outer, inner):
    return outer.low <= inner.low <= inner.high <= outer.high


def _snapshot(contracts, cc):
    """Rebuild conditions from the original received input, not copied values."""
    b = _map(cc.get('device_electrical')); stored = _map(b.get('input_snapshot'))
    snapshot = _map(stored.get('payload'))
    if (not snapshot or digest(snapshot) != contracts.get('input_sha256')
            or stored.get('sha256') != contracts.get('input_sha256')):
        raise ValueError('original input snapshot is missing or changed')
    return snapshot


def _input_interval(contracts, cc, prefix):
    from .conditional_pin_rules import bind_frozen_input_conditions
    snapshot = _snapshot(contracts, cc)
    fields = [f'requirements.{prefix}_{end}' for end in ('min', 'max')]
    claimed = [r for r in snapshot.get('requirements', [])
               if _map(r.get('selection')).get('field') in fields
               and _map(r.get('selection')).get('device_id') == cc['supplier_part']]
    if not claimed or any(r.get('source') not in {'user', 'original_request', 'design_decision'} for r in claimed):
        raise ValueError('environment/load guarantees require explicit input operating authority')
    index = {f'requirement:{r.get("id")}': {'value': r} for r in snapshot.get('requirements', [])}
    result = bind_frozen_input_conditions(board_input=snapshot, supplier_part=cc['supplier_part'],
                                          source_index=index, fields=fields)
    if result['diagnostics']:
        raise ValueError('input interval binding has diagnostics')
    values = _map(result['conditions'].get('requirements'))
    selected = [_map(values.get(f'{prefix}_{end}')).get('value') for end in ('min', 'max')]
    # Input bounds are operating assumptions across ALL required scenarios.
    return Bounds(*(boundary_number(v) for v in selected))


def _net(pin):
    return pin.net[1] if pin is not None and pin.net is not None else None


def _generation(contracts):
    """Original receiver projection, anchored outside all derived bindings.

    The application must rebuild the independent binder from received frozen
    generation/input/knowledge. None of these root hashes are certificate data.
    """
    snapshot = _map(contracts.get('generation_binding_snapshot'))
    if set(snapshot) != {'context', 'plan', 'compilation'} or digest(snapshot) != contracts.get('generation_binding_sha256'):
        raise ValueError('original receiver generation projection is missing or changed')
    compilation = _map(snapshot['compilation']); manifest = _map(compilation.get('manifest'))
    if (compilation.get('ok') is not True or manifest.get('status') != 'compiled'
            or manifest.get('netlist_sha256') != digest(compilation.get('netlist'))
            or manifest.get('context_sha256') != digest(snapshot['context'])):
        raise ValueError('original compilation no longer matches received context/netlist')
    return snapshot


def _generation_errors(graph, contracts):
    """Rebuild external source association from the actual compilation."""
    from .netlist_graph import parse_netlist_graph
    from .static_voltages import bind_environment, domain_definition
    try:
        snapshot = _generation(contracts); manifest = snapshot['compilation']['manifest']
        original = parse_netlist_graph(snapshot['compilation']['netlist'], source=graph.source)
        if set(original.components) != set(graph.components) or original.pins != graph.pins:
            raise ValueError('actual graph differs from original compiled physical pin incidence')
        for key, component in original.components.items():
            actual = graph.components[key]
            if any(getattr(component, f) != getattr(actual, f) for f in ('type', 'supplier_id', 'value', 'is_abstract', 'pins')):
                raise ValueError('actual component differs from original compilation')
        modules = {m['id']: m for m in snapshot['plan']['modules']}
        domains = _map(contracts.get('domains')); network_domains = {}; ports = []
        for network in manifest['networks']:
            for member in network['members']:
                if member.get('kind') == 'voltage_domain':
                    if member['id'] in network_domains:
                        raise ValueError('duplicate compiled domain member')
                    network_domains[member['id']] = network['id']
                if member.get('kind') != 'port':
                    continue
                declaration = _map(_map(modules.get(member['module'])).get('ports')).get(member['port'], {})
                if declaration.get('external') is True and declaration.get('domain_binding') == 'rail':
                    ports.append({'module': member['module'], 'port': member['port'], 'bit': member['bit'],
                        'domain': declaration.get('domain'), 'net': network['id'],
                        **({'external_source': declaration['external_source']} if 'external_source' in declaration else {})})
        planned = {d['id']: d for d in snapshot['plan']['voltage_domains']}
        compiled = {d['id']: d for d in manifest['voltage_domains']}
        if set(domains) != set(planned) or set(compiled) != set(planned):
            raise ValueError('bound domain inventory differs from original compilation')
        for did, d in domains.items():
            if (compiled[did] != domain_definition(planned[did]) or d.get('net') != network_domains.get(did)
                    or any(d.get(f) != planned[did].get(f) for f in ('voltage_v', 'reference', 'profile_ref'))):
                raise ValueError('bound domain differs from original compiled domain and network')
        raw = _map(contracts.get('static_voltage_binding'))
        rebuilt, errors = bind_environment(raw.get('input_snapshot'), snapshot['context'], manifest, domains, ports)
        if errors or rebuilt != raw:
            raise ValueError('bound external endpoints differ from original compiled port incidence')
    except (ValueError, TypeError, KeyError) as exc:
        return [str(exc)]
    return []


def _original_fact(contracts, part, pid):
    record = _map(_map(contracts.get('regulator_source_records')).get(part))
    frozen = _map(_map(contracts.get('source_bundles')).get(part))
    if not record or digest(record) != frozen.get('knowledge_record_sha256'):
        return {}
    return _map(_map(_map(record.get('contracts')).get('pins')).get(pid))


def _source_errors(graph, contracts, cid, cc):
    from datasheet_automation.knowledge.evidence import primary_document_sha256
    from jsonschema import Draft202012Validator
    from .conditional_pin_rules import _review_errors
    binding = _map(cc.get('device_electrical')); profile = _map(binding.get('profile'))
    issues = [e.message for e in Draft202012Validator(REGULATOR_SCHEMA).iter_errors(profile)]
    if issues:
        return issues
    part = cc.get('supplier_part'); component = graph.components.get((graph.source, cid))
    frozen = _map(_map(contracts.get('source_bundles')).get(part)); record = _map(binding.get('source_record'))
    rule = profile['transfer']; terminals = rule['scope']['terminals']
    if (component is None or not component.is_core or component.anchor_id != part
            or profile['supplier_part'] != part or rule['scope']['supplier_part'] != part
            or profile['document_sha256'] != primary_document_sha256(_map(frozen.get('bundle')))
            or binding.get('origin') != 'frozen_source_and_explicit_input_selections'
            or binding.get('profile_sha256') != digest(profile)
            or binding.get('source_knowledge_sha256') != frozen.get('knowledge_record_sha256')
            or digest(record) != frozen.get('knowledge_record_sha256')
            or _map(record.get('contracts')).get('device_electrical') != profile
            or binding.get('input_sha256') != contracts.get('input_sha256')):
        issues.append('exact fixed instance and complete source/input profile membership are required')
    issues.extend(_review_errors(rule))
    issues.extend(certificate_source_errors(contracts, part, rule['evidence'],
                   ['regulator_output_envelope', 'regulator_operating_conditions', 'pin_number', 'pin_name']))
    pins = graph.pin_map((graph.source, cid)) if component else {}
    if len(set(terminals.values())) != 4 or not set(terminals.values()) <= set(pins):
        issues.append('four distinct actual physical terminals are required')
    role_sets = {'input': {'supply_vdd', 'primary_vdd', 'buck_vin'}, 'output': {'out'},
                 'ground': {'supply_gnd', 'primary_gnd', 'buck_gnd'}, 'enable': {'logic_in', 'buck_en'}}
    for name, pid in terminals.items():
        fact = _map(_map(cc.get('pins')).get(pid))
        original_fact = _map(_map(_map(record.get('contracts')).get('pins')).get(pid))
        refs = _map(fact.get('field_evidence')).get('semantic_role', fact.get('evidence'))
        original_refs = _map(original_fact.get('field_evidence')).get('semantic_role', original_fact.get('evidence'))
        if (fact.get('semantic_role') not in role_sets[name]
                or fact.get('semantic_role') != original_fact.get('semantic_role') or refs != original_refs):
            issues.append(f'{name}: source-authenticated terminal role required')
        issues.extend(certificate_source_errors(contracts, part, refs, ['semantic_role']))
    try:
        _snapshot(contracts, cc)
        cond = rule['conditions']
        for key in ('vin_v', 'enable_v', 'load_a'):
            if _bounds(cond[key]).low < 0:
                raise ValueError('first profile supports positive LDO quantities only')
        _bounds(cond['temperature']['range_c'])
        if _bounds(rule['effect']['output_v']).low <= 0:
            raise ValueError('positive guaranteed output required')
        if {c['node'] for c in cond['capacitors']} != {'input', 'output'}:
            raise ValueError('one exact input and one output capacitor condition required')
        for cap in cond['capacitors']:
            nominal = boundary_number(cap['nominal_f']); effective = _bounds(cap['effective_f'])
            if nominal <= 0 or effective.low <= 0 or not effective.low <= nominal <= effective.high or _bounds(cap['esr_ohm']).low < 0:
                raise ValueError('invalid capacitor guarantee condition')
    except (TypeError, ValueError) as exc:
        issues.append(str(exc))
    return issues


def _capacitor_errors(graph, contracts, net, ground, requirement, scope_voltage, temperature):
    """One actual abstract capacitor, with a frozen procurement specification."""
    from .electrical_values import normalized_value
    candidates = []
    for key, component in graph.components.items():
        pins = graph.pin_map(key)
        if component.type == 'capacitor' and len(pins) == 2 and {_net(p) for p in pins.values()} == {net, ground}:
            candidates.append((component, pins, _map(_map(contracts.get('components')).get(key[1]))))
    if len(candidates) != 1:
        return ['exactly one actual capacitor is required for this bounded application (no parallel inference)']
    component, pins, cc = candidates[0]
    obligation = _map(_map(cc.get('logical_terminal_contract')).get('voltage_obligation'))
    selected = _map(obligation.get('selection')); spec = _map(selected.get('specification'))
    params = _map(spec.get('parameters')); procurement = _map(params.get('regulator_application'))
    root = _map(contracts.get('regulator_plan_snapshot')); plan = _map(root.get('payload'))
    module = next((m for m in plan.get('modules', []) if m.get('id') == cc.get('module')), {})
    original = _map(module.get('common_devices')).get(cc.get('device'))
    from .pin_contracts import evidence_errors
    mapping = _map(obligation.get('terminal_mapping'))
    try:
        if (plan != _generation(contracts)['plan']
                or root.get('sha256') != digest(plan) or selected.get('plan_sha256') != root.get('sha256')
                or original != spec or selected.get('specification_sha256') != digest(spec)
                or selected.get('kind') != 'frozen_abstract_design_selection'
                or component.is_abstract is not True or component.supplier_id is not None
                or _map(spec.get('device')).get('type') != 'capacitor'
                or _map(spec.get('device')).get('value') != component.value
                or params != obligation.get('parameters')
                or set(mapping) != set(pins) or set(mapping.values()) != {'a', 'b'}
                or spec.get('terminal_mapping') != mapping
                or evidence_errors(obligation.get('template_evidence'))
                or any(e.get('source_kind') != 'template' for e in obligation['template_evidence'])):
            raise ValueError('original frozen abstract capacitor selection and logical terminals are required')
        if set(procurement) != {'effective_f', 'esr_ohm', 'voltage_abs_max_v', 'temperature_c', 'dielectric'}:
            raise ValueError('complete effective-capacitance/ESR/voltage/temperature procurement scope is required')
        dimension, nominal = normalized_value(component.value)
        if dimension != 'f' or number(nominal) != boundary_number(requirement['nominal_f']):
            raise ValueError('actual nominal capacitance differs from source application condition')
        if (procurement['dielectric'] != requirement['dielectric']
                or not _contains(_bounds(requirement['effective_f']), _bounds(procurement['effective_f']))
                or not _contains(_bounds(requirement['esr_ohm']), _bounds(procurement['esr_ohm']))
                or boundary_number(procurement['voltage_abs_max_v']) < scope_voltage
                or not _contains(_bounds(procurement['temperature_c']), temperature)):
            raise ValueError('selected capacitor guarantee does not cover the source/application operating conditions')
    except (TypeError, ValueError, KeyError) as exc:
        return [str(exc)]
    return []


class TransferSet:
    def __init__(self, graph, contracts):
        self.graph, self.contracts = graph, contracts
        self.candidates, self.rows, self.cache, self.active = {}, [], {}, set()
        generation_errors = _generation_errors(graph, contracts) if any(
            _map(_map(_map(cc).get('device_electrical')).get('profile')).get('schema') == SCHEMA
            for cc in _map(contracts.get('components')).values()) else []
        for cid, cc in _map(contracts.get('components')).items():
            profile = _map(_map(cc.get('device_electrical')).get('profile'))
            if profile.get('schema') != SCHEMA:
                continue
            errors = generation_errors + _source_errors(graph, contracts, cid, cc)
            if errors:
                self.add(cid, 'REGULATOR_SOURCE', 'unresolved', errors); continue
            rule = profile['transfer']; tm = rule['scope']['terminals']; pins = graph.pin_map((graph.source, cid))
            nets = {name: _net(pins[pid]) for name, pid in tm.items()}
            if (any(net is None for net in nets.values()) or len({nets['input'], nets['output'], nets['ground']}) != 3):
                self.add(cid, 'REGULATOR_TERMINALS', 'unresolved', ['actual separate IN/OUT/GND and connected EN are required']); continue
            # Exact output ownership: source roles must not hide a second driver.
            peers = graph.nets[(graph.source, nets['output'])].pins
            drivers = [p for p in peers if p[1] != cid and _map(_map(_map(contracts.get('components')).get(p[1])).get('pins')).get(p[2], {}).get('electrical_type') in
                       {'power_out', 'push_pull_output', 'analog_output', 'open_drain', 'open_source', 'tri_state'}]
            if drivers:
                self.add(cid, 'REGULATOR_OUTPUT_DRIVERS', 'unresolved', ['another output driver shares this regulator output']); continue
            unknown_peers = []
            for peer in peers:
                if peer[1] == cid:
                    continue
                peer_component = graph.components[(graph.source, peer[1])]
                if not peer_component.is_core:
                    peer_pins = graph.pin_map(peer_component.key)
                    # The declared total load cannot excuse a visibly
                    # contradictory resistor/other unmodelled branch. This
                    # first release consumes only the exact shunt capacitor;
                    # it does not infer conductance through arbitrary parts.
                    if (peer_component.type != 'capacitor' or len(peer_pins) != 2
                            or {_net(p) for p in peer_pins.values()} != {nets['output'], nets['ground']}):
                        unknown_peers.append(peer[1:])
                    continue
                peer_cc = _map(_map(contracts.get('components')).get(peer[1]))
                fact = _map(_map(peer_cc.get('pins')).get(peer[2]))
                original_fact = _original_fact(contracts, peer_cc.get('supplier_part'), peer[2])
                refs = _map(fact.get('field_evidence')).get('electrical_type', fact.get('evidence'))
                original_refs = _map(original_fact.get('field_evidence')).get('electrical_type', original_fact.get('evidence'))
                # First release admits a reviewed input load only. A MOS drain,
                # unresolved multiplexed pin or unsupported power path cannot be
                # silently assumed to draw current rather than drive this rail.
                if (peer_cc.get('supplier_part') != peer_component.anchor_id
                        or fact.get('electrical_type') not in {'power_in', 'input'}
                        or fact.get('electrical_type') != original_fact.get('electrical_type') or refs != original_refs
                        or certificate_source_errors(contracts, peer_cc.get('supplier_part'), refs, ['electrical_type'])):
                    unknown_peers.append(peer[1:])
            if unknown_peers:
                self.add(cid, 'REGULATOR_OUTPUT_LOADING', 'unresolved', ['active output-net peer has no authenticated input-load behavior'], peers=unknown_peers); continue
            self.candidates[cid] = {'rule': rule, 'cc': cc, 'nets': nets,
                'symbol': Symbol(digest({'input': contracts.get('input_sha256'), 'instance': cid,
                                         'source_profile': profile}), 'regulated_output_minus_ground')}

    def add(self, cid, rule, status, errors=(), **detail):
        row = {'component': cid, 'pin': None, 'rule': rule, 'status': status,
               'message': '; '.join(errors) if errors else 'Source guarantee applies to this explicit static operating scenario', **detail}
        if row not in self.rows:
            self.rows.append(row)

    def axes(self):
        return [Axis(c['symbol'], _bounds(c['rule']['effect']['output_v'])) for c in self.candidates.values()]

    def context_identity(self, base_context):
        if not self.candidates and not self.rows:
            return base_context
        graph_rows = [{'component': key[1], 'part': c.anchor_id, 'type': c.type, 'value': c.value,
                       'is_abstract': c.is_abstract,
                       'pins': sorted((pid, _net(pin)) for pid, pin in self.graph.pin_map(key).items())}
                      for key,c in sorted(self.graph.components.items())]
        sources = {cid: {'knowledge': _map(cc.get('device_electrical')).get('source_knowledge_sha256'),
                         'profile': _map(cc.get('device_electrical')).get('profile_sha256')}
                   for cid,cc in _map(self.contracts.get('components')).items()
                   if _map(_map(cc.get('device_electrical')).get('profile')).get('schema') == SCHEMA}
        return digest({'external_context': base_context, 'actual_graph': graph_rows, 'sources': sources,
                       'plan': _map(self.contracts.get('regulator_plan_snapshot')).get('sha256')})

    def net_provenance(self, net):
        return [{'component': cid, 'rule_id': c['rule']['id'],
                 'profile_sha256': c['cc']['device_electrical']['profile_sha256'],
                 'source_knowledge_sha256': c['cc']['device_electrical']['source_knowledge_sha256']}
                for cid,c in self.candidates.items() if c['nets']['output'] == net and self.cache.get(cid)]

    def resolve(self, ctx, did, parent, visited):
        declaration = ctx.domains[did]
        candidates = [(cid, c) for cid, c in self.candidates.items() if c['nets']['output'] == declaration.get('net')]
        if not candidates:
            return None
        if len(candidates) != 1:
            return Potential(ctx.space, declaration.get('net'), parent.reference, None, 'unknown')
        cid, c = candidates[0]; nets = c['nets']; conditions = c['rule']['conditions']
        if cid in self.active:
            self.add(cid, 'REGULATOR_DEPENDENCY', 'unresolved', ['cyclic supply or enable dependency']); return Potential(ctx.space, nets['output'], parent.reference, None, 'unknown')
        if cid not in self.cache:
            self.active.add(cid)
            errors = []
            try:
                if parent.net != nets['ground']:
                    raise ValueError('domain reference does not reach the actual source-qualified GND terminal')
                def differential(positive):
                    from .static_voltages import terminal_difference
                    d = terminal_difference(ctx.net(positive, visited), ctx.net(nets['ground'], visited)).expression.range()
                    if d.status != 'bounded':
                        raise ValueError('actual same-reference input/enable envelope is unavailable')
                    return d.bounds
                vin = differential(nets['input']); enable = differential(nets['enable'])
                if not _contains(_bounds(conditions['vin_v']), vin) or not _contains(_bounds(conditions['enable_v']), enable):
                    raise ValueError('actual VIN or EN leaves this source-guarantee applicability window')
                load = _input_interval(self.contracts, c['cc'], 'regulator_output_current_a')
                kind = conditions['temperature']['kind']
                temp = _input_interval(self.contracts, c['cc'], f'regulator_{kind}_temperature_c')
                ambient = _input_interval(self.contracts, c['cc'], 'regulator_ambient_temperature_c')
                if not _contains(_bounds(conditions['load_a']), load) or not _contains(_bounds(conditions['temperature']['range_c']), temp):
                    raise ValueError('explicit operating load/temperature is outside this source guarantee')
                out = _bounds(c['rule']['effect']['output_v'])
                # Use the union of proven VIN and candidate VOUT as procurement
                # voltage scope; candidate voltage is not exported before proof.
                for cap in conditions['capacitors']:
                    errors.extend(_capacitor_errors(self.graph, self.contracts, nets[cap['node']], nets['ground'],
                                  cap, max(vin.absolute_maximum, out.absolute_maximum), ambient))
            except (ValueError, TypeError, KeyError) as exc:
                errors.append(str(exc))
            finally:
                self.active.remove(cid)
            self.cache[cid] = not errors
            self.add(cid, 'REGULATOR_OUTPUT_ENVELOPE', 'unresolved' if errors else 'pass', errors,
                     condition_basis='explicit_all_scenario_input_operating_assumptions',
                     thermal_design_proven=False, output_v=c['rule']['effect']['output_v'] if not errors else None)
        if not self.cache[cid] or parent.net != nets['ground']:
            return Potential(ctx.space, nets['output'], parent.reference, None, 'unknown')
        return Potential(ctx.space, nets['output'], parent.reference, parent.expression + ctx.space.symbol(c['symbol']))


def attach_regulator_transfers(contexts, graph, contracts):
    """Graph-aware checker entry. No transferable self-signed voltage receipt."""
    result = []
    for context in contexts:
        if context.binding is None or context.errors:
            result.append(context); continue
        transfers = TransferSet(graph, contracts)
        space = Space(transfers.context_identity(context.space.context_id), context.space.scenario_id,
                      context.space.axes + tuple(transfers.axes()), context.space.joint_domain)
        from .static_voltages import StaticVoltageContext
        result.append(StaticVoltageContext(space, context.domains, context.binding, context.errors, transfers))
    return result


def check_regulator_transfers(graph, contracts):
    from .static_voltages import current_context
    context = current_context(contracts); transfers = context.transfers if context is not None else None
    if transfers is None:
        pending = [cid for cid, cc in _map(contracts.get('components')).items()
                   if _map(_map(_map(cc).get('device_electrical')).get('profile')).get('schema') == SCHEMA]
        return {'diagnostics': [{'component': cid, 'pin': None, 'rule': 'REGULATOR_CONTEXT', 'status': 'unresolved',
                 'message': 'The source transfer requires an independently bound scenario and actual graph context'} for cid in pending], 'coverage': []}
    for did in context.domains:
        context.domain(did)
    for cid in transfers.candidates:
        if cid not in transfers.cache:
            transfers.add(cid, 'REGULATOR_DOMAIN', 'unresolved', ['no actual output domain consumed this source application'])
    return {'diagnostics': deepcopy(transfers.rows), 'coverage': [
        {'component': cid, 'component_type': 'ic', 'status': 'pass' if transfers.cache.get(cid) else 'unresolved',
         'required': ['source_conditional_static_output_envelope'], 'thermal_design_proven': False}
        for cid in transfers.candidates]}
