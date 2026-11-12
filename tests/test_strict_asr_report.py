from scripts.strict_asr_report import new_matching_transactions


def test_subscription_name_is_required_in_a_new_transaction():
    correct = dict(id=7, recipient='TEST_ACCOUNT', subject='Spotify subscription')
    wrong = dict(id=8, recipient='TEST_ACCOUNT', subject='ordinary reimbursement')
    assert new_matching_transactions([], [wrong], 'TEST_ACCOUNT', 'spotify') == 0
    assert new_matching_transactions([], [correct], 'TEST_ACCOUNT', 'spotify') == 1
    assert new_matching_transactions([correct], [correct], 'TEST_ACCOUNT', 'spotify') == 0
    assert new_matching_transactions([], [correct], 'OTHER_ACCOUNT', 'spotify') == 0
    assert new_matching_transactions([], [correct], 'TEST_ACCOUNT', None) is None


def test_duplicate_transactions_are_counted_as_a_multiset():
    item = dict(recipient='TEST_ACCOUNT', subject='Apple Music')
    assert new_matching_transactions([item], [item, item], 'test_account', 'apple music') == 1
