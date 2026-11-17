"""Only the newly failed terminal-punctuation boundary; prior tests excluded."""

from mgar_v3.super_agent.source_case_experiments import canonical_request


def test_missing_final_chinese_full_stop_preserves_fixed_request_without_relaxing_constraints():
    request = (
        "请实现 MemoryRetriever 的 rrf_k 可调参数：默认60，严格整数范围0到10000，实际传入RRF融合；"
        "与 rag.rank.rrf 共用相同参数约束，非法值抛 RRF_K_INVALID，保留默认行为，不修改测试。"
    )
    assert canonical_request("@_user_1 " + request[:-1]) == canonical_request(request)
    assert canonical_request(request.replace("默认60", "默认0")) != canonical_request(request)
    assert canonical_request(request.replace("不修改测试", "修改测试")) != canonical_request(
        request
    )
    assert canonical_request("不要" + request) != canonical_request(request)
    assert canonical_request("module.py") != canonical_request("modulepy")
