import pytest

from weighttally.bench_reweigh.rechecks import (
    Recheck, RecheckPlan, _ticket_candidates, select_rechecks, within_training,
)
from weighttally.pipeline import preview
from weighttally.variance_context.service import derive


def make_recheck(key, zone, action, work, base, revised, *, line=0.6, field='actual_g', before=500.0, after=900.0):
    return Recheck(key, zone, action, work, line, base, revised, ((field, before, after),))


def test_recheck_requires_crossing_below_line():
    with pytest.raises(ValueError, match='派单线'):
        make_recheck('K1', '甲区', '重测', 1, 0.9, 0.7)
    with pytest.raises(ValueError, match='派单线'):
        make_recheck('K1', '甲区', '重测', 1, 0.4, 0.2)


def test_recheck_requests_change_within_field_range():
    with pytest.raises(ValueError, match='有效区间'):
        make_recheck('K1', '甲区', '重测', 1, 0.9, 0.2, field='actual_g', after=99999)
    with pytest.raises(ValueError, match='改测量'):
        make_recheck('K1', '甲区', '重测', 1, 0.9, 0.2, field='actual_g', before=500.0, after=500.0)


def test_recheck_plan_must_reconcile_with_original_dispatch():
    ticket = make_recheck('K1', '甲区', '重测', 2, 0.9, 0.2)
    plan = RecheckPlan((ticket,), 12, 8, 2, 2, 10, 0.6, (('甲区', 2),), (('甲区', 10),))
    assert plan.balance == 2
    with pytest.raises(ArithmeticError, match='不平'):
        RecheckPlan((ticket,), 12, 8, 2, 3, 10, 0.6, (('甲区', 2),), (('甲区', 10),))
    with pytest.raises(ArithmeticError, match='重复'):
        RecheckPlan((ticket, ticket), 12, 8, 4, 0, 10, 0.6, (('甲区', 4),), (('甲区', 12),))


def test_recheck_plan_enforces_group_ceiling():
    ticket = make_recheck('K1', '甲区', '重测', 3, 0.9, 0.2)
    with pytest.raises(ArithmeticError, match='分区'):
        RecheckPlan((ticket,), 20, 8, 3, 9, 8, 0.6, (('甲区', 3),), (('甲区', 11),))


def test_selection_prefers_more_zones_over_larger_drop():
    # 余位只够两票：全投甲区合计降险 1.70，跨甲、乙两区只有 0.85，但覆盖数优先。
    deep = [make_recheck('A1', '甲区', '重测', 2, 0.95, 0.10),
            make_recheck('A2', '甲区', '重测', 2, 0.95, 0.10)]
    thin = [make_recheck('B1', '乙区', '重测', 2, 0.70, 0.30)]
    chosen = select_rechecks(deep + thin, 4, {'甲区': 4, '乙区': 4})
    assert {ticket.cohort for ticket in chosen} == {'甲区', '乙区'}
    assert sum(ticket.key in {'A1', 'A2'} for ticket in chosen) == 1
    assert 'B1' in {ticket.key for ticket in chosen}
    assert sum(ticket.work for ticket in chosen) == 4


def test_selection_uses_risk_drop_when_coverage_is_equal():
    # 覆盖数相同（各一区一票）时，比较合计降险。
    weak = make_recheck('A1', '甲区', '重测', 1, 0.9, 0.5)
    strong = make_recheck('A2', '甲区', '重测', 1, 0.9, 0.2)
    other = make_recheck('B1', '乙区', '重测', 1, 0.9, 0.5)
    chosen = select_rechecks([weak, strong, other], 2, {'甲区': 4, '乙区': 4})
    assert {ticket.key for ticket in chosen} == {'A2', 'B1'}


def test_selection_respects_budget_and_zone_headroom():
    tickets = [make_recheck('A1', '甲区', '重测', 3, 0.9, 0.1),
               make_recheck('B1', '乙区', '重测', 3, 0.9, 0.1)]
    assert select_rechecks(tickets, 2, {'甲区': 4, '乙区': 4}) == ()
    assert [ticket.key for ticket in select_rechecks(tickets, 6, {'甲区': 4, '乙区': 2})] == ['A1']
    # 分区余位不足时，即使总余位充足也不能加签。
    assert select_rechecks(tickets, 6, {'甲区': 2, '乙区': 2}) == ()


def test_selection_never_signs_same_ticket_twice():
    first = make_recheck('A1', '甲区', '重测', 1, 0.9, 0.2)
    second = make_recheck('A1', '甲区', '复称', 1, 0.9, 0.4)
    chosen = select_rechecks([first, second], 4, {'甲区': 4})
    assert len(chosen) == 1 and chosen[0].key == 'A1'


def test_sample_recheck_is_signed_on_frozen_run():
    report = preview()
    stage = report['sections'][6]
    signed = [row for row in report['rows'] if row['recheck']]
    assert signed
    for row in signed:
        proof = row['recheck']
        assert row['state'] == '已分派'
        assert proof['原预测'] >= proof['派单线'] and proof['复测预测'] < proof['派单线']
        assert proof['复测预测'] < proof['原预测']
        for field, pair in proof['改测量'].items():
            assert pair['原值'] == row['measurements'][field]
        # 试算值不写回实测。
        for field, pair in proof['改测量'].items():
            assert row['measurements'][field] == pair['原值']
    assert stage['recheck_rows']
    assert stage['recheck_summary']['tickets']


def test_sample_recheck_ledger_balances_with_original_dispatch():
    report = preview()
    stage = report['sections'][6]
    summary = stage['recheck_summary']
    original_used = sum(row['cost'] for row in report['rows'] if row['state'] == '已分派')
    assert summary['original_used'] == original_used
    assert original_used == report['capacity'] - report['metrics'][-1]['value']
    assert summary['original_used'] + summary['used'] + summary['balance'] == report['capacity']
    assert summary['balance'] == report['metrics'][-1]['value'] - summary['used']
    for zone, total in summary['zone_total']:
        assert total <= summary['group_quota']


def test_recheck_runs_are_stable():
    first = preview()
    second = preview()
    assert first['sections'][6]['recheck_rows'] == second['sections'][6]['recheck_rows']


def test_paused_ticket_cannot_be_rechecked():
    # 暂停票即使分数过线也不参与复测：这里直接验证选择器不会拿到暂停票。
    report = preview()
    paused = {row['key'] for row in report['rows'] if row['state'] == '暂停'}
    signed = {row['key'] for row in report['rows'] if row['recheck']}
    assert not (paused & signed)


def test_out_of_envelope_change_cannot_be_signed():
    # TRAP-61：改动后特征超出训练最小/最大包络时，不得充当复测凭据。
    from weighttally.bench_reweigh.contracts import Job
    from weighttally.pipeline import ShiftDossier, open_fixture

    dossier = ShiftDossier(open_fixture()['records'], 96)
    reference = dossier.feature_stamp
    vector = next(item for item in dossier.prepared.live if item.key == 'WT-1268')
    job = Job(vector.key, vector.cohort, .9, None, None, 1, '已分派', 'RETURN_DISPUTE', '', .9, ('RETURN_DISPUTE',))
    # 假设每个改测量都把风险压到派单线以下；此时仍须由训练包络筛掉越界试算。
    always_low = lambda scenario: .1
    signed = _ticket_candidates(job, vector, reference, always_low, .6)
    names = {ticket.action for ticket in signed}
    for ticket in signed:
        adjusted = tuple(dict((field, (before, after)) for field, before, after in ticket.changes)
                         .get(field, (value, value))[1]
                         for field, value in zip(('ticket_g', 'actual_g', 'tare_g', 'temp_c', 'bins', 'return_g'),
                                                 vector.input_values, strict=True))
        assert within_training(derive(adjusted), reference), ticket.action
    zero_return = derive((vector.input_values[0], vector.input_values[1], vector.input_values[2],
                          vector.input_values[3], vector.input_values[4], 0.0))
    assert not within_training(zero_return, reference)
    assert '置零退秤重称' not in names and '取消争议退秤读数' not in names


def test_within_training_is_inclusive_at_training_bounds():
    reference = type('Ref', (), {'floor': (-1.0, 0.0), 'ceil': (1.0, 5.0)})()
    assert within_training((-1.0, 5.0), reference)
    assert within_training((1.0, 0.0), reference)
    assert not within_training((-1.1, 1.0), reference)
    assert not within_training((0.0, 5.1), reference)


def test_within_training_is_inclusive_at_training_bounds():
    reference = type('Ref', (), {'floor': (-1.0, 0.0), 'ceil': (1.0, 5.0)})()
    assert within_training((-1.0, 5.0), reference)
    assert within_training((1.0, 0.0), reference)
    assert not within_training((-1.1, 1.0), reference)
    assert not within_training((0.0, 5.1), reference)


def test_selection_is_independent_of_candidate_order():
    deep = [make_recheck('A1', '甲区', '重测', 2, 0.95, 0.10),
            make_recheck('A2', '甲区', '复称', 2, 0.95, 0.10),
            make_recheck('B1', '乙区', '重测', 2, 0.70, 0.30),
            make_recheck('C1', '丙区', '重测', 1, 0.80, 0.20)]
    headroom = {'甲区': 4, '乙区': 4, '丙区': 4}
    forward = select_rechecks(deep, 4, headroom)
    backward = select_rechecks(list(reversed(deep)), 4, headroom)
    assert forward == backward


def test_selection_rejects_negative_budget_and_headroom():
    ticket = make_recheck('A1', '甲区', '重测', 1, 0.9, 0.2)
    with pytest.raises(ValueError, match='余额'):
        select_rechecks([ticket], -1, {'甲区': 4})
    with pytest.raises(ValueError, match='额度'):
        select_rechecks([ticket], 4, {'甲区': -2})


def test_sample_recheck_uses_remaining_capacity_only():
    tight = preview(8, 'balanced')
    wide = preview(12, 'balanced')
    for report in (tight, wide):
        summary = report['sections'][6]['recheck_summary']
        assert summary['used'] <= report['metrics'][-1]['value']
        assert [ticket['复测单'] for ticket in summary['tickets']].count('WT-1258') == 0
