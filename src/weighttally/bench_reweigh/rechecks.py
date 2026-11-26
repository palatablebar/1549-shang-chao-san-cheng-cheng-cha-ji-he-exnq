"""当班复测单加签。

复测沿用本班冻结的特征版本、模型和校准版本重新预测：原分数过线的派单票，
只有选中的改测量仍落在训练包络内、且复测预测降到派单线以下时才可加签。
原派单先占工位，复测只使用剩余工位和分区剩余额度；暂停票不参与，同票只签一种。
"""

from collections import defaultdict
from dataclasses import dataclass, replace
from math import isfinite
from typing import Callable, Iterable

from ..scale_slips.contracts import FIELDS
from ..variance_context.contracts import RAW_KEYS
from ..variance_context.interventions import propose_bench_reweigh
from ..variance_context.joint import options
from ..variance_context.service import numeric_fields


@dataclass(frozen=True, slots=True)
class Recheck:
    key: str
    cohort: str
    action: str
    work: int
    line: float
    base: float
    revised: float
    changes: tuple[tuple[str, float, float], ...]

    def __post_init__(self):
        if not self.key or not self.cohort or not self.action:
            raise ValueError('复测单缺少单号、作业区或复测安排')
        if type(self.work) is not int or not 1 <= self.work <= 12:
            raise ValueError('复测用量不在有效范围')
        if not all(isfinite(value) for value in (self.line, self.base, self.revised)):
            raise ValueError('复测前后风险必须是有限值')
        if self.base < self.line:
            raise ValueError('原分数未过派单线的单据不能加签复测')
        if self.revised >= self.base:
            raise ValueError('加签复测必须降低风险')
        if self.revised >= self.line:
            raise ValueError('复测预测未降到派单线以下')
        if not self.changes:
            raise ValueError('复测单必须登记选中的改测量')
        for field, before, after in self.changes:
            if field not in RAW_KEYS or before == after:
                raise ValueError('复测改测量无效')
            low, high = FIELDS[field][2], FIELDS[field][3]
            if not low <= after <= high:
                raise ValueError('复测改测量超出字段有效区间')

    @property
    def drop(self):
        return self.base-self.revised

    def signature(self):
        return self.cohort, self.key, self.action

    def evidence(self):
        return {'复测单': self.key, '作业区': self.cohort, '复测安排': self.action,
                '用量': self.work, '派单线': round(self.line, 4),
                '原预测': round(self.base, 4), '复测预测': round(self.revised, 4),
                '风险变化': round(self.revised-self.base, 4), '降险': round(self.drop, 4),
                '改测量': {field: {'原值': before, '替换值': after}
                          for field, before, after in self.changes}}

    def summary(self):
        labels = '、'.join(f'{FIELDS[field][0]} {before:g}→{after:g}'
                          for field, before, after in self.changes)
        row = self.evidence()
        row['改测量'] = labels
        return row


@dataclass(frozen=True, slots=True)
class RecheckPlan:
    tickets: tuple[Recheck, ...]
    capacity: int
    original_used: int
    used: int
    balance: int
    group_quota: int
    line: float | None
    used_by_group: tuple[tuple[str, int], ...]
    zone_total: tuple[tuple[str, int], ...]

    def __post_init__(self):
        if self.original_used + self.used + self.balance != self.capacity:
            raise ArithmeticError('复测用量与原派单额度不平')
        if self.original_used < 0 or self.used < 0 or self.balance < 0:
            raise ArithmeticError('复测用量不能为负')
        if len({ticket.key for ticket in self.tickets}) != len(self.tickets):
            raise ArithmeticError('同一张单据不能重复加签复测')
        if sum(work for _, work in self.used_by_group) != self.used:
            raise ArithmeticError('复测分区用量与总用量不符')
        if sum(work for _, work in self.zone_total) != self.original_used+self.used:
            raise ArithmeticError('分区占用与原派单无法对账')
        if any(work > self.group_quota for _, work in self.zone_total):
            raise ArithmeticError('复测超出分区额度上限')

    @property
    def coverage(self):
        return tuple(sorted({ticket.cohort for ticket in self.tickets}))

    def evidence(self):
        return {'capacity': self.capacity, 'original_used': self.original_used,
                'used': self.used, 'balance': self.balance, 'group_quota': self.group_quota,
                'line': self.line, 'coverage': list(self.coverage),
                'used_by_group': [list(item) for item in self.used_by_group],
                'zone_total': [list(item) for item in self.zone_total],
                'tickets': [ticket.evidence() for ticket in self.tickets]}

    def ledger_evidence(self):
        rows = [{'核对项': '原派单用量', '数量': self.original_used},
                {'核对项': '复测加签单量', '数量': len(self.tickets)},
                {'核对项': '复测用量', '数量': self.used},
                {'核对项': '复测后余位', '数量': self.balance},
                {'核对项': '覆盖作业区', '数量': len(self.coverage)}]
        rows.extend({'核对项': '复测分区 '+zone, '数量': work} for zone, work in self.used_by_group)
        rows.extend({'核对项': '分区合计 '+zone, '数量': total} for zone, total in self.zone_total)
        rows.append({'核对项': '分区上限', '数量': self.group_quota})
        return rows


def _path_key(path):
    return tuple(sorted(ticket.signature() for ticket in path))


def _prefer(value, path, reference):
    benchmark, route = reference
    if round(value, 12) != round(benchmark, 12):
        return value > benchmark
    return _path_key(path) < _path_key(route)


def _zone_options(candidates, headroom):
    """每个作业区在分区剩余额度内可达的 (用量, 降险, 复测单) 组合。"""
    grouped = defaultdict(lambda: defaultdict(list))
    for candidate in candidates:
        grouped[candidate.cohort][candidate.key].append(candidate)
    by_zone = []
    for zone in sorted(grouped):
        limit = headroom.get(zone, 0)
        if limit <= 0:
            continue
        states = {0: (0.0, ())}
        for key in sorted(grouped[zone]):
            expanded = dict(states)
            for cost, (drop, path) in states.items():
                for candidate in grouped[zone][key]:
                    spent = cost+candidate.work
                    if spent > limit:
                        continue
                    route = path+(candidate,)
                    known = expanded.get(spent)
                    if known is None or _prefer(drop+candidate.drop, route, known):
                        expanded[spent] = (drop+candidate.drop, route)
            states = expanded
        reached = [(cost, drop, path) for cost, (drop, path) in sorted(states.items()) if path]
        if reached:
            by_zone.append((zone, reached))
    return by_zone


def select_rechecks(candidates: Iterable[Recheck], budget: int, headroom) -> tuple[Recheck, ...]:
    """在剩余工位与分区剩余额度内挑选复测单。

    目标按字典序：覆盖的作业区数尽可能多，其次合计降险尽可能大；
    仍相同时取用量更省、单号与安排更靠前的方案，保证重跑结论一致。
    """
    if type(budget) is not int or budget < 0:
        raise ValueError('复测工位余额须为非负整数')
    if any(amount < 0 for amount in headroom.values()):
        raise ValueError('分区剩余额度不能为负')
    pool = tuple(candidates)
    if not pool:
        return ()
    zones = _zone_options(pool, headroom)
    if not zones:
        return ()

    states = {(0, 0): (0.0, ())}
    for _zone, choices in zones:
        expanded = dict(states)
        for (covered, cost), (drop, path) in states.items():
            for work, gain, picked in choices:
                spent = cost+work
                if spent > budget:
                    continue
                slot = (covered+1, spent)
                route = path+picked
                known = expanded.get(slot)
                if known is None or _prefer(drop+gain, route, known):
                    expanded[slot] = (drop+gain, route)
        states = expanded

    best = None
    for (covered, cost), (drop, path) in states.items():
        ranked = (covered, round(drop, 12), -cost)
        if best is None or ranked > best[0] or (ranked == best[0] and _path_key(path) < _path_key(best[1])):
            best = (ranked, path)
    if best is None:
        return ()
    return tuple(sorted(best[1], key=lambda ticket: ticket.signature()))


def sign_rechecks(plan, vectors, reference, score: Callable) -> RecheckPlan:
    """按冻结的特征版本与校准版本，为当班原派单加签复测单。

    原派单已占用的工位和分区额度不会被改写；复测只从余位重新计算，
    因此同一班次重跑得到相同结果，不会重复占位。
    """
    spent = dict(plan.used_by_group)
    original_used = sum(job.cost for job in plan.jobs if job.state == '已分派')
    if original_used != plan.capacity-plan.balance:
        raise ArithmeticError('原派单用量与工位余额不符')
    headroom = {job.cohort: plan.group_quota-spent.get(job.cohort, 0) for job in plan.jobs}
    candidates = []
    for job in plan.jobs:
        # 只给已过派单线的原派单加签；暂停票与未分派票都不参与。
        if job.state != '已分派' or job.estimate < plan.line:
            continue
        vector = vectors.get(job.key)
        if vector is None:
            raise ValueError('复测单缺少当班特征向量')
        candidates.extend(_ticket_candidates(job, vector, reference, score, plan.line))
    picked = select_rechecks(candidates, plan.balance, headroom)
    used_by_group = tuple(sorted((zone, sum(ticket.work for ticket in picked if ticket.cohort == zone))
                                 for zone in sorted({ticket.cohort for ticket in picked})))
    totals = defaultdict(int, spent)
    for zone, work in used_by_group:
        totals[zone] += work
    used = sum(ticket.work for ticket in picked)
    return RecheckPlan(picked, plan.capacity, original_used, used, plan.balance-used,
                       plan.group_quota, plan.line, used_by_group, tuple(sorted(totals.items())))


def _ticket_candidates(job, vector, reference, score, line):
    cases = []
    for proposal in propose_bench_reweigh(vector.input_values, vector.missing):
        cases.append((proposal.name, {proposal.field: (proposal.before, proposal.after)}, proposal.work))
    for action in options(vector.input_values, vector.missing):
        cases.append((action.case, dict(action.changes), action.work_units))
    found = []
    for action, changes, work in cases:
        if not changes or any(before == after for before, after in changes.values()):
            continue
        if any(not FIELDS[field][2] <= pair[1] <= FIELDS[field][3] for field, pair in changes.items()):
            continue
        adjusted = tuple(changes[field][1] if field in changes else value
                         for field, value in zip(RAW_KEYS, vector.input_values, strict=True))
        normalized, measured, flags = numeric_fields(adjusted, reference)
        # 改测量必须仍落在训练包络内；超训练域的试算不能当作复测凭据。
        if flags or not within_training(measured, reference):
            continue
        scenario = replace(vector, values=normalized, input_values=adjusted,
                           factors=measured, excursions=flags)
        revised = score(scenario)
        if not isfinite(revised) or revised >= line or revised >= job.estimate:
            continue
        ordered = tuple((field, *changes[field]) for field in RAW_KEYS if field in changes)
        found.append(Recheck(job.key, job.cohort, action, work, line, job.estimate, revised, ordered))
    return tuple(found)


def within_training(factors, reference):
    """改测量换算出的特征须逐项落在训练最小到最大的包络内。"""
    return all(low <= value <= high
               for value, low, high in zip(factors, reference.floor, reference.ceil, strict=True))


def recheck_evidence(plan: RecheckPlan):
    rows = [{'处置': f'复测加签（覆盖 {len(plan.coverage)} 个作业区）', '额度': plan.used},
            {'处置': '复测后剩余额度', '额度': plan.balance}]
    rows.extend({'处置': '复测分区 '+zone, '额度': work} for zone, work in plan.used_by_group)
    return rows
