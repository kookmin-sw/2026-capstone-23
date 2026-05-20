"""흐름도 노드/엣지 F1 평가 메트릭"""
import re
from typing import List, Optional, Tuple


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _edge_key(edge: list) -> Tuple[str, str]:
    """엣지를 (출발, 도착) 튜플로 정규화 (레이블 무시)"""
    return (_normalize(edge[0]), _normalize(edge[1]))


def _f1(pred: list, gt: list, key_fn) -> dict:
    if not gt:
        return None
    if not pred:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}

    pred_keys = [key_fn(x) for x in pred]
    gt_keys = [key_fn(x) for x in gt]

    gt_counts: dict = {}
    for k in gt_keys:
        gt_counts[k] = gt_counts.get(k, 0) + 1

    matched = 0
    for k in pred_keys:
        if gt_counts.get(k, 0) > 0:
            matched += 1
            gt_counts[k] -= 1

    precision = matched / len(pred_keys)
    recall = matched / len(gt_keys)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4)}


def parse_flow_output(text: str) -> Tuple[List[str], List[list]]:
    """
    FLOWCHART_PROMPT 출력에서 노드/엣지 파싱.
    형식:
      ## 노드
      - 노드명
      ## 엣지
      - 출발 → 도착 [조건]
    """
    nodes: List[str] = []
    edges: List[list] = []

    node_m = re.search(r"##\s*노드\s*\n(.*?)(?=##|$)", text, re.DOTALL)
    if node_m:
        for line in node_m.group(1).splitlines():
            line = re.sub(r"^\s*[-*]\s*", "", line).strip()
            if line:
                nodes.append(_normalize(line))

    edge_m = re.search(r"##\s*엣지\s*\n(.*?)(?=##|$)", text, re.DOTALL)
    if edge_m:
        for line in edge_m.group(1).splitlines():
            line = re.sub(r"^\s*[-*]\s*", "", line).strip()
            if not line:
                continue
            m = re.match(r"(.+?)\s*(?:→|->|>)\s*(.+?)(?:\s*\[(.+?)\])?$", line)
            if m:
                src = _normalize(m.group(1))
                tgt = _normalize(m.group(2))
                label = m.group(3)
                edges.append([src, tgt, label] if label else [src, tgt])

    return nodes, edges


def compute_flow_accuracy(
    pred_text: str,
    gt_nodes: Optional[List[str]],
    gt_edges: Optional[List[list]],
) -> Optional[dict]:
    """
    예측 텍스트에서 노드/엣지를 파싱하여 GT와 비교.
    GT가 없으면 None 반환.
    """
    if gt_nodes is None and gt_edges is None:
        return None

    pred_nodes, pred_edges = parse_flow_output(pred_text)

    node_score = None
    if gt_nodes is not None:
        node_score = _f1(pred_nodes, gt_nodes, lambda x: _normalize(x))

    edge_score = None
    if gt_edges is not None:
        edge_score = _f1(pred_edges, gt_edges, _edge_key)

    avg_f1 = None
    scores = [s["f1"] for s in [node_score, edge_score] if s is not None]
    if scores:
        avg_f1 = round(sum(scores) / len(scores), 4)

    return {
        "node": node_score,
        "edge": edge_score,
        "avg_f1": avg_f1,
        "pred_nodes": pred_nodes,
        "pred_edges": pred_edges,
    }
