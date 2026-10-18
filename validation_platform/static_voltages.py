"""Exact affine static voltage resolution from frozen input and actual networks.

Arithmetic helpers have no independent source authority. Public context builders
retain the external input boundary and do not infer internal device transfers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
import json
from typing import Mapping


def number(value: object) -> Fraction:
    """New profile numbers preserve decimal lexemes; binary floats are rejected.

    Fractions are an internal exact representation. A legacy float-point adapter
    must remain explicitly versioned outside this new bounded-profile parser.
    """
    if type(value) is int or type(value) is Fraction:
        return Fraction(value)
    if type(value) is Decimal and value.is_finite():
        return Fraction(value)
    raise ValueError('exact finite numeric required: int/Decimal/Fraction; no bool, float or strings')


def json_exact(text: str) -> object:
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def bad_constant(value):
        raise ValueError('non-finite JSON number: ' + value)
    return json.loads(text, parse_float=Decimal, parse_constant=bad_constant, object_pairs_hook=pairs)


@dataclass(frozen=True)
class Bounds:
    low: Fraction
    high: Fraction

    def __post_init__(self):
        object.__setattr__(self, 'low', number(self.low))
        object.__setattr__(self, 'high', number(self.high))
        if self.low > self.high:
            raise ValueError('reversed interval')

    @property
    def absolute_maximum(self) -> Fraction:
        return max(abs(self.low), abs(self.high))

    def to_dict(self) -> dict:
        def rational(value):
            return {'numerator': value.numerator, 'denominator': value.denominator}
        return {'minimum_v': rational(self.low), 'maximum_v': rational(self.high)}


def parse_quantity(value: Mapping) -> Bounds:
    """Shape validation only; source ownership/claim class remain external."""
    if not isinstance(value, Mapping):
        raise ValueError('quantity object required')
    if set(value) == {'kind', 'value_v'} and value['kind'] == 'fixed':
        v = number(value['value_v'])
        return Bounds(v, v)
    if (value.get('kind') == 'bounded' and {'kind', 'minimum_v', 'maximum_v'} <= set(value)
            <= {'kind', 'minimum_v', 'maximum_v', 'nominal_v'}):
        bounds = Bounds(value['minimum_v'], value['maximum_v'])
        if value.get('nominal_v') is not None:
            nominal = number(value['nominal_v'])
            if not bounds.low <= nominal <= bounds.high:
                raise ValueError('nominal outside declared bounds')
        return bounds
    raise ValueError('explicit point or complete bounds required; nominal/typical/absence is not a range')


@dataclass(frozen=True, order=True)
class Symbol:
    owner: str  # independently bound fixed source/instance, not display name
    quantity: str

    def __post_init__(self):
        if not all(type(x) is str and x for x in (self.owner, self.quantity)):
            raise ValueError('nonempty source-owner and quantity identities required')


@dataclass(frozen=True)
class Axis:
    symbol: Symbol
    bounds: Bounds | None  # named but unknown envelope can cancel algebraically


@dataclass(frozen=True)
class Space:
    context_id: str  # frozen board/profile/input identity, not an LLM label
    scenario_id: str
    axes: tuple[Axis, ...] = ()
    joint_domain: str = 'enclosure'  # independent_box is an explicit math hypothesis

    def __post_init__(self):
        if not all(type(x) is str and x for x in (self.context_id, self.scenario_id)):
            raise ValueError('frozen context and scenario identities required')
        if self.joint_domain not in {'enclosure', 'independent_box'}:
            raise ValueError('unknown joint-domain semantics')
        if any(not isinstance(a, Axis) or not isinstance(a.symbol, Symbol)
               or a.bounds is not None and not isinstance(a.bounds, Bounds) for a in self.axes):
            raise ValueError('invalid symbolic domain')
        if len({a.symbol for a in self.axes}) != len(self.axes):
            raise ValueError('duplicate symbol identity, even when ranges are identical')
        object.__setattr__(self, 'axes', tuple(sorted(self.axes, key=lambda a: a.symbol)))

    def constant(self, value: object) -> Affine:
        return Affine(self, number(value))

    def symbol(self, identity: Symbol) -> Affine:
        if identity not in {a.symbol for a in self.axes}:
            return Affine.unknown(self, 'undeclared_symbol')
        return Affine(self, Fraction(0), ((identity, Fraction(1)),))


@dataclass(frozen=True)
class RangeResult:
    status: str  # bounded or unresolved; NOT an electrical check status
    bounds: Bounds | None
    reasons: tuple[str, ...] = ()
    minimum_assignment: tuple = ()
    maximum_assignment: tuple = ()


@dataclass(frozen=True)
class Affine:
    space: Space
    offset: Fraction = Fraction(0)
    terms: tuple[tuple[Symbol, Fraction], ...] = ()
    unresolved: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, 'offset', number(self.offset))
        merged = {}
        for symbol, coefficient in self.terms:
            if symbol not in {a.symbol for a in self.space.axes}:
                raise ValueError('expression contains an undeclared symbol')
            merged[symbol] = merged.get(symbol, Fraction(0)) + number(coefficient)
        object.__setattr__(self, 'terms', tuple(sorted((s,c) for s,c in merged.items() if c)))
        object.__setattr__(self, 'unresolved', tuple(sorted(set(self.unresolved))))

    @classmethod
    def unknown(cls, space: Space, reason: str) -> Affine:
        return cls(space, unresolved=(reason,))

    def __add__(self, other: Affine) -> Affine:
        if not isinstance(other, Affine):
            raise TypeError('explicit affine expression required')
        if self.space != other.space:
            return self.unknown(self.space, 'context_or_scenario_mismatch')
        return Affine(self.space, self.offset+other.offset, self.terms+other.terms,
                      self.unresolved+other.unresolved)

    def __sub__(self, other: Affine) -> Affine:
        return self + other.scale(-1)

    def scale(self, value: object) -> Affine:
        if isinstance(value, Affine):
            return self.unknown(self.space, 'non_affine_product_unsupported')
        factor = number(value)
        return Affine(self.space, self.offset*factor, tuple((s,c*factor) for s,c in self.terms), self.unresolved)

    def divide(self, value: object) -> Affine:
        if isinstance(value, Affine):
            return self.unknown(self.space, 'variable_division_unsupported')
        divisor = number(value)
        return self.unknown(self.space, 'division_by_zero') if divisor == 0 else self.scale(1/divisor)

    def range(self) -> RangeResult:
        if self.unresolved:
            return RangeResult('unresolved', None, self.unresolved)
        axes = {a.symbol:a.bounds for a in self.space.axes}
        if any(axes[s] is None for s,_ in self.terms):
            return RangeResult('unresolved', None, ('unknown_symbol_envelope',))
        lo = hi = self.offset
        low_witness = {}; high_witness = {}
        # Include all axes in a full mathematical box witness, not just terms.
        if any(axis.bounds is None for axis in self.space.axes):
            full_assignment_available = False
        else:
            full_assignment_available = True
            low_witness = {a.symbol:a.bounds.low for a in self.space.axes}
            high_witness = dict(low_witness)
        for symbol, coefficient in self.terms:
            bounds = axes[symbol]
            low = bounds.low if coefficient > 0 else bounds.high
            high = bounds.high if coefficient > 0 else bounds.low
            lo += coefficient*low; hi += coefficient*high
            if full_assignment_available:
                low_witness[symbol]=low; high_witness[symbol]=high
        return RangeResult('bounded', Bounds(lo, hi), (), tuple(sorted(low_witness.items())), tuple(sorted(high_witness.items())))

    def at(self, assignment: Mapping[Symbol, object]) -> Fraction:
        """Evaluate a complete box assignment; this does NOT prove it reachable."""
        if self.unresolved or set(assignment) != {a.symbol for a in self.space.axes}:
            raise ValueError('complete known-expression assignment required')
        values = {s:number(v) for s,v in assignment.items()}
        if any(a.bounds is None or not a.bounds.low <= values[a.symbol] <= a.bounds.high for a in self.space.axes):
            raise ValueError('assignment lacks an established enclosure')
        return self.offset + sum((coefficient*values[symbol] for symbol,coefficient in self.terms), Fraction(0))


@dataclass(frozen=True)
class Proof:
    space: Space
    outcome: str  # proved/disproved/unresolved: a mathematical proposition only
    margin: RangeResult
    reason: str
    witness: tuple = ()
    electrical_authority: bool = field(default=False, init=False)

    @property
    def context_id(self):
        return self.space.context_id

    @property
    def scenario_id(self):
        return self.space.scenario_id


def prove(left: Affine, op: str, right: Affine) -> Proof:
    if op not in {'>=', '<=', '=='}:
        raise ValueError('unsupported relation; strict/nonlinear predicates require another reviewed capability')
    margin = (right-left) if op=='<=' else (left-right)
    ranged = margin.range()
    if ranged.status!='bounded':
        return Proof(left.space,'unresolved',ranged,'unknown operands or unsupported relation')
    bounds = ranged.bounds
    safe = bounds.low >= 0 if op!='==' else bounds.low == bounds.high == 0
    if safe:
        return Proof(left.space,'proved',ranged,'universal affine inequality over entire enclosing box')
    witness = ranged.minimum_assignment
    if op=='==' and bounds.low==0:
        witness = ranged.maximum_assignment
    # A possible violating corner of an enclosure is not an admissible physical
    # counterexample unless independent_box was explicitly established upstream.
    if (left.space.joint_domain == 'independent_box' and
            len(witness)==len(left.space.axes)):
        return Proof(left.space,'disproved',ranged,
                     'violating assignment under explicit independent-box hypothesis',witness)
    return Proof(left.space,'unresolved',ranged,
                 'possible violation; actual joint admissibility is not established')


def predicate_gte(value: Affine, threshold: Affine) -> str:
    ranged = (value-threshold).range()
    if ranged.status!='bounded':
        return 'unknown'
    return 'true' if ranged.bounds.low>=0 else 'false' if ranged.bounds.high<0 else 'mixed'


@dataclass(frozen=True)
class Potential:
    space: Space
    net: str | None
    reference: str | None
    expression: Affine | None
    state: str = 'potential'

    def __post_init__(self):
        if any(value is not None and (type(value) is not str or not value) for value in (self.net,self.reference)):
            raise ValueError('actual net/reference identities must be nonempty strings or unknown')
        if self.expression is not None and (not isinstance(self.expression,Affine) or self.expression.space != self.space):
            raise ValueError('potential expression context mismatch')
        if self.state not in {'potential','floating','absent','high_impedance','unknown'}:
            raise ValueError('unrecognized terminal state')
        if self.state != 'potential' and self.expression is not None:
            raise ValueError('terminal state does not itself establish a potential')


@dataclass(frozen=True)
class Difference:
    expression: Affine
    basis: str
    ground_authority: bool = field(default=False, init=False)
    source_contention_checked: bool = field(default=False, init=False)
    graph_modified: bool = field(default=False, init=False)


def terminal_difference(positive: Potential, negative: Potential) -> Difference:
    space = positive.space
    if space != negative.space:
        return Difference(Affine.unknown(space,'context_or_scenario_mismatch'),'unresolved')
    if positive.net is not None and positive.net == negative.net:
        return Difference(space.constant(0),'same_actual_wire_identity')
    if positive.reference is None or positive.reference != negative.reference:
        return Difference(Affine.unknown(space,'reference_relation_unresolved'),'unresolved')
    if positive.expression is None or negative.expression is None:
        return Difference(Affine.unknown(space,'terminal_potential_unresolved'),'unresolved')
    return Difference(positive.expression-negative.expression,'shared_reference_affine_difference')


def all_scenarios(context_id: str, required: tuple[Space,...], proofs: Mapping[str,Proof]) -> dict:
    """Coverage prototype only: no graph/value metric or denominator argument."""
    expected = {space.scenario_id:space for space in required}
    complete = (bool(required) and len(expected)==len(required) and set(proofs)==set(expected)
                and all(space.context_id==context_id for space in required)
                and all(p.space==expected[key] for key,p in proofs.items()))
    if not complete:
        return {'outcome':'unresolved','complete':False,'reason':'exact required scenario coverage missing or mismatched',
                'required':[s.scenario_id for s in required],'observed':sorted(proofs),'electrical_authority':False}
    outcome = ('disproved' if any(p.outcome=='disproved' for p in proofs.values()) else
               'proved' if all(p.outcome=='proved' for p in proofs.values()) else 'unresolved')
    return {'outcome':outcome,'complete':True,'required':[s.scenario_id for s in required],'observed':sorted(proofs),
            'electrical_authority':False}


# Shared input/context boundary. Numeric strings are an explicit v1 wire type;
# only this decoder converts them to exact numbers. Legacy numeric points have
# their own compatibility branch below and cannot supply bounded guarantees.
import hashlib
import re
from copy import deepcopy

ENVIRONMENT_SCHEMA = 'pcbagent.static_environment.v1'
_DECIMAL = re.compile(r'[+-]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')


def _map(value):
    return value if isinstance(value, Mapping) else {}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def boundary_number(value):
    if type(value) is not str or len(value) > 128 or not _DECIMAL.fullmatch(value):
        raise ValueError('bounded-profile numbers must be explicit finite decimal strings')
    try:
        decimal = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError('bounded-profile decimal is outside the supported exact numeric budget') from exc
    if not decimal.is_finite() or abs(decimal.adjusted()) > 100:
        raise ValueError('bounded-profile decimal exponent exceeds the supported budget')
    return number(decimal)


def _boundary_quantity(raw):
    if not isinstance(raw, Mapping):
        raise ValueError('closed explicit voltage quantity required')
    converted = {k: boundary_number(v) if k in {'value_v', 'minimum_v', 'maximum_v', 'nominal_v'} else v
                 for k, v in raw.items()}
    return parse_quantity(converted)


def validate_environment(raw, *, requirements=None):
    """Validate explicit external input structure, never infer it from prose."""
    if (not isinstance(raw, Mapping) or set(raw) != {'schema_version', 'sources', 'required_scenarios'}
            or raw['schema_version'] != ENVIRONMENT_SCHEMA):
        raise ValueError('closed versioned static_environment required')
    sources, scenarios = raw['sources'], raw['required_scenarios']
    if not isinstance(sources, Mapping) or not 1 <= len(sources) <= 16:
        raise ValueError('static environment requires 1..16 explicit external sources')
    known = None if requirements is None else {r.get('id'): r for r in requirements if isinstance(r, Mapping)}
    def refs(value):
        if not isinstance(value, list) or not value or any(
                type(r) is not str or not r.startswith('requirement:') or not r[12:] for r in value) or len(value) != len(set(value)):
            raise ValueError('static source claims require unique explicit requirement references')
        if known is not None and any(r[12:] not in known or known[r[12:]].get('source') not in
                {'user', 'original_request', 'design_decision'} for r in value):
            raise ValueError('external guarantees must bind exact frozen input requirements')
    for sid, source in sources.items():
        if type(sid) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', sid):
            raise ValueError('bounded external source identifier required')
        if (not isinstance(source, Mapping) or set(source) != {'kind', 'terminals', 'source_authority', 'evidence_refs'}
                or source['kind'] != 'external_voltage_source'
                or source['source_authority'] != 'input_external_operating_constraint'
                or set(_map(source['terminals'])) != {'positive', 'return'}
                or any(type(v) is not str or not v for v in source['terminals'].values())):
            raise ValueError('external source identity and terminal meanings must be explicit')
        refs(source['evidence_refs'])
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 32:
        raise ValueError('static environment requires 1..32 exhaustive scenario declarations')
    seen = set()
    for scenario in scenarios:
        required = {'id', 'sources', 'evidence_refs', 'required_functionality'}
        if (not isinstance(scenario, Mapping) or not required <= set(scenario)
                or set(scenario) - required - {'external_configuration'}
                or type(scenario['id']) is not str or not scenario['id'] or scenario['id'] in seen
                or scenario['required_functionality'] != 'all_requested_static_functions'):
            raise ValueError('unique closed scenarios retain all static obligations; no Agent-authored exclusions')
        seen.add(scenario['id']); refs(scenario['evidence_refs'])
        config = scenario.get('external_configuration', {})
        if not isinstance(config, Mapping) or any(type(k) is not str or type(v) not in {str, int, bool} for k,v in config.items()):
            raise ValueError('external scenario configuration is finite input metadata only')
        if not isinstance(scenario['sources'], Mapping) or set(scenario['sources']) != set(sources):
            raise ValueError('every required scenario must declare every external source state')
        for state in scenario['sources'].values():
            if not isinstance(state, Mapping):
                raise ValueError('explicit source state required')
            if state.get('state') == 'present' and set(state) == {'state', 'voltage', 'evidence_refs'}:
                _boundary_quantity(state['voltage'])
            elif state.get('state') != 'absent' or set(state) != {'state', 'evidence_refs'}:
                raise ValueError('present source needs an exact point or full range; absent source has no voltage')
            refs(state['evidence_refs'])
    return deepcopy(dict(raw))


def voltage_domain_profile_valid(domain: Mapping, environment: Mapping | None) -> bool:
    """Share the SCL profile lookup guard with planning; create no source facts.

    Environment structure and evidence authority have independent validators.
    This predicate only rejects unsupported profile selections; its success is
    not a proof of voltage, endpoints, source applicability or circuit validity.
    """
    if 'profile_ref' not in domain:
        return True
    profile = domain['profile_ref']
    sources = _map(_map(environment).get('sources'))
    return (isinstance(profile, str) and profile in sources
            and domain.get('voltage_v') is None and domain.get('reference') is not None)


def domain_definition(domain):
    if not isinstance(domain, Mapping) or not {'id', 'voltage_v', 'reference'} <= set(domain):
        raise ValueError('voltage domain requires explicit id, voltage_v and reference')
    result = {k: domain[k] for k in ('id', 'voltage_v', 'reference')}
    if 'profile_ref' in domain:
        if type(domain['profile_ref']) is not str or not domain['profile_ref'] or domain['voltage_v'] is not None:
            raise ValueError('profile_ref is an existing source ID and excludes a scalar/nominal voltage')
        result['profile_ref'] = domain['profile_ref']
    return result


def has_profiles(contracts):
    return ('static_voltage_binding' in _map(contracts) or any('profile_ref' in _map(d)
            for d in _map(_map(contracts).get('domains')).values()))


def domains_have_profiles(domains):
    return any('profile_ref' in _map(d) for d in _map(domains).values())


def bind_environment(board_input, context, manifest, domains, external_ports):
    """Independent binder entry; output is rebuilt from the received full input."""
    environment = _map(board_input).get('static_environment')
    if environment is None:
        if _map(context).get('static_environment') is not None or domains_have_profiles(domains):
            return None, ['profile domain has no explicit frozen external environment']
        return None, []
    try:
        environment = validate_environment(environment, requirements=_map(board_input).get('requirements', []))
    except (ValueError, TypeError) as exc:
        return None, [str(exc)]
    errors = []
    if _map(context).get('static_environment') != environment or _map(manifest).get('static_environment_sha256') != digest(environment):
        errors.append('compiled environment differs from the full frozen input')
    endpoints = {}
    for sid in environment['sources']:
        candidates = [(did, d) for did, d in domains.items() if _map(d).get('profile_ref') == sid]
        if len(candidates) != 1:
            errors.append(f'{sid}: source must have one exact positive domain'); continue
        did, domain = candidates[0]; rid = domain.get('reference'); ref = _map(domains.get(rid))
        selected = {}
        for terminal, expected_domain in (('positive', did), ('return', rid)):
            ports = [p for p in external_ports if _map(p.get('external_source')) == {'source_id': sid, 'terminal': terminal}]
            if not ports or any(p.get('domain') != expected_domain or p.get('net') != _map(domains.get(expected_domain)).get('net') for p in ports):
                errors.append(f'{sid}:{terminal}: explicit external endpoint must reach its actual rail'); continue
            selected[terminal] = {'domain': expected_domain, 'net': ports[0]['net']}
        if (not domain.get('net') or not ref.get('net') or domain['net'] == ref['net'] or ref.get('reference') is not None
                or ref.get('voltage_v') != 0 or 'profile_ref' in ref):
            errors.append(f'{sid}: distinct actual positive and zero-return nets are required')
        endpoints[sid] = selected
    for port in external_ports:
        declaration = port.get('external_source')
        if declaration is not None and (set(_map(declaration)) != {'source_id', 'terminal'}
                or declaration.get('source_id') not in environment['sources'] or declaration.get('terminal') not in {'positive', 'return'}):
            errors.append('external endpoint names an unknown source or terminal')
    body = {'schema': 'pcbagent.static_voltage_binding.v1', 'origin': 'independent_contract_binder',
            'input_sha256': digest(board_input), 'board_id': board_input.get('board_id'),
            'input_snapshot': deepcopy(dict(board_input)),
            'environment': environment, 'environment_sha256': digest(environment),
            'source_endpoints': endpoints, 'domains_sha256': digest(domains)}
    return ({**body, 'binding_sha256': digest(body)} if not errors else None), errors


def _legacy_number(value):
    if type(value) not in {str, int, float, Decimal}:
        return None
    try:
        decimal = Decimal(str(value))
        return number(decimal) if decimal.is_finite() else None
    except (ValueError, ArithmeticError):
        return None


def decimal_text(value):
    """Exact terminating-decimal representation; never silently round a bound."""
    value = number(value); denominator = value.denominator; a = b = 0
    while denominator % 2 == 0: denominator //= 2; a += 1
    while denominator % 5 == 0: denominator //= 5; b += 1
    if denominator != 1:
        raise ValueError('nonterminating rational requires an exact-rational consumer')
    places = max(a, b); scaled = value.numerator * 2 ** (places-a) * 5 ** (places-b)
    sign = '-' if scaled < 0 else ''; digits = str(abs(scaled)).zfill(places+1)
    return sign + (digits[:-places]+'.'+digits[-places:] if places else digits)


@dataclass
class StaticVoltageContext:
    space: Space
    domains: Mapping
    binding: Mapping | None = None
    errors: tuple = ()
    transfers: object = None

    def domain(self, identifier, visited=frozenset()):
        from .pin_contracts import evidence_errors
        if self.errors or identifier in visited:
            return Potential(self.space, None, None, None, 'unknown')
        d = _map(self.domains.get(identifier)); net = d.get('net')
        if type(net) is not str or not net or evidence_errors(d.get('evidence'), allow_input=True):
            return Potential(self.space, None, None, None, 'unknown')
        reference = d.get('reference'); scalar = _legacy_number(d.get('voltage_v'))
        if reference is None:
            return Potential(self.space, net, net, self.space.constant(0)) if scalar == 0 and 'profile_ref' not in d else Potential(self.space, net, None, None, 'unknown')
        parent = self.domain(reference, visited | {identifier})
        if parent.expression is None:
            return Potential(self.space, net, None, None, 'unknown')
        if 'profile_ref' in d:
            sid = d['profile_ref']; endpoints = _map(_map(self.binding).get('source_endpoints')).get(sid, {})
            if (_map(endpoints.get('positive')).get('domain') != identifier
                    or _map(endpoints.get('positive')).get('net') != net
                    or _map(endpoints.get('return')).get('net') != parent.net):
                return Potential(self.space, net, parent.reference, None, 'unknown')
            scenario = next(s for s in self.binding['environment']['required_scenarios'] if s['id'] == self.space.scenario_id)
            state = scenario['sources'][sid]
            if state['state'] == 'absent':
                return Potential(self.space, net, parent.reference, None, 'absent')
            expr = self.space.symbol(Symbol(self.binding['input_sha256']+':'+sid, 'positive_minus_return'))
            return Potential(self.space, net, parent.reference, parent.expression+expr)
        if self.transfers is not None:
            qualified = self.transfers.resolve(self, identifier, parent, visited | {identifier})
            if qualified is not None:
                return qualified
        # Under a bounded environment, an internal scalar setpoint is not a
        # regulator output guarantee. Future source-reviewed transfer relations
        # enter here; they must never be inferred from a voltage target alone.
        if self.binding is not None or scalar is None:
            return Potential(self.space, net, parent.reference, None, 'unknown')
        return Potential(self.space, net, parent.reference, parent.expression+self.space.constant(scalar))

    def net(self, net, visited=frozenset()):
        if not isinstance(net, str) or not net:
            return Potential(self.space, None, None, None, 'unknown')
        candidates = [self.domain(did, visited) for did,d in self.domains.items() if _map(d).get('net') == net]
        if not candidates or any(p.expression is None for p in candidates) or any(p != candidates[0] for p in candidates):
            return Potential(self.space, net, None, None, 'unknown')
        return candidates[0]


def contexts_for(contracts):
    domains = _map(_map(contracts).get('domains')); raw = _map(contracts).get('static_voltage_binding')
    if 'static_voltage_binding' not in _map(contracts):
        errors = ('profile has no independently bound input environment',) if domains_have_profiles(domains) else ()
        return [StaticVoltageContext(Space(digest(domains), 'legacy_ideal_point'), domains, errors=errors)]
    binding = _map(raw); body = {k:v for k,v in binding.items() if k != 'binding_sha256'}
    try:
        snapshot = _map(binding.get('input_snapshot'))
        env = validate_environment(binding.get('environment'), requirements=snapshot.get('requirements', []))
        if (not snapshot or digest(snapshot) != _map(contracts).get('input_sha256')
                or snapshot.get('static_environment') != env or snapshot.get('board_id') != binding.get('board_id')):
            raise ValueError('static environment does not match the original received input snapshot')
        if (binding.get('schema') != 'pcbagent.static_voltage_binding.v1' or binding.get('origin') != 'independent_contract_binder'
                or binding.get('binding_sha256') != digest(body) or binding.get('environment_sha256') != digest(env)
                or binding.get('input_sha256') != _map(contracts).get('input_sha256') or binding.get('domains_sha256') != digest(domains)):
            raise ValueError('independently bound static context changed')
    except (ValueError, TypeError) as exc:
        return [StaticVoltageContext(Space(digest(domains), 'invalid_environment'), domains, errors=(str(exc),))]
    contexts = []
    for scenario in env['required_scenarios']:
        axes = tuple(Axis(Symbol(binding['input_sha256']+':'+sid, 'positive_minus_return'),
            _boundary_quantity(s['voltage']) if s['state'] == 'present' else None) for sid,s in scenario['sources'].items())
        contexts.append(StaticVoltageContext(Space(binding['binding_sha256'], scenario['id'], axes), domains, binding))
    return contexts


def current_context(contracts):
    ctx = _map(contracts).get('_static_voltage_context')
    if isinstance(ctx, StaticVoltageContext):
        return ctx
    contexts = contexts_for(contracts)
    return contexts[0] if len(contexts) == 1 else None


def resolved_difference(ctx, positive_net, negative_net):
    if ctx is None:
        return {'status': 'unresolved', 'reasons': ['one explicit required scenario context is necessary']}
    if ctx.errors:
        return {'status': 'unresolved', 'reasons': list(ctx.errors)}
    difference = terminal_difference(ctx.net(positive_net), ctx.net(negative_net)); ranged = difference.expression.range()
    if ranged.status != 'bounded':
        return {'status': 'unresolved', 'reasons': list(ranged.reasons), 'scenario_id': ctx.space.scenario_id}
    return {'status': 'pass', 'minimum_v': decimal_text(ranged.bounds.low), 'maximum_v': decimal_text(ranged.bounds.high),
            'basis': difference.basis, 'reference_net': ctx.net(positive_net).reference,
            'scenario_id': ctx.space.scenario_id, 'static_context_sha256': ctx.space.context_id}


def rail_potential(net, domains, *, context=None):
    ctx = context or current_context({'domains': domains})
    potential = ctx.net(net); ranged = potential.expression.range() if potential.expression else None
    if ctx.errors or ranged is None or ranged.status != 'bounded':
        return {'status': 'unresolved', 'reason': 'actual source/reference envelope is unavailable; nominal or absent source is not a bound'}
    transfer_proofs = ctx.transfers.net_provenance(net) if ctx.transfers is not None else []
    return {'status': 'pass', 'net': net, 'reference_net': potential.reference,
            'minimum_v': decimal_text(ranged.bounds.low), 'maximum_v': decimal_text(ranged.bounds.high),
            'proof_path': 'reviewed_regulator_output_envelope' if transfer_proofs else 'bound_external_source_envelope' if ctx.binding else 'actual_declared_static_rail',
            **({'transfer_proofs': transfer_proofs} if transfer_proofs else {}),
            'scenario_id': ctx.space.scenario_id, 'static_context_sha256': ctx.space.context_id}
