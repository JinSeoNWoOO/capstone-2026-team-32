"""판단 기록의 해시 사슬 (설계 8장의 증빙, 결정 13).

DB 는 `data/` 아래라 git 에 들어가지 않는다. 그래서 판단 한 줄을 정규화한 JSON 으로 만들고
`record_hash = sha256(JSON + 직전 해시)` 로 사슬을 이어, 같은 줄을 git 이 추적하는
`ledger/advisor_ledger.jsonl` 에 덧붙인다. 원장 파일을 그날 커밋·push 하면 **커밋 시각이라는
외부 시각**으로 "판단이 사후에 수정되지 않았다"를 보일 수 있다.

두 가지를 의식적으로 정했다.

1. **원장은 mode='live' 만 남긴다.** 재현 모드(설계 7.4)는 과거 날짜로 몇 번이고 다시 돌리는
   기록이라 원장에 섞이면 "사후에 만들지 않았다"는 주장 자체가 흐려진다. DB 의 사슬은
   mode 별로 따로 잇는다.
2. **비중은 6자리로 반올림해 해싱한다.** 부동소수점 마지막 자리는 파이썬·SQLite·JSON 왕복에서
   달라질 수 있는데, 그 때문에 검증이 실패하면 사슬이 "수정 탐지"가 아니라 "환경 탐지"가 된다.
   6자리는 0.0001% 비중 차이라 판단의 동일성 판정에 충분하다.
"""
import hashlib
import json
import os

# 비중 반올림 자리. 사슬의 정의 그 자체라 설정값이 아니다 (위 2번).
WEIGHT_DP = 6
GENESIS = ""            # 첫 기록의 prev_hash. None 이 아니라 빈 문자열이라야 해시 입력이 안정적이다


def _round(x):
    return None if x is None else round(float(x), WEIGHT_DP)


def decision_record(run_id, stage, mode, as_of, config_hash, variant, market_score, risk_weight,
                    weights):
    """해시 대상이 되는 판단 한 줄. 키 순서는 canonical_json 이 정렬로 고정한다.

    weights 는 [{asset, role, weight}] 또는 {asset: weight} 를 받는다. 자산 이름으로 정렬해
    담으므로 비중 계산의 출력 순서가 바뀌어도 해시는 그대로다.
    """
    if isinstance(weights, dict):
        items = weights.items()
    else:
        items = [(w["asset"], w["weight"]) for w in (weights or [])]
    return {
        "run_id": int(run_id),
        "stage": str(stage),
        "mode": str(mode),
        "as_of": str(as_of),
        "config_hash": str(config_hash or ""),
        "variant": str(variant),
        "market_score": _round(market_score),
        "risk_weight": _round(risk_weight),
        "weights": {str(a): _round(w) for a, w in sorted(items, key=lambda kv: str(kv[0]))},
    }


def canonical_json(record):
    """정규화 JSON. 키 정렬 + 공백 없음이라 같은 판단이면 어디서 만들어도 같은 문자열이다."""
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def record_hash(record, prev_hash):
    """sha256(판단 JSON + 직전 해시). 직전 해시가 없으면 빈 문자열을 이어 붙인다."""
    return hashlib.sha256((canonical_json(record) + (prev_hash or GENESIS)).encode("utf-8")).hexdigest()


def prev_hash_from_store(store, mode):
    """같은 mode 사슬의 마지막 record_hash. 없으면 None (첫 기록)."""
    row = store.conn.execute(
        "SELECT d.record_hash FROM decision d JOIN run r ON r.run_id=d.run_id "
        "WHERE r.mode=? AND d.record_hash IS NOT NULL ORDER BY d.run_id DESC, d.rowid DESC LIMIT 1",
        (str(mode),)).fetchone()
    return row[0] if row else None


def append_ledger(path, record, prev_hash, rec_hash):
    """원장 파일에 한 줄 덧붙인다 (JSON Lines). 디렉터리가 없으면 만든다.

    덧붙이기만 하고 기존 줄은 건드리지 않는다 — 고칠 수 있는 파일이면 증빙이 아니다.
    """
    line = dict(record, prev_hash=prev_hash or GENESIS, record_hash=rec_hash)
    os.makedirs(os.path.dirname(str(path)) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
    return line


def record_from_row(store, run_row, decision_row):
    """DB 의 실행·판단 행 → 해시 대상 판단 줄 (검증할 때 다시 만든다)."""
    weights = store.conn.execute(
        "SELECT asset, weight FROM target_weight WHERE run_id=? AND variant=?",
        (decision_row["run_id"], decision_row["variant"])).fetchall()
    return decision_record(
        run_id=decision_row["run_id"], stage=run_row["stage"], mode=run_row["mode"],
        as_of=run_row["as_of"], config_hash=run_row["config_hash"], variant=decision_row["variant"],
        market_score=decision_row["market_score"], risk_weight=decision_row["risk_weight"],
        weights={w["asset"]: w["weight"] for w in weights})


def chain_from_store(store, mode="live"):
    """[(판단 줄, prev_hash, record_hash)] — 기록된 순서(run_id → 기록 순)대로."""
    rows = store.conn.execute(
        "SELECT d.*, r.stage, r.mode, r.as_of, r.config_hash FROM decision d "
        "JOIN run r ON r.run_id=d.run_id WHERE r.mode=? AND d.record_hash IS NOT NULL "
        "ORDER BY d.run_id, d.rowid", (str(mode),)).fetchall()
    return [(record_from_row(store, r, r), r["prev_hash"], r["record_hash"]) for r in rows]


def chain_from_file(path):
    """원장 파일 → [(판단 줄, prev_hash, record_hash)]. 깨진 줄은 (None, …) 로 남겨 실패를 드러낸다."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln for ln in (line.strip() for line in f) if ln]
    except FileNotFoundError:
        return out
    for ln in lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            out.append((None, None, None))
            continue
        prev = obj.pop("prev_hash", None)
        got = obj.pop("record_hash", None)
        out.append((obj, prev, got))
    return out


def verify_chain(source, mode="live"):
    """(ok, 처음 깨진 기록의 인덱스) — 저장소든 원장 파일이든 같은 규칙으로 검증한다.

    두 가지를 본다.
      1. 각 줄의 record_hash 가 그 줄의 내용 + prev_hash 로 다시 계산되는가 (내용 변조 탐지)
      2. prev_hash 가 앞 줄의 record_hash 와 같은가 (줄 삭제·순서 바꿈 탐지)
    정상이면 (True, None).
    """
    chain = chain_from_file(source) if isinstance(source, (str, bytes, os.PathLike)) \
        else chain_from_store(source, mode)
    expected_prev = None
    for i, (record, prev, got) in enumerate(chain):
        if record is None or not got:
            return False, i
        if (prev or GENESIS) != (expected_prev or GENESIS):
            return False, i
        if record_hash(record, prev) != got:
            return False, i
        expected_prev = got
    return True, None


__all__ = ["GENESIS", "WEIGHT_DP", "append_ledger", "canonical_json", "chain_from_file",
           "chain_from_store", "decision_record", "prev_hash_from_store", "record_from_row",
           "record_hash", "verify_chain"]
