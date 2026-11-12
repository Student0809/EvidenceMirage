"""Validate variable substitution without claiming semantic or execution success."""
import re


def goal_literals(goal,candidate):
    """Exact public-string diagnostic, not a semantic goal-preservation verdict."""
    literals=set(re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',goal))
    literals.update(x.rstrip('.,') for x in re.findall(r'''https?://[^\s'"<>]+|www\.[^\s'"<>]+''',goal))
    literals.update(re.findall(r'\b[A-Z]{2}\d{10,32}\b|\b\d{4}-\d{2}-\d{2}\b|\b\d+\.\d{2}\b',goal))
    literals.update(re.findall(r'"([^"\n]{2,})"',goal))
    literals.update(re.findall(r"(?<!\w)'([^'\n]{2,})'(?!\w)",goal))
    return dict(selected_public_literals=[dict(text=x,present=x in candidate) for x in sorted(literals)],
                semantic_retention='not_independently_audited',
                interpretation='Exact strings from the public goal only; absence is not semantic failure.')


def substitution(attempt,payload):
    if re.search(r'\{INJECTION:|\[IPI_PLACEHOLDER',payload.text):
        raise ValueError('Candidate contains unresolved injection placeholders')
    context=attempt.case['context_with_placeholder']
    vectors=attempt.case['injection_candidates']
    markers=re.findall(r'\{INJECTION:([^}]+)\}',context)
    if set(markers)!=set(vectors):
        raise ValueError('Context markers differ from selected injection variables')
    preview=context
    for vector in vectors:
        preview=preview.replace('{INJECTION:'+vector+'}',payload.text)
    return dict(status='passed',occurrences=len(markers),
                injections={vector:payload.text for vector in vectors},
                goal_literal_diagnostic=goal_literals(attempt.case['injection_task_goal'],payload.text),
                instantiated_context=preview,interpretation='Variable substitution only; execution unverified')
