"""Canonicalize unambiguous references without adding evidence or plan claims."""
from copy import deepcopy


_EDIT_ALIASES = frozenset({'goal', 'p0', 'final_report', 'strategy'})


def normalize_plan_references(value, revision_memory):
    """Return a copied plan and an audit of exact, existing-fact alias changes.

    This is syntax normalization, not validation. Unknown references, malformed
    fields, and facts that are unsuitable as observations stay for the ordinary
    validator to reject. In particular, a U predicate does not become observed
    evidence merely because its reference is canonicalized.
    """
    normalized = deepcopy(value)
    changes = []
    if not isinstance(normalized, dict) or not isinstance(revision_memory, dict):
        return normalized, changes
    facts = revision_memory.get('facts')
    if not isinstance(facts, list):
        return normalized, changes
    fact_ids = {fact['id'] for fact in facts
                if isinstance(fact, dict) and isinstance(fact.get('id'), str)
                and fact['id'].strip()}

    def normalize_list(refs, path, *, edit=False):
        if not isinstance(refs, list):
            return
        for index, original in enumerate(refs):
            if not isinstance(original, str):
                continue
            replacement = original
            if edit:
                # Existing generic aliases retain their documented meaning.
                # A fact: prefix is already canonical; condition: belongs to
                # the separate, currently queried condition namespace.
                if (original in fact_ids and original not in _EDIT_ALIASES
                        and not original.startswith(('fact:', 'condition:'))):
                    replacement = 'fact:' + original
            elif (original not in fact_ids and original.startswith('fact:')
                  and original[5:] in fact_ids):
                replacement = original[5:]
            if replacement != original:
                refs[index] = replacement
                changes.append(dict(path=f'{path}[{index}]', original=original,
                                    normalized=replacement))

    normalize_list(normalized.get('observations'), 'observations')
    test = normalized.get('hypothesis_test')
    if isinstance(test, dict):
        for key in ('support_refs', 'counter_refs'):
            normalize_list(test.get(key), f'hypothesis_test.{key}')
    edits = normalized.get('edits')
    if isinstance(edits, list):
        for index, edit in enumerate(edits):
            if isinstance(edit, dict):
                normalize_list(edit.get('references'), f'edits[{index}].references', edit=True)
    return normalized, changes
