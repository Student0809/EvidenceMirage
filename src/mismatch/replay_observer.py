"""Permitted receipt view of controlled logs; never reads state or goal annotations."""
from copy import deepcopy
import json
import re

WRITE_TOOLS = {
    'workspace': {'send_email', 'delete_email', 'delete_file', 'create_calendar_event', 'append_to_file'},
    'banking': {'send_money', 'update_scheduled_transaction', 'schedule_transaction'},
    'slack': {'send_direct_message', 'send_channel_message', 'post_webpage',
              'invite_user_to_slack', 'add_user_to_channel', 'remove_user_from_slack'},
    'travel': {'send_email', 'create_calendar_event', 'reserve_hotel', 'reserve_restaurant', 'reserve_car_rental'},
}


def neutralize(value):
    """Display-only normalization; original evidence bytes remain frozen."""
    if isinstance(value, str):
        return re.sub(r'\[IPI_PLACEHOLDER[^\]]*\]', '[injection slot omitted]', value)
    if isinstance(value, list):
        return [neutralize(v) for v in value]
    if isinstance(value, dict):
        return {k: neutralize(v) for k, v in value.items()}
    return value


def replay_packets(evidence, suite):
    permitted = WRITE_TOOLS[suite]
    packets = []
    for step in evidence['steps']:
        call = step['call']
        if call['function'] not in permitted:
            continue
        receipt = neutralize(dict(call=dict(function=call['function'], args=deepcopy(call['args'])),
                       result=deepcopy(step['result']), error=step.get('error')))
        packets.append(dict(query_id=f'event_{len(packets)}',
            description='This particular permitted tool call returned successfully; not whole-goal truth.',
            scope='returned_call_fields_only',
            response=dict(supported=step.get('error') is None,
                          evidence=json.dumps(receipt, ensure_ascii=False))))
    return packets
