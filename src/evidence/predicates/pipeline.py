"""End-to-end public-scene retrieval -> discovery -> compile -> frozen evaluation."""
from .history import public_scene, retrieve
from .generation import discover, compile_predicates
from .engine import PredicateEngine, digest


def run_pipeline(case, memory, episode_id, policy, packets, *, backend=None,
                 excluded_groups=(), exclude_same_context=False, use_history=True):
    scene = public_scene(case)
    retrieved = retrieve(scene, memory if use_history else [], excluded_groups=excluded_groups,
                         exclude_same_context=exclude_same_context)
    # Validate packets BEFORE any potentially paid model invocation.
    engine = PredicateEngine(episode_id, policy)
    for packet in packets:
        engine.add_packet(packet)
    semantic = discover(scene, retrieved, backend)
    compiled = compile_predicates(semantic, scene, retrieved, episode_id, policy)
    engine.references = compiled['references']
    feedback = engine.evaluate(compiled['candidates'])
    feedback['rejected_candidates'] = compiled['rejections'] + feedback['rejected_candidates']
    feedback['evidence_requirements'] += compiled['evidence_requirements']
    return dict(protocol='scene-predicate-pipeline-v1', generator='model' if backend else 'rules-v1',
                current_scene=scene, retrieval=retrieved, semantic_predicates=semantic,
                compilation=compiled, feedback=feedback,
                manifest=dict(scene_hash=digest(scene), memory_hash=digest(memory),
                              packets_hash=digest(packets), policy_hash=digest(policy),
                              evidence_used_for_generation=False, use_history=use_history))
