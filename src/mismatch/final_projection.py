"""Text-only final projection for the probe-sampling arms.

The legacy black-box projection includes final tool-call names and arguments.
For probe sampling that would expose the report body or arbitrary send_email
arguments as raw tool text, bypassing the receiver's minimal projection.
"""


def extract_final_text_only(messages):
    last = None
    for message in messages:
        role = message.get('role') if isinstance(message, dict) else getattr(message, 'role', None)
        if role == 'assistant':
            last = message
    if last is None:
        return '(no assistant text)'
    content = (last.get('content') if isinstance(last, dict)
               else getattr(last, 'content', ''))
    text = ''
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text':
                text += block.get('content', '') or block.get('text', '')
    text = (text or '').strip()
    return text if text else '(no assistant text)'
