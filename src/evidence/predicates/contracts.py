"""Versioned field allowlist; no permissions are inferred by generated predicates."""
VERSION = 'predicate-contracts-v1.1'
# Query -> scalar type, allowed record fields. None means count-only.
CONTRACTS = {
    'email_received': ('count', {}),
    'received_email_subjects': ('text', {}),
    'received_email_bodies': ('text', {}),
    'received_email_recipients': ('text', {}),
    'received_attachment_counts': ('number', {}),
    'payment_received': ('count', {}),
    'payment_records': ('record', {'amount': 'number', 'subject': 'text', 'date': 'text', 'recipient': 'text'}),
    'payment_total': ('total', {}),
    'website_visited': ('count', {}),
    'website_requests': ('record', {'url': 'text', 'kind': 'text'}),
    'website_uploads': ('record', {'url': 'text', 'kind': 'text', 'content': 'text'}),
    'slack_direct_messages': ('record', {'body': 'text', 'user': 'text', 'kind': 'text'}),
    'slack_channel_messages': ('record', {'body': 'text', 'channel': 'text', 'kind': 'text'}),
    # Membership records have heterogeneous fields; only common fields exposed in M1.
    'slack_membership_events': ('record', {'user': 'text', 'kind': 'text'}),
}
OPERATORS = {'exists_event', 'exists_equal', 'exists_contains', 'exists_starts_with', 'all_numeric_le', 'sum_equal', 'count_equal'}
